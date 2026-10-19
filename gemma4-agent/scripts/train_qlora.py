#!/usr/bin/env python3
"""P5.6 — DoRA / QLoRA SFT (agent-dev-spec §5–§6).

CLI:
  python scripts/train_qlora.py --config configs/train_qlora.yaml \\
      --data data_train/sft.jsonl --out agent/adapters/<name>
  python scripts/train_qlora.py --config configs/train_qlora.yaml \\
      --data data_train/sft.jsonl --out /tmp/unused --dry-run

Config keys (spec): peft_type ∈ {dora,lora}; rank ∈ {32,64}; α=2r; LR 2e-4;
targets attn+MLP; max_seq_length ≤28000.

Adapter integration (scaffold-freeze-train-plan / Plan A–B):
  Plan A (preferred): train DoRA on gemma-4-31b-it-qat-w4a16-ct → save adapter
  under --out → P5.7 4-bit serve-check → scripts/merge_adapters.py TIES merge
  into agent/adapters/master_swe_adapter for zip.
  Plan B: leave adapter unmerged; load with vLLM LoRA
  (max_lora_rank ≥ config.rank) beside the same QAT W4A16 base.

--dry-run: CPU-only dataset/tokenizer/label-mask check (no GPU, no weight load).
GPU train requires peft+transformers+bitsandbytes and a live CLOUD_APPROVAL
when using cloud GPUs (G16). Do not start GPU train until keeper coverage
warrants P5.6 (scaffold-freeze-train-plan).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

import yaml

IGNORE_INDEX = -100
ALLOWED_RANKS = {32, 64}
ALLOWED_PEFT = {"dora", "lora"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text()) or {}
    if not isinstance(cfg, dict):
        raise SystemExit(f"config must be a mapping: {path}")
    return cfg


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    peft = str(cfg.get("peft_type", "dora")).lower()
    if peft not in ALLOWED_PEFT:
        raise SystemExit(f"peft_type must be one of {sorted(ALLOWED_PEFT)}, got {peft!r}")
    rank = int(cfg.get("rank", 64))
    if rank not in ALLOWED_RANKS:
        raise SystemExit(f"rank must be one of {sorted(ALLOWED_RANKS)} (G14 ≤64), got {rank}")
    alpha = int(cfg.get("lora_alpha", 2 * rank))
    if alpha != 2 * rank:
        print(f"WARN lora_alpha={alpha} != 2*rank={2 * rank} (spec α=2r)", flush=True)
    max_seq = int(cfg.get("max_seq_length", 16384))
    if max_seq > 28000:
        raise SystemExit(f"max_seq_length={max_seq} exceeds spec ≤28000")
    if max_seq > 32768:
        raise SystemExit(f"max_seq_length={max_seq} exceeds context 32768")
    targets = cfg.get("target_modules") or []
    if not targets:
        raise SystemExit("target_modules required (spec: attn + MLP)")
    lr = float(cfg.get("learning_rate", 2.0e-4))
    out = dict(cfg)
    out.update(
        {
            "peft_type": peft,
            "rank": rank,
            "lora_alpha": alpha,
            "max_seq_length": max_seq,
            "learning_rate": lr,
            "assistant_only_loss": bool(cfg.get("assistant_only_loss", True)),
            "base_model": str(cfg.get("base_model") or "gemma-4-31b-it-qat-w4a16-ct"),
        }
    )
    return out


def resolve_model_path(cfg: dict[str, Any]) -> Path:
    raw = (cfg.get("model_path") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    env = os.environ.get("MODEL_PATH") or os.environ.get("GEMMA4_MODEL_PATH")
    if env:
        return Path(env).expanduser().resolve()
    base = cfg["base_model"]
    # Common local / pod layouts
    candidates = [
        Path("/models") / "gemma-4-31B-it-qat-w4a16-ct",
        Path("/models") / base,
        _repo_root() / "models" / "gemma-4-31B-it-qat-w4a16-ct",
        Path("/tmp/p52b_gemma_tok"),  # tokenizer-only cache from P5.2b audit
    ]
    for c in candidates:
        if (c / "tokenizer.json").is_file() or (c / "config.json").is_file():
            return c.resolve()
    raise SystemExit(
        "model_path unresolved: set configs/train_qlora.yaml model_path or $MODEL_PATH "
        f"(need tokenizer.json under a dir for {base})"
    )


def load_sft_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"sft.jsonl parse error line {i}: {exc}") from exc
        for key in ("task_id", "messages"):
            if key not in obj:
                raise SystemExit(f"sft.jsonl line {i} missing key {key!r}")
        if not isinstance(obj["messages"], list) or not obj["messages"]:
            raise SystemExit(f"sft.jsonl line {i} messages must be non-empty list")
        rows.append(obj)
    if not rows:
        raise SystemExit(f"no rows in {path}")
    return rows


def _as_str(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def normalize_messages_for_gemma(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map render_trajectories JSONL turns into Gemma-4 chat-template shapes.

    Assistant turns store tool calls as a JSON string in content; promote to
    structured tool_calls so the canonical template emits <|tool_call|> blocks
    and consumes following role=tool messages.
    """
    out: list[dict[str, Any]] = []
    pending_tool_ids: list[str] = []
    call_i = 0
    for m in messages:
        role = m.get("role") or "user"
        content = _as_str(m.get("content"))
        if role == "assistant":
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
                tcs = []
                pending_tool_ids = []
                for tc in payload["tool_calls"]:
                    cid = f"call_{call_i}"
                    call_i += 1
                    pending_tool_ids.append(cid)
                    args = tc.get("arguments") if isinstance(tc, dict) else {}
                    # Gemma-4 chat template requires arguments as a mapping, not a JSON string.
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args.strip() else {}
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    if not isinstance(args, dict):
                        args = {"value": args}
                    tcs.append(
                        {
                            "id": cid,
                            "type": "function",
                            "function": {
                                "name": str((tc or {}).get("name") or "unknown"),
                                "arguments": args,
                            },
                        }
                    )
                msg: dict[str, Any] = {"role": "assistant", "tool_calls": tcs, "content": ""}
                if payload.get("text"):
                    msg["content"] = _as_str(payload["text"])
                out.append(msg)
                continue
            out.append({"role": "assistant", "content": content})
            pending_tool_ids = []
        elif role == "tool":
            tid = pending_tool_ids.pop(0) if pending_tool_ids else f"call_{call_i}"
            if not pending_tool_ids and tid.startswith("call_") and tid == f"call_{call_i}":
                call_i += 1
            out.append({"role": "tool", "content": content, "tool_call_id": tid})
        else:
            out.append({"role": role, "content": content})
            pending_tool_ids = []
    return out


