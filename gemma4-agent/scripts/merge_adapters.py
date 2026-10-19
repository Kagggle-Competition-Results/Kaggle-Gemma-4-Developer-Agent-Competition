#!/usr/bin/env python3
"""P5.19–21 — Plan A TIES / DARE merge (agent-dev-spec §5–§6).

CLI:
  python scripts/merge_adapters.py --config configs/merge_ties.yaml \\
      --out agent/adapters/master_swe_adapter
  python scripts/merge_adapters.py --config configs/merge_ties.yaml \\
      --adapters path/a path/b --out /tmp/merged --dry-run

Plan A: TIES (trim → elect sign → disjoint merge); DARE-style density trim.
Must pass P5.7 4-bit serve-check before shipping the merged adapter.

W4A16-CT (`gemma-4-31b-it-qat-w4a16-ct`):
  - peft_delta_then_apply (preferred): TIES on PEFT/DoRA deltas, apply to a
    bf16/fp16 reference when available; CT pack stays the serve base until
    requant tooling is run on the train host.
  - dequant_ties_requant: dequant CT → dense TIES → requant (GPU/host; not in dry-run).
  - plan_b_multi_lora: skip merge; serve r≤64 adapters via vLLM LoRA (G14 Plan B).

--dry-run: validate YAML + TIES math on synthetic CPU tensors (no GPU, no CT I/O).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from pathlib import Path
from typing import Any

import yaml

ALLOWED_METHODS = {"ties"}
ALLOWED_CT_MODES = {"peft_delta_then_apply", "dequant_ties_requant", "plan_b_multi_lora"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text()) or {}
    if not isinstance(cfg, dict):
        raise SystemExit(f"config must be a mapping: {path}")
    return cfg


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    method = str(cfg.get("method", "ties")).lower()
    if method not in ALLOWED_METHODS:
        raise SystemExit(f"method must be one of {sorted(ALLOWED_METHODS)}, got {method!r}")
    density = float(cfg.get("density", 0.2))
    if not (0.0 < density <= 1.0):
        raise SystemExit(f"density must be in (0,1], got {density}")
    max_rank = int(cfg.get("max_rank", 64))
    if max_rank > 64:
        raise SystemExit(f"max_rank={max_rank} violates G14 (≤64)")
    ct_mode = str(cfg.get("w4a16_ct_mode", "peft_delta_then_apply"))
    if ct_mode not in ALLOWED_CT_MODES:
        raise SystemExit(f"w4a16_ct_mode must be one of {sorted(ALLOWED_CT_MODES)}")
    out = dict(cfg)
    out.update(
        {
            "method": method,
            "density": density,
            "max_rank": max_rank,
            "w4a16_ct_mode": ct_mode,
            "base_model": str(cfg.get("base_model") or "gemma-4-31b-it-qat-w4a16-ct"),
            "sign_consensus": str(cfg.get("sign_consensus") or "majority"),
            "disjoint": bool(cfg.get("disjoint", True)),
            "seed": int(cfg.get("seed", 42)),
        }
    )
    return out


def ties_merge(
    deltas: list[list[float]],
    *,
    density: float,
    weights: list[float] | None = None,
    disjoint: bool = True,
) -> list[float]:
    """TIES on 1-D parameter vectors: trim → elect sign → weighted disjoint merge."""
    if not deltas:
        raise ValueError("no deltas")
    n = len(deltas[0])
    for d in deltas:
        if len(d) != n:
            raise ValueError("delta length mismatch")
    m = len(deltas)
    if weights is None or not weights:
        weights = [1.0 / m] * m
    if len(weights) != m:
        raise ValueError("weights length mismatch")
    wsum = sum(weights) or 1.0
    weights = [w / wsum for w in weights]

    # 1) Trim: keep top density fraction by |value| per adapter (DARE-style magnitude keep)
    k = max(1, int(math.ceil(density * n)))
    trimmed: list[list[float]] = []
    for d in deltas:
        order = sorted(range(n), key=lambda i: abs(d[i]), reverse=True)
        keep = set(order[:k])
        trimmed.append([d[i] if i in keep else 0.0 for i in range(n)])

    # 2) Elect sign (majority of non-zero signs, weighted by |mass|)
    elected: list[float] = []
    for i in range(n):
        pos = sum(weights[j] for j, d in enumerate(trimmed) if d[i] > 0)
        neg = sum(weights[j] for j, d in enumerate(trimmed) if d[i] < 0)
        if pos == 0 and neg == 0:
            elected.append(0.0)
        elif pos >= neg:
            elected.append(1.0)
        else:
            elected.append(-1.0)

    # 3) Disjoint merge: average values agreeing with elected sign
    out = [0.0] * n
    for i in range(n):
        sign = elected[i]
        if sign == 0.0:
            continue
        num = 0.0
        den = 0.0
        for j, d in enumerate(trimmed):
            if d[i] == 0.0:
                continue
            agree = (d[i] > 0 and sign > 0) or (d[i] < 0 and sign < 0)
            if disjoint and not agree:
                continue
            num += weights[j] * d[i]
            den += weights[j]
        if den > 0:
            out[i] = num / den
    return out


def dry_run_ties(cfg: dict[str, Any]) -> dict[str, Any]:
    rng = random.Random(int(cfg["seed"]))
    n = 64
    # Two synthetic adapters with shared + conflicting signs
    a = [rng.uniform(-1, 1) for _ in range(n)]
    b = [x + rng.uniform(-0.2, 0.2) for x in a]
    # Flip a block of signs in b to create conflict
    for i in range(0, n, 4):
        b[i] = -abs(b[i]) if a[i] >= 0 else abs(b[i])
    density = float(cfg["density"])
    merged = ties_merge([a, b], density=density, weights=[0.5, 0.5], disjoint=bool(cfg["disjoint"]))

    # Invariants
    k = max(1, int(math.ceil(density * n)))
    # After trim, each vector has ≤k nonzeros — merge support ⊆ union of trimmed supports
    def trim_support(d: list[float]) -> set[int]:
        order = sorted(range(n), key=lambda i: abs(d[i]), reverse=True)
        return set(order[:k])

    support = trim_support(a) | trim_support(b)
    outside = [i for i in range(n) if i not in support and merged[i] != 0.0]
    # Sign agreement: nonzero merged entries should match majority sign of trimmed
    sign_viol = 0
    trimmed_a = [a[i] if i in trim_support(a) else 0.0 for i in range(n)]
    trimmed_b = [b[i] if i in trim_support(b) else 0.0 for i in range(n)]
    for i, v in enumerate(merged):
        if v == 0.0:
            continue
        votes = []
        if trimmed_a[i] != 0:
            votes.append(1 if trimmed_a[i] > 0 else -1)
        if trimmed_b[i] != 0:
            votes.append(1 if trimmed_b[i] > 0 else -1)
        if not votes:
            continue
        maj = 1 if votes.count(1) >= votes.count(-1) else -1
        if (v > 0 and maj < 0) or (v < 0 and maj > 0):
            sign_viol += 1

    report = {
        "dry_run": True,
        "method": cfg["method"],
        "density": density,
        "base_model": cfg["base_model"],
        "max_rank": cfg["max_rank"],
        "w4a16_ct_mode": cfg["w4a16_ct_mode"],
        "sign_consensus": cfg["sign_consensus"],
        "disjoint": cfg["disjoint"],
        "synthetic": {
            "n": n,
            "nnz_merged": sum(1 for x in merged if x != 0.0),
            "outside_trim_support_nnz": len(outside),
            "sign_violations": sign_viol,
        },
        "plan_a": "TIES+DARE density trim → master_swe_adapter; then P5.7 4-bit serve-check",
        "plan_b": "multi-LoRA vLLM serve (r≤64) without merge if Plan A parity <95%",
        "output_format": {
            "expected_files": ["adapter_config.json", "adapter_model.safetensors", "merge_ties_meta.json"],
            "compatible_with": "PEFT LoRA/DoRA load or merged dense weights post-requant",
        },
        "pass": len(outside) == 0 and sign_viol == 0,
    }
    return report


def _load_adapter_state(ad: Path) -> dict[str, Any]:
    """Load PEFT adapter tensors (safetensors or bin) without base model."""
    try:
        import torch
    except ImportError as exc:
        raise SystemExit(f"torch required for merge: {exc}") from exc

    st_path = ad / "adapter_model.safetensors"
    bin_path = ad / "adapter_model.bin"
    if st_path.is_file():
        try:
            from safetensors.torch import load_file

            return {k: v for k, v in load_file(str(st_path)).items()}
        except Exception:
            pass
    if bin_path.is_file():
        obj = torch.load(str(bin_path), map_location="cpu")
        if isinstance(obj, dict):
            return obj
    raise SystemExit(f"no adapter weights under {ad}")


def merge_from_adapters(cfg: dict[str, Any], adapter_dirs: list[Path], out_dir: Path) -> dict[str, Any]:
    """PEFT-space TIES merge → loadable master_swe_adapter (no full base load)."""
    import shutil

    out_dir.mkdir(parents=True, exist_ok=True)

    if cfg["w4a16_ct_mode"] == "plan_b_multi_lora":
        # Plan B: ship the last (typically GRPO) adapter as master for LoRA serve.
        src = adapter_dirs[-1]
        for name in ("adapter_config.json", "adapter_model.safetensors", "adapter_model.bin"):
            src_p = src / name
            if src_p.is_file():
                shutil.copy2(src_p, out_dir / name)
        meta = {
            "plan": "B",
            "action": "copy_last_adapter",
            "adapters": [str(p) for p in adapter_dirs],
            "source": str(src),
            "note": "Serve via vLLM LoRA beside gemma-4-31b-it-qat-w4a16-ct",
        }
        (out_dir / "merge_ties_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        return meta

    try:
        import torch
        from safetensors.torch import save_file
    except ImportError as exc:
        raise SystemExit(
            f"full merge imports missing ({exc}). Use --dry-run on CPU, or install "
            "torch safetensors on the train host."
        ) from exc

    if len(adapter_dirs) == 1:
        src = adapter_dirs[0]
        for name in ("adapter_config.json", "adapter_model.safetensors", "adapter_model.bin"):
            src_p = src / name
            if src_p.is_file():
                shutil.copy2(src_p, out_dir / name)
        meta = {
            "plan": "A",
            "method": "ties",
            "action": "single_adapter_copy",
            "density": float(cfg["density"]),
            "base_model": cfg["base_model"],
            "w4a16_ct_mode": cfg["w4a16_ct_mode"],
            "adapters": [str(p) for p in adapter_dirs],
            "note": "Single adapter → master_swe_adapter (TIES N/A); P5.7 required",
        }
        (out_dir / "merge_ties_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        return meta

    states: list[dict[str, Any]] = []
    ranks: list[int] = []
    for ad in adapter_dirs:
        conf_p = ad / "adapter_config.json"
        if conf_p.is_file():
            ac = json.loads(conf_p.read_text())
            r = int(ac.get("r") or ac.get("rank") or 0)
            ranks.append(r)
            if r > int(cfg["max_rank"]):
                raise SystemExit(f"adapter {ad} rank={r} > max_rank={cfg['max_rank']}")
        states.append(_load_adapter_state(ad))

    keys = sorted(set.intersection(*(set(s.keys()) for s in states)))
    if not keys:
        raise SystemExit("no shared adapter keys to TIES-merge")

    density = float(cfg["density"])
    weights = list(cfg.get("weights") or []) or None
    merged: dict[str, Any] = {}
    nnz_report: dict[str, Any] = {}
    for key in keys:
        tensors = [s[key].detach().float().cpu() for s in states]
        shape = tensors[0].shape
        vecs = [t.flatten().tolist() for t in tensors]
        merged_flat = ties_merge(
            vecs, density=density, weights=weights, disjoint=bool(cfg["disjoint"])
        )
        merged[key] = torch.tensor(merged_flat, dtype=tensors[0].dtype).reshape(shape)
        nnz_report[key] = {
            "n": len(merged_flat),
            "nnz": sum(1 for x in merged_flat if x != 0.0),
        }

    # Prefer GRPO adapter_config (last) as schema; rewrite base path if present.
    conf_src = adapter_dirs[-1] / "adapter_config.json"
    if conf_src.is_file():
        shutil.copy2(conf_src, out_dir / "adapter_config.json")
    else:
        shutil.copy2(adapter_dirs[0] / "adapter_config.json", out_dir / "adapter_config.json")

    save_file(merged, str(out_dir / "adapter_model.safetensors"))
    meta = {
        "plan": "A",
        "method": "ties",
        "density": density,
        "base_model": cfg["base_model"],
        "w4a16_ct_mode": cfg["w4a16_ct_mode"],
        "adapters": [str(p) for p in adapter_dirs],
        "ranks": ranks,
        "n_keys_merged": len(merged),
        "note": "PEFT-delta TIES saved as loadable adapter; CT base unchanged; P5.7 required",
    }
    (out_dir / "merge_ties_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    (out_dir / "ties_merged_deltas.json").write_text(json.dumps(nnz_report, indent=2) + "\n")
    return meta


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="P5.19–21 TIES merge (agent-dev-spec §5)")
    ap.add_argument("--config", type=Path, required=True, help="configs/merge_ties.yaml")
    ap.add_argument("--out", type=Path, required=True, help="agent/adapters/master_swe_adapter")
    ap.add_argument(
        "--adapters",
        nargs="*",
        default=None,
        help="adapter directories (default: config.adapters)",
    )
    ap.add_argument("--dry-run", action="store_true", help="CPU TIES math + config check only")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = validate_config(load_config(args.config))
    if args.dry_run:
        report = dry_run_ties(cfg)
        print(json.dumps(report, indent=2))
        print(f"MERGE_DRY_RUN_PASS={report['pass']}", flush=True)
        return 0 if report["pass"] else 2

    adapter_dirs = [Path(p) for p in (args.adapters or cfg.get("adapters") or [])]
    if not adapter_dirs:
        raise SystemExit("no adapters provided (--adapters or config.adapters)")
    for p in adapter_dirs:
        if not p.is_dir():
            raise SystemExit(f"adapter dir missing: {p}")
    out_dir = args.out if args.out.is_absolute() else Path.cwd() / args.out
    meta = merge_from_adapters(cfg, adapter_dirs, out_dir)
    print(json.dumps(meta, indent=2))
    print(f"MERGE_OUT={out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
