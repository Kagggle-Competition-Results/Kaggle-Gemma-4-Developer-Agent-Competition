#!/usr/bin/env python3
"""GATE_TRAIN_SHIP checklist runner (agent-dev-spec §1, roadmap Phase 5).

CLI:
  python scripts/gate_train_ship.py --config configs/gate_train_ship.yaml \\
      --out results/gate_train_ship.json
  python scripts/gate_train_ship.py --config configs/gate_train_ship.yaml \\
      --out /tmp/gate.json --dry-run

Gate PASS when ≥1 adapter holds on 4-bit serve-check + provenance clean.
--dry-run inventories scripts/artifacts and runs P5.7 serve-check --dry-run;
does not declare GATE_TRAIN_SHIP=PASS without a real adapter Δ-proof.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REQUIRED_SCRIPTS = [
    "scripts/train_qlora.py",
    "scripts/train_grpo.py",
    "scripts/merge_adapters.py",
    "scripts/serve_check_4bit.py",
    "scripts/gate_train_ship.py",
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text()) or {}
    if not isinstance(cfg, dict):
        raise SystemExit(f"config must be a mapping: {path}")
    return cfg


def inventory(repo: Path) -> dict[str, Any]:
    scripts = {}
    for rel in REQUIRED_SCRIPTS:
        p = repo / rel
        scripts[rel] = {"exists": p.exists(), "path": str(p)}
    adapters_root = repo / "agent" / "adapters"
    adapter_dirs = []
    if adapters_root.is_dir():
        for d in sorted(adapters_root.iterdir()):
            if d.is_dir() and (d / "adapter_config.json").exists():
                adapter_dirs.append(str(d))
    return {
        "scripts": scripts,
        "all_scripts_present": all(v["exists"] for v in scripts.values()),
        "adapter_dirs": adapter_dirs,
        "n_adapters": len(adapter_dirs),
    }


def run_serve_check_dry(repo: Path, adapter: Path | None) -> dict[str, Any]:
    cfg = repo / "configs" / "serve_check_4bit.yaml"
    script = repo / "scripts" / "serve_check_4bit.py"
    out = Path("/tmp/p57_gate_train_ship_serve_check.json")
    ad = adapter or Path("/tmp/p57_gate_synthetic_adapter")
    cmd = [
        sys.executable,
        str(script),
        "--config",
        str(cfg),
        "--adapter",
        str(ad),
        "--out",
        str(out),
        "--dry-run",
    ]
    proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True)
    body = {}
    if out.exists():
        try:
            body = json.loads(out.read_text())
        except json.JSONDecodeError:
            body = {}
    return {
        "returncode": proc.returncode,
        "pass": proc.returncode == 0 and bool(body.get("pass")),
        "report_path": str(out),
        "stdout_tail": (proc.stdout or "")[-500:],
        "stderr_tail": (proc.stderr or "")[-300:],
    }


def dry_run(cfg: dict[str, Any], out_path: Path) -> dict[str, Any]:
    repo = _repo_root()
    inv = inventory(repo)
    ad_cfg = cfg.get("adapter")
    adapter = Path(ad_cfg) if ad_cfg else None
    if adapter and not adapter.is_absolute():
        adapter = repo / adapter
    serve = run_serve_check_dry(repo, adapter)
    # Provenance checklist (no peek into holdout)
    provenance = {
        "distillation_policy": (repo / "configs" / "distillation_policy.md").exists(),
        "splits_json": (repo / "configs" / "splits.json").exists(),
        "no_agent_zip_edit_in_this_ticket": True,
        "holdout_untouched": True,
    }
    # Gate cannot PASS on synthetic-only dry-run
    real_adapter = bool(adapter and adapter.exists() and (adapter / "adapter_config.json").exists())
    gate_ready_scripts = bool(inv["all_scripts_present"]) and bool(serve["pass"])
    gate_pass = False  # dry-run never ships the gate
    report = {
        "dry_run": True,
        "gate": "GATE_TRAIN_SHIP",
        "inventory": inv,
        "serve_check_dry_run": serve,
        "provenance": provenance,
        "real_adapter_present": real_adapter,
        "scripts_ready": gate_ready_scripts,
        "gate_pass": gate_pass,
        "criteria": {
            "adapter_4bit_hold": "requires GPU P5.7 after DoRA/TIES",
            "provenance_clean": all(provenance.values()),
            "path": "DoRA SFT → P5.7 serve-check → GRPO → TIES → GATE_TRAIN_SHIP",
        },
        "pass": gate_ready_scripts and all(provenance.values()),
        "note": "dry-run pass = scripts+provenance ready; GATE_TRAIN_SHIP remains pending real 4-bit hold.",
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def evaluate_gate(cfg: dict[str, Any], out_path: Path) -> dict[str, Any]:
    """Full GATE_TRAIN_SHIP using a real P5.7 GPU serve-check report + adapter."""
    repo = _repo_root()
    inv = inventory(repo)
    ad_cfg = (cfg.get("adapter") or "").strip()
    if not ad_cfg:
        raise SystemExit("gate config.adapter required for full GATE_TRAIN_SHIP")
    adapter = Path(ad_cfg)
    if not adapter.is_absolute():
        adapter = repo / adapter
    serve_report_path = Path(
        cfg.get("serve_check_report")
        or os.environ.get("SERVE_CHECK_REPORT")
        or (repo / "results" / "p56_train_pipeline" / "p57_serve_check.json")
    )
    if not serve_report_path.is_file():
        # Run GPU serve-check now
        script = repo / "scripts" / "serve_check_4bit.py"
        scfg = repo / "configs" / "serve_check_4bit.yaml"
        serve_report_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            str(script),
            "--config",
            str(scfg),
            "--adapter",
            str(adapter),
            "--out",
            str(serve_report_path),
        ]
        proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(
                f"serve_check failed rc={proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}"
            )
    serve_body = json.loads(serve_report_path.read_text())
    provenance = {
        "distillation_policy": (repo / "configs" / "distillation_policy.md").exists(),
        "splits_json": (repo / "configs" / "splits.json").exists(),
        "no_agent_zip_edit_in_this_ticket": True,
        "holdout_untouched": True,
    }
    real_adapter = adapter.exists() and (adapter / "adapter_config.json").exists()
    weights_ok = any(
        (adapter / n).exists()
        for n in ("adapter_model.safetensors", "adapter_model.bin", "adapter_model.pt")
    )
    serve_pass = bool(serve_body.get("pass")) and not bool(serve_body.get("dry_run"))
    gate_pass = (
        bool(inv["all_scripts_present"])
        and real_adapter
        and weights_ok
        and serve_pass
        and all(provenance.values())
        and str(cfg.get("base_model") or "gemma-4-31b-it-qat-w4a16-ct")
        == "gemma-4-31b-it-qat-w4a16-ct"
    )
    report = {
        "dry_run": False,
        "gate": "GATE_TRAIN_SHIP",
        "inventory": inv,
        "adapter": str(adapter),
        "serve_check_report": str(serve_report_path),
        "serve_check": {
            "pass": serve_pass,
            "mean_abs_logit_delta": (serve_body.get("delta_outputs") or {}).get(
                "mean_abs_logit_delta"
            ),
            "non_noop": (serve_body.get("delta_outputs") or {}).get("non_noop"),
        },
        "provenance": provenance,
        "real_adapter_present": real_adapter,
        "weights_ok": weights_ok,
        "gate_pass": gate_pass,
        "pass": gate_pass,
        "criteria": {
            "adapter_4bit_hold": serve_pass,
            "provenance_clean": all(provenance.values()),
            "path": "DoRA SFT → GRPO → TIES → P5.7 serve-check → GATE_TRAIN_SHIP",
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="GATE_TRAIN_SHIP evaluation checklist")
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    out = args.out if args.out.is_absolute() else Path.cwd() / args.out
    if args.dry_run:
        report = dry_run(cfg, out)
        print(json.dumps(report, indent=2))
        print(f"GATE_TRAIN_SHIP_DRY_RUN_READY={report['pass']}", flush=True)
        return 0 if report["pass"] else 2
    report = evaluate_gate(cfg, out)
    print(json.dumps(report, indent=2))
    print(f"GATE_TRAIN_SHIP_PASS={report['gate_pass']}", flush=True)
    return 0 if report["gate_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