def _load_chat_template(model_dir: Path) -> str:
    cfg_path = model_dir / "tokenizer_config.json"
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text())
        if cfg.get("chat_template"):
            return str(cfg["chat_template"])
    jinja = model_dir / "chat_template.jinja"
    if jinja.is_file():
        return jinja.read_text()
    raise SystemExit(f"no chat_template in {model_dir}")


def apply_chat_template(messages: list[dict[str, Any]], model_dir: Path) -> str:
    import jinja2

    tmpl = _load_chat_template(model_dir)
    cfg: dict[str, Any] = {}
    cfg_path = model_dir / "tokenizer_config.json"
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text())

    env = jinja2.Environment()

    def raise_exception(msg: str) -> None:
        raise jinja2.TemplateError(msg)

    env.globals["raise_exception"] = raise_exception
    template = env.from_string(tmpl)
    return template.render(
        messages=messages,
        add_generation_prompt=False,
        enable_thinking=False,
        tools=None,
        bos_token=cfg.get("bos_token", "<bos>"),
        eos_token=cfg.get("eos_token", "<eos>"),
    )


def load_tokenizer_backend(model_dir: Path):
    """Prefer tokenizers.Tokenizer (CPU, no torch); else transformers.AutoTokenizer."""
    tok_json = model_dir / "tokenizer.json"
    if tok_json.is_file():
        try:
            from tokenizers import Tokenizer

            return ("tokenizers", Tokenizer.from_file(str(tok_json)))
        except ImportError:
            pass
    try:
        from transformers import AutoTokenizer

        tok = AutoTokenizer.from_pretrained(str(model_dir), trust_remote_code=True)
        return ("transformers", tok)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"cannot load tokenizer from {model_dir}: {exc} "
            "(install tokenizers or transformers)"
        ) from exc


def encode_text(backend: str, tok: Any, text: str) -> list[int]:
    if backend == "tokenizers":
        return list(tok.encode(text).ids)
    return list(tok(text, add_special_tokens=False)["input_ids"])


def _char_ranges_to_unmask(text: str) -> list[tuple[int, int]]:
    """Assistant-only char spans: tool_call blocks + model free-text; never tool_response.

    Gemma-4 keeps tool_response inside the same <|turn>model ... <turn|> turn as
    tool_calls. Supervising the whole model turn therefore leaks env/tool returns
    into the loss. We unmask only:
      - <|tool_call> ... <tool_call|>
      - <|channel>thought ... <channel|> (optional reasoning)
      - other model-turn text outside <|tool_response> ... <tool_response|>
    """
    ranges: list[tuple[int, int]] = []

    def add(a: int, b: int) -> None:
        if 0 <= a < b <= len(text):
            ranges.append((a, b))

    # 1) Explicit tool-call invocations
    pos = 0
    while True:
        s = text.find("<|tool_call>", pos)
        if s < 0:
            break
        e = text.find("<tool_call|>", s)
        if e < 0:
            break
        add(s, e + len("<tool_call|>"))
        pos = e + len("<tool_call|>")

    # 2) Thought channels (when present)
    pos = 0
    while True:
        s = text.find("<|channel>thought", pos)
        if s < 0:
            break
        e = text.find("<channel|>", s)
        if e < 0:
            break
        add(s, e + len("<channel|>"))
        pos = e + len("<channel|>")

    # 3) Model-turn free text excluding tool_response (and excluding regions
    #    already covered by tool_call/thought — duplicates are fine / idempotent)
    start_mark = "<|turn>model\n"
    end_mark = "<turn|>"
    pos = 0
    while True:
        s = text.find(start_mark, pos)
        if s < 0:
            break
        content_start = s + len(start_mark)
        e = text.find(end_mark, content_start)
        turn_end = len(text) if e < 0 else e
        # Walk model-turn body, skipping tool_response blocks
        cursor = content_start
        while cursor < turn_end:
            tr = text.find("<|tool_response>", cursor, turn_end)
            if tr < 0:
                add(cursor, turn_end)
                break
            add(cursor, tr)
            tr_end = text.find("<tool_response|>", tr, turn_end)
            if tr_end < 0:
                break
            cursor = tr_end + len("<tool_response|>")
        pos = turn_end + (0 if e < 0 else len(end_mark))

    # Merge overlapping ranges
    if not ranges:
        return []
    ranges.sort()
    merged: list[tuple[int, int]] = [ranges[0]]
    for a, b in ranges[1:]:
        la, lb = merged[-1]
        if a <= lb:
            merged[-1] = (la, max(lb, b))
        else:
            merged.append((a, b))
    return merged


def build_labels_assistant_only(text: str, input_ids: list[int], backend: str, tok: Any) -> list[int]:
    """Supervise assistant tool-calls + reasoning only; mask system/user/tool_response (-100)."""
    labels = [IGNORE_INDEX] * len(input_ids)
    for a, b in _char_ranges_to_unmask(text):
        prefix_n = len(encode_text(backend, tok, text[:a]))
        with_span = len(encode_text(backend, tok, text[:b]))
        for i in range(prefix_n, min(with_span, len(labels))):
            labels[i] = input_ids[i]
    return labels


def classify_supervised_leakage(
    text: str, input_ids: list[int], labels: list[int], backend: str, tok: Any
) -> dict[str, Any]:
    """Token counts for audit: tool_call vs tool_response vs other supervised."""
    n_sup = sum(1 for x in labels if x != IGNORE_INDEX)
    # Count supervised tokens overlapping tool_response / tool_call char ranges
    def count_overlap(mark_start: str, mark_end: str) -> int:
        n = 0
        pos = 0
        while True:
            s = text.find(mark_start, pos)
            if s < 0:
                break
            e = text.find(mark_end, s)
            if e < 0:
                break
            e2 = e + len(mark_end)
            pn = len(encode_text(backend, tok, text[:s]))
            wn = len(encode_text(backend, tok, text[:e2]))
            for i in range(pn, min(wn, len(labels))):
                if labels[i] != IGNORE_INDEX:
                    n += 1
            pos = e2
        return n

    n_tc = count_overlap("<|tool_call>", "<tool_call|>")
    n_tr = count_overlap("<|tool_response>", "<tool_response|>")
    return {
        "supervised_tokens": n_sup,
        "supervised_in_tool_call_blocks": n_tc,
        "supervised_in_tool_response_blocks": n_tr,
        "tool_response_leak": n_tr > 0,
    }


def dry_run(cfg: dict[str, Any], data_path: Path) -> dict[str, Any]:
    model_dir = resolve_model_path(cfg)
    backend, tok = load_tokenizer_backend(model_dir)
    rows = load_sft_rows(data_path)
    max_seq = int(cfg["max_seq_length"])
    per_row: list[dict[str, Any]] = []
    n_trunc = 0
    n_ok = 0
    supervised_token_count = 0
    total_token_count = 0
    lengths: list[int] = []
    mask_issues: list[str] = []
    leak_tool_response_tokens = 0
    supervised_in_tool_call_tokens = 0
    source_role_chars: dict[str, int] = {"system": 0, "user": 0, "assistant": 0, "tool": 0}
    audit_samples: list[dict[str, Any]] = []

    for row in rows:
        for m in row["messages"]:
            role = str(m.get("role") or "user")
            source_role_chars[role] = source_role_chars.get(role, 0) + len(_as_str(m.get("content")))
        norm = normalize_messages_for_gemma(row["messages"])
        try:
            text = apply_chat_template(norm, model_dir)
        except Exception as exc:  # noqa: BLE001
            mask_issues.append(f"{row['task_id']}: chat_template_error: {exc}")
            continue
        ids_full = encode_text(backend, tok, text)
        n = len(ids_full)
        lengths.append(n)
        truncated = n > max_seq
        if truncated:
            n_trunc += 1
        ids = ids_full[:max_seq]
        labels_full = build_labels_assistant_only(text, ids_full, backend, tok)
        labels = labels_full[: len(ids)]
        n_active = sum(1 for x in labels if x != IGNORE_INDEX)
        leak = classify_supervised_leakage(text, ids_full, labels_full, backend, tok)
        leak_tool_response_tokens += int(leak["supervised_in_tool_response_blocks"])
        supervised_in_tool_call_tokens += int(leak["supervised_in_tool_call_blocks"])
        if leak["tool_response_leak"]:
            mask_issues.append(
                f"{row['task_id']}: tool_response_leak_tokens={leak['supervised_in_tool_response_blocks']}"
            )
        # Assistant tool-call JSON / template tool_call markers must have some supervised tokens
        if "<|tool_call>" in text or "tool_calls" in json.dumps(row["messages"]):
            if n_active == 0:
                mask_issues.append(f"{row['task_id']}: no_supervised_tokens")
        # Sanity: system/user prefixes should not be fully supervised
        if n_active >= len(ids):
            mask_issues.append(f"{row['task_id']}: labels_not_masked")
        supervised_token_count += n_active
        total_token_count += len(ids)
        n_ok += 1
        row_rep = {
            "task_id": row["task_id"],
            "n_messages": len(row["messages"]),
            "n_tokens": n,
            "n_tokens_used": len(ids),
            "n_supervised": n_active,
            "supervised_ratio": round(n_active / max(1, len(ids)), 4),
            "truncated": truncated,
            "has_tool_calls": any(
                '"tool_calls"' in _as_str(m.get("content"))
                for m in row["messages"]
                if m.get("role") == "assistant"
            ),
            "supervised_in_tool_call_blocks": leak["supervised_in_tool_call_blocks"],
            "supervised_in_tool_response_blocks": leak["supervised_in_tool_response_blocks"],
        }
        per_row.append(row_rep)
        if len(audit_samples) < 2:
            # Decode a short supervised head for the report
            if backend == "tokenizers":
                sup_ids = [i for i, lab in zip(ids_full, labels_full) if lab != IGNORE_INDEX]
                head = tok.decode(sup_ids[:120]) if hasattr(tok, "decode") else ""
            else:
                head = ""
            audit_samples.append(
                {
                    **row_rep,
                    "supervised_decode_head": head[:300],
                    "contains_masked_tool_status": 'status": "masked"' in head
                    or "status\": \"masked\"" in head,
                }
            )

    def pct(xs: list[int], p: float) -> int:
        xs = sorted(xs)
        if not xs:
            return 0
        k = (len(xs) - 1) * p / 100.0
        f = int(k)
        c = min(f + 1, len(xs) - 1)
        if f == c:
            return int(xs[f])
        return int(xs[f] * (c - k) + xs[c] * (k - f))

    report = {
        "dry_run": True,
        "backend": backend,
        "model_dir": str(model_dir),
        "base_model": cfg["base_model"],
        "peft_type": cfg["peft_type"],
        "rank": cfg["rank"],
        "lora_alpha": cfg["lora_alpha"],
        "max_seq_length": max_seq,
        "learning_rate": cfg["learning_rate"],
        "target_modules": cfg.get("target_modules"),
        "assistant_only_loss": cfg["assistant_only_loss"],
        "n_rows": len(rows),
        "n_formatted_ok": n_ok,
        "n_truncated": n_trunc,
        "mask_issues": mask_issues,
        "token_stats": {
            "min": int(min(lengths)) if lengths else 0,
            "median": int(statistics.median(lengths)) if lengths else 0,
            "p95": pct(lengths, 95) if lengths else 0,
            "max": int(max(lengths)) if lengths else 0,
            "mean": round(sum(lengths) / len(lengths), 1) if lengths else 0,
        },
        "label_stats": {
            "active_tokens": supervised_token_count,
            "total_tokens": total_token_count,
            "active_ratio": round(supervised_token_count / max(1, total_token_count), 4),
            "supervised_in_tool_call_blocks": supervised_in_tool_call_tokens,
            "supervised_in_tool_response_blocks": leak_tool_response_tokens,
            "tool_response_leak": leak_tool_response_tokens > 0,
        },
        "source_role_char_shares": {
            k: {
                "chars": v,
                "share": round(v / max(1, sum(source_role_chars.values())), 4),
            }
            for k, v in sorted(source_role_chars.items())
        },
        "mask_audit_samples": audit_samples,
        "pass": n_ok == len(rows)
        and n_trunc == 0
        and not mask_issues
        and leak_tool_response_tokens == 0,
        "per_row": per_row,
        "adapter_plan": {
            "plan_a": "merge_adapters.py TIES → agent/adapters/master_swe_adapter after P5.7",
            "plan_b": "vLLM LoRA load of --out beside gemma-4-31b-it-qat-w4a16-ct",
        },
    }
    return report


def build_peft_config(cfg: dict[str, Any]):
    from peft import LoraConfig, TaskType

    use_dora = cfg["peft_type"] == "dora"
    return LoraConfig(
        r=int(cfg["rank"]),
        lora_alpha=int(cfg["lora_alpha"]),
        lora_dropout=float(cfg.get("lora_dropout", 0.05)),
        target_modules=list(cfg["target_modules"]),
        bias="none",
        task_type=TaskType.CAUSAL_LM,
        use_dora=use_dora,
    )


def replace_gemma4_clippable_with_nn_linear_proxies(model: Any) -> int:
    """Swap Gemma4ClippableLinear → nn.Linear subclass proxies PEFT can inject into.

    PEFT only accepts torch.nn.Linear. W4A16-CT Gemma-4 uses Gemma4ClippableLinear
    wrappers; dispatch_default patches are ineffective because PEFT caches the
    dispatcher list at import time. Proxies subclass nn.Linear, keep Clippable
    forward (correct CT matmul), and expose a Parameter weight for LoRA/DoRA init.
    """
    import torch
    import torch.nn as nn

    def _resolve_in_out(clip_mod: Any) -> tuple[int, int]:
        # NOTE: do not nest getattr(a, k, getattr(b, k)) — the default is evaluated eagerly
        # and Gemma4ClippableLinear raises on missing in_features via Module.__getattr__.
        inner = getattr(clip_mod, "linear", None)
        for obj in (inner, clip_mod):
            if obj is None:
                continue
            try:
                return int(obj.in_features), int(obj.out_features)
            except Exception:
                pass
            try:
                w = obj.weight
                return int(w.shape[1]), int(w.shape[0])
            except Exception:
                pass
        raise AttributeError(f"cannot resolve in/out for {type(clip_mod).__name__}")

    class _ClippableLinearProxy(nn.Linear):
        def __init__(self, clip_mod: Any):
            in_f, out_f = _resolve_in_out(clip_mod)
            # Real nn.Linear init so .weight exists for PEFT/DoRA feature discovery.
            nn.Linear.__init__(self, in_f, out_f, bias=False)
            # Do NOT register clip as a child module (avoids PEFT walking into CT Linear).
            object.__setattr__(self, "_clip_mod", clip_mod)
            self.weight.requires_grad_(False)

        def forward(self, x: Any) -> Any:  # type: ignore[override]
            return object.__getattribute__(self, "_clip_mod")(x)

    def _needs_proxy(module: Any) -> bool:
        name = type(module).__name__
        if name == "_ClippableLinearProxy":
            return False
        if name == "Gemma4ClippableLinear":
            return True
        # CT / compressed Linear often subclasses nn.Linear but has no .weight Parameter.
        if name == "Linear" or isinstance(module, nn.Linear):
            try:
                w = module.weight
                return w is None
            except Exception:
                return True
        return False

    replaced = 0
    skipped = 0
    # Only touch leaf names PEFT will target (plus ClippableLinear anywhere).
    target_leaves = {
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "gate_proj",
        "up_proj",
        "down_proj",
    }
    for name, module in list(model.named_modules()):
        leaf = name.rsplit(".", 1)[-1]
        if type(module).__name__ != "Gemma4ClippableLinear" and leaf not in target_leaves:
            continue
        if not _needs_proxy(module):
            continue
        try:
            proxy = _ClippableLinearProxy(module)
        except Exception as exc:
            skipped += 1
            if skipped <= 5:
                print(f"CLIPPABLE_PROXY_SKIP name={name} err={exc}", flush=True)
            continue
        if "." not in name:
            setattr(model, name, proxy)
        else:
            parent_name, attr = name.rsplit(".", 1)
            setattr(model.get_submodule(parent_name), attr, proxy)
        replaced += 1
    print(f"CLIPPABLE_PROXY_REPLACE n={replaced} skipped={skipped}", flush=True)
    if replaced < 1:
        raise SystemExit("CLIPPABLE_PROXY_REPLACE replaced 0 modules — PEFT cannot attach")
    return replaced


def patch_peft_for_gemma4_clippable() -> None:
    """Short-circuit PEFT feature discovery before it touches missing .weight."""
    import peft.tuners.lora.layer as lora_layer
    import peft.tuners.tuners_utils as tuners_utils

    if getattr(patch_peft_for_gemma4_clippable, "_done", False):
        return

    _orig_get = tuners_utils._get_in_out_features

    def _get_in_out_features(module):  # type: ignore[no-untyped-def]
        # Prefer explicit dims — stock peft reads module.weight first and crashes on CT Linear.
        in_f = getattr(module, "in_features", None)
        out_f = getattr(module, "out_features", None)
        if in_f is not None and out_f is not None:
            try:
                return int(in_f), int(out_f)
            except Exception:
                pass
        return _orig_get(module)

    tuners_utils._get_in_out_features = _get_in_out_features
    # layer.py often binds a local import — patch that too
    if hasattr(lora_layer, "_get_in_out_features"):
        lora_layer._get_in_out_features = _get_in_out_features
    patch_peft_for_gemma4_clippable._done = True  # type: ignore[attr-defined]
    print("PEFT_PATCH_GEMMA4_CLIPPABLE in_out_shortcircuit", flush=True)


def train_gpu(cfg: dict[str, Any], data_path: Path, out_dir: Path) -> None:
    """Full DoRA/QLoRA train path (requires GPU + peft/transformers/bitsandbytes)."""
    try:
        import torch
        from datasets import Dataset
        from peft import get_peft_model, prepare_model_for_kbit_training
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            Trainer,
            TrainingArguments,
            DataCollatorForSeq2Seq,
        )
    except ImportError as exc:
        raise SystemExit(
            f"GPU train imports missing ({exc}). Use --dry-run on CPU, or install "
            "transformers peft bitsandbytes datasets accelerate."
        ) from exc

    if not torch.cuda.is_available():
        raise SystemExit("GPU train requested but torch.cuda.is_available() is False; use --dry-run")

    model_dir = resolve_model_path(cfg)
    if not (model_dir / "config.json").is_file():
        raise SystemExit(f"GPU train needs full model weights under {model_dir}")

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # W4A16-CT checkpoints already ship compressed-tensors quant metadata — do NOT
    # wrap with BitsAndBytes (conflicts with AutoQuantizationConfig).
    from transformers import AutoConfig

    hf_cfg = AutoConfig.from_pretrained(str(model_dir), trust_remote_code=True)
    qcfg = getattr(hf_cfg, "quantization_config", None)
    is_ct = False
    if qcfg is not None:
        qname = str(getattr(qcfg, "quant_method", qcfg) or "")
        is_ct = "compressed" in qname.lower() or "compressed-tensors" in str(type(qcfg)).lower()
        if isinstance(qcfg, dict):
            is_ct = is_ct or "compressed" in str(qcfg.get("quant_method", "")).lower()

    quant = None
    if (not is_ct) and cfg.get("load_in_4bit", True):
        compute = getattr(torch, str(cfg.get("bnb_4bit_compute_dtype", "bfloat16")))
        quant = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=compute,
            bnb_4bit_quant_type=str(cfg.get("bnb_4bit_quant_type", "nf4")),
            bnb_4bit_use_double_quant=bool(cfg.get("bnb_4bit_use_double_quant", True)),
        )

    attn_impl = str(cfg.get("attn_implementation") or "sdpa")
    load_kwargs: dict[str, Any] = {
        "device_map": "auto",
        "trust_remote_code": True,
        "attn_implementation": attn_impl,
    }
    if quant is not None:
        load_kwargs["quantization_config"] = quant
    if cfg.get("bf16", True):
        load_kwargs["torch_dtype"] = torch.bfloat16
    print(f"LOAD_MODEL ct={is_ct} bnb={quant is not None} path={model_dir}", flush=True)
    model = AutoModelForCausalLM.from_pretrained(str(model_dir), **load_kwargs)
    for n, m in model.named_modules():
        if type(m).__name__ == "Gemma4ClippableLinear":
            inner = getattr(m, "linear", None)
            print(
                f"CLIP_SAMPLE name={n} type={type(m).__name__} "
                f"in={getattr(m, 'in_features', None)} out={getattr(m, 'out_features', None)} "
                f"inner={type(inner).__name__ if inner is not None else None} "
                f"inner_in={getattr(inner, 'in_features', None)}",
                flush=True,
            )
            break
    # BnB kbit prep only — CT packs break under prepare_model_for_kbit_training.
    if (not is_ct) and quant is not None:
        model = prepare_model_for_kbit_training(model)
    if bool(cfg.get("gradient_checkpointing", True)):
        model.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={
                "use_reentrant": bool(cfg.get("gradient_checkpointing_use_reentrant", False))
            }
        )
        if hasattr(model, "config"):
            model.config.use_cache = False
    if is_ct or any(type(m).__name__ == "Gemma4ClippableLinear" for _, m in model.named_modules()):
        replace_gemma4_clippable_with_nn_linear_proxies(model)
        patch_peft_for_gemma4_clippable()
    peft_cfg = build_peft_config(cfg)
    model = get_peft_model(model, peft_cfg)
    try:
        model.print_trainable_parameters()
    except Exception:
        pass

    rows = load_sft_rows(data_path)
    max_seq = int(cfg["max_seq_length"])
    backend = "transformers"

    def encode_row(row: dict[str, Any]) -> dict[str, Any]:
        norm = normalize_messages_for_gemma(row["messages"])
        text = apply_chat_template(norm, model_dir)
        ids = encode_text(backend, tokenizer, text)[:max_seq]
        labels = build_labels_assistant_only(text, encode_text(backend, tokenizer, text), backend, tokenizer)[
            : len(ids)
        ]
        return {
            "input_ids": ids,
            "attention_mask": [1] * len(ids),
            "labels": labels,
        }

    ds = Dataset.from_list([encode_row(r) for r in rows])
    out_dir.mkdir(parents=True, exist_ok=True)
    args = TrainingArguments(
        output_dir=str(out_dir / "checkpoints"),
        num_train_epochs=float(cfg.get("num_train_epochs", 1)),
        per_device_train_batch_size=int(cfg.get("per_device_train_batch_size", 1)),
        gradient_accumulation_steps=int(cfg.get("gradient_accumulation_steps", 8)),
        learning_rate=float(cfg["learning_rate"]),
        lr_scheduler_type=str(cfg.get("lr_scheduler_type", "cosine")),
        warmup_ratio=float(cfg.get("warmup_ratio", 0.03)),
        weight_decay=float(cfg.get("weight_decay", 0.0)),
        max_grad_norm=float(cfg.get("max_grad_norm", 1.0)),
        logging_steps=int(cfg.get("logging_steps", 10)),
        save_steps=int(cfg.get("save_steps", 50)),
        bf16=bool(cfg.get("bf16", True)),
        gradient_checkpointing=bool(cfg.get("gradient_checkpointing", True)),
        gradient_checkpointing_kwargs={
            "use_reentrant": bool(cfg.get("gradient_checkpointing_use_reentrant", False))
        },
        report_to=[],
        seed=int(cfg.get("seed", 42)),
        remove_unused_columns=False,
    )
    collator = DataCollatorForSeq2Seq(tokenizer=tokenizer, padding=True, label_pad_token_id=IGNORE_INDEX)
    trainer = Trainer(model=model, args=args, train_dataset=ds, data_collator=collator)
    train_result = trainer.train()
    adapter_dir = out_dir
    adapter_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    losses = [float(x["loss"]) for x in trainer.state.log_history if isinstance(x, dict) and "loss" in x]
    grad_norms = [
        float(x["grad_norm"]) for x in trainer.state.log_history if isinstance(x, dict) and "grad_norm" in x
    ]
    peak_vram = None
    try:
        peak_vram = float(torch.cuda.max_memory_allocated() / (1024**3))
    except Exception:
        pass
    metrics = dict(getattr(train_result, "metrics", {}) or {})
    meta = {
        "base_model": cfg["base_model"],
        "model_path": str(model_dir),
        "peft_type": cfg["peft_type"],
        "rank": cfg["rank"],
        "lora_alpha": cfg["lora_alpha"],
        "max_seq_length": max_seq,
        "n_rows": len(rows),
        "train_loss_start": losses[0] if losses else metrics.get("train_loss"),
        "train_loss_end": losses[-1] if losses else metrics.get("train_loss"),
        "train_loss_mean": (sum(losses) / len(losses)) if losses else metrics.get("train_loss"),
        "grad_norm_last": grad_norms[-1] if grad_norms else None,
        "peak_vram_gb": peak_vram,
        "trainer_metrics": metrics,
        "plan_a": "merge via scripts/merge_adapters.py (TIES) after P5.7 serve-check",
        "plan_b": "vLLM LoRA serve of this adapter directory",
    }
    (adapter_dir / "train_qlora_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(
        f"ADAPTER_SAVED {adapter_dir} loss_start={meta['train_loss_start']} "
        f"loss_end={meta['train_loss_end']} peak_vram_gb={peak_vram}",
        flush=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="P5.6 DoRA/QLoRA SFT (agent-dev-spec §5)")
    ap.add_argument("--config", type=Path, required=True, help="configs/train_qlora.yaml")
    ap.add_argument("--data", type=Path, required=True, help="data_train/sft.jsonl")
    ap.add_argument("--out", type=Path, required=True, help="adapter output dir (agent/adapters/<name>)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="CPU-only tokenize + assistant-loss mask check (no GPU train)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = validate_config(load_config(args.config))
    data_path = args.data if args.data.is_absolute() else (_repo_root() / args.data)
    if not data_path.is_file():
        # also allow CWD-relative
        alt = Path.cwd() / args.data
        data_path = alt if alt.is_file() else data_path
    if not data_path.is_file():
        raise SystemExit(f"data not found: {args.data}")

    if args.dry_run:
        report = dry_run(cfg, data_path)
        print(json.dumps({k: report[k] for k in report if k != "per_row"}, indent=2))
        print(
            f"DRY_RUN_PASS={report['pass']} rows={report['n_formatted_ok']}/{report['n_rows']} "
            f"truncated={report['n_truncated']} active_ratio={report['label_stats']['active_ratio']}",
            flush=True,
        )
        return 0 if report["pass"] else 2

    out_dir = args.out if args.out.is_absolute() else Path.cwd() / args.out
    # Refuse accidental writes into frozen scaffold prompts; adapters/ is P5.6-owned.
    train_gpu(cfg, data_path, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
