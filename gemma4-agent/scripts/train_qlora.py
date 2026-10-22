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
import re
import statistics
import sys
from pathlib import Path
from typing import Any

# Before any torch import (direct or via peft/transformers): cut CUDA allocator
# fragmentation on 48GB CT+LoRA runs (P5.6c VRAM hot-fix).
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

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
    max_windows = int(cfg.get("max_windows_per_row", 2))
    if max_windows < 1:
        raise SystemExit(f"max_windows_per_row must be >=1, got {max_windows}")
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
            "mask_superseded_edits": bool(cfg.get("mask_superseded_edits", True)),
            "max_windows_per_row": max_windows,
            "allow_mid_edit_windows": bool(cfg.get("allow_mid_edit_windows", False)),
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


EDIT_TOOL_NAMES = frozenset({"edit_file", "write_file", "submit_patch"})
_MUTATING_EDIT_NAMES = frozenset({"edit_file", "write_file"})
_TOOL_CALL_RE = re.compile(r"<\|tool_call\>(.*?)<tool_call\|>", re.DOTALL)
_CALL_NAME_RE = re.compile(r"call:([A-Za-z0-9_]+)")
# Gemma chat-template arg encoding: filepath:<|"|>path/to/file<|"|>
_FILEPATH_TEMPLATED_RE = re.compile(r'filepath:<\|"\|>([^<]+)<\|"\|>')
_ESCAPED_DOCSTRING_RE = re.compile(r'\\"\\"\\"')


def header_char_end(text: str) -> int:
    """Char end of System + first User problem-statement turn (inclusive of <turn|>)."""
    s = text.find("<|turn>user")
    if s < 0:
        s = text.find("<|turn>system")
        if s < 0:
            return 0
        e = text.find("<turn|>", s)
        return (e + len("<turn|>")) if e >= 0 else 0
    e = text.find("<turn|>", s)
    return (e + len("<turn|>")) if e >= 0 else 0


def header_token_len(text: str, backend: str, tok: Any) -> int:
    end = header_char_end(text)
    if end <= 0:
        return 0
    return len(encode_text(backend, tok, text[:end]))


def turn_token_boundaries(text: str, backend: str, tok: Any) -> list[int]:
    """Token indices of <|turn> starts (plus 0 and len)."""
    bounds = [0]
    pos = 0
    while True:
        i = text.find("<|turn>", pos)
        if i < 0:
            break
        bounds.append(len(encode_text(backend, tok, text[:i])))
        pos = i + len("<|turn>")
    return sorted(set(bounds))


def snap_token_to_turn(tok_i: int, bounds: list[int], *, prefer: str = "floor") -> int:
    """Snap token index to a turn boundary."""
    if not bounds:
        return tok_i
    if tok_i <= bounds[0]:
        return bounds[0]
    if tok_i >= bounds[-1]:
        return bounds[-1]
    lo = bounds[0]
    for b in bounds:
        if b == tok_i:
            return b
        if b < tok_i:
            lo = b
        else:
            if prefer == "ceil":
                return b
            return lo
    return lo


def find_edit_token_spans(text: str, backend: str, tok: Any) -> list[dict[str, Any]]:
    """Token spans for assistant edit_file / write_file / submit_patch tool_calls."""
    return [
        {"name": e["name"], "start": e["tok_start"], "end": e["tok_end"]}
        for e in find_edit_calls_detailed(text, backend, tok)
    ]


def find_edit_calls_detailed(text: str, backend: str, tok: Any) -> list[dict[str, Any]]:
    """Edit/submit tool_calls with filepath + char/token spans (templated Gemma format)."""
    out: list[dict[str, Any]] = []
    for m in _TOOL_CALL_RE.finditer(text):
        nm = _CALL_NAME_RE.search(m.group(1) or "")
        if not nm or nm.group(1) not in EDIT_TOOL_NAMES:
            continue
        name = nm.group(1)
        body = m.group(1) or ""
        fp_m = _FILEPATH_TEMPLATED_RE.search(body)
        filepath = (fp_m.group(1) if fp_m else "").strip()
        tok_start = len(encode_text(backend, tok, text[: m.start()]))
        tok_end = len(encode_text(backend, tok, text[: m.end()]))
        out.append(
            {
                "name": name,
                "filepath": filepath,
                "char_start": m.start(),
                "char_end": m.end(),
                "tok_start": tok_start,
                "tok_end": tok_end,
                "has_escaped_docstring": bool(_ESCAPED_DOCSTRING_RE.search(body)),
            }
        )
    return out


def classify_edit_roles(
    edits: list[dict[str, Any]],
    *,
    window_abs_ranges: list[tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    """Mark mutating edits as final_winning or superseded; submit_patch when in-window.

    P6.5 surgical rule:
      - Only edits fully covered by head/tail/full window ranges are candidates.
      - Among window-covered mutating edits, the last per filepath is final_winning.
      - Mutating edits in the middle gap (or earlier retries for the same file) are
        superseded and must not receive CE supervision.
      - No degenerate fallback: gap-only chronological lasts stay superseded so
        ``final_winning_edits_in_window_pct`` is well-defined at 100% under the
        head+tail cap (edits that cannot fit are not counted as finals).
    """

    def _in_windows(ed: dict[str, Any]) -> bool:
        if not window_abs_ranges:
            return True
        return _window_covers_edit(
            window_abs_ranges, int(ed["tok_start"]), int(ed["tok_end"])
        )

    candidate_idxs = [
        i
        for i, ed in enumerate(edits)
        if ed["name"] in _MUTATING_EDIT_NAMES and _in_windows(ed)
    ]
    by_fp: dict[str, list[int]] = {}
    for i in candidate_idxs:
        key = edits[i].get("filepath") or f"__unknown_{i}"
        by_fp.setdefault(key, []).append(i)
    final_idx: set[int] = {idxs[-1] for idxs in by_fp.values()}

    tagged: list[dict[str, Any]] = []
    for i, ed in enumerate(edits):
        row = dict(ed)
        if ed["name"] == "submit_patch":
            in_win = _in_windows(ed)
            row["role"] = "final_winning" if in_win else "superseded"
            row["superseded"] = not in_win
        elif ed["name"] in _MUTATING_EDIT_NAMES:
            is_final = i in final_idx
            row["role"] = "final_winning" if is_final else "superseded"
            row["superseded"] = not is_final
        else:
            row["role"] = "other"
            row["superseded"] = False
        tagged.append(row)
    return tagged


def _chronological_must_cover_edits(edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Last mutating edit per filepath + every submit_patch (true trajectory winners)."""
    by_fp: dict[str, dict[str, Any]] = {}
    submits: list[dict[str, Any]] = []
    for ed in edits:
        if ed["name"] == "submit_patch":
            submits.append(ed)
        elif ed["name"] in _MUTATING_EDIT_NAMES:
            key = ed.get("filepath") or f"__unknown_{ed['tok_start']}"
            by_fp[key] = ed
    return list(by_fp.values()) + submits


def prepare_surgical_labels_and_windows(
    text: str,
    ids: list[int],
    labels: list[int],
    backend: str,
    tok: Any,
    max_seq: int,
    *,
    mask_superseded: bool = True,
    max_windows_per_row: int = 2,
    allow_mid_edit_windows: bool = False,
) -> tuple[list[int], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Classify superseded edits, mask them, then build ≤max_windows head+tail windows."""
    detailed_raw = find_edit_calls_detailed(text, backend, tok)
    # Geometry pass (unmasked) so abs_ranges match the head+tail layout.
    geom_wins = build_sft_windows(
        text,
        ids,
        labels,
        backend,
        tok,
        max_seq,
        multi_window=True,
        max_windows_per_row=max_windows_per_row,
        allow_mid_edit_windows=allow_mid_edit_windows,
    )
    win_ranges: list[tuple[int, int]] = []
    for w in geom_wins:
        win_ranges.extend(w.get("abs_ranges") or [])
    detailed = classify_edit_roles(detailed_raw, window_abs_ranges=win_ranges or None)
    mask_stats = {"superseded_spans_masked": 0, "superseded_active_tokens_masked": 0}
    labels_out = labels
    if mask_superseded:
        labels_out, mask_stats = mask_superseded_edit_labels(labels, detailed)
    windows = build_sft_windows(
        text,
        ids,
        labels_out,
        backend,
        tok,
        max_seq,
        multi_window=True,
        max_windows_per_row=max_windows_per_row,
        allow_mid_edit_windows=allow_mid_edit_windows,
    )
    return labels_out, windows, detailed, mask_stats


def mask_superseded_edit_labels(
    labels: list[int],
    edits: list[dict[str, Any]],
) -> tuple[list[int], dict[str, int]]:
    """Set labels=-100 on superseded intermediate edit_file/write_file tool_call spans."""
    out = list(labels)
    n_span = 0
    n_tok = 0
    for ed in edits:
        if not ed.get("superseded"):
            continue
        a, b = int(ed["tok_start"]), int(ed["tok_end"])
        a = max(0, min(a, len(out)))
        b = max(a, min(b, len(out)))
        if a >= b:
            continue
        n_span += 1
        for i in range(a, b):
            if out[i] != IGNORE_INDEX:
                n_tok += 1
            out[i] = IGNORE_INDEX
    return out, {"superseded_spans_masked": n_span, "superseded_active_tokens_masked": n_tok}


def count_supervised_on_spans(
    labels: list[int], spans: list[tuple[int, int]]
) -> int:
    n = 0
    for a, b in spans:
        a = max(0, min(a, len(labels)))
        b = max(a, min(b, len(labels)))
        for i in range(a, b):
            if labels[i] != IGNORE_INDEX:
                n += 1
    return n


def _window_covers_edit(abs_ranges: list[tuple[int, int]], e_start: int, e_end: int) -> bool:
    """True if edit [e_start, e_end) is fully inside the union of absolute ranges."""
    if e_end <= e_start:
        return False
    covered = 0
    pos = e_start
    while pos < e_end:
        hit = False
        for a, b in abs_ranges:
            if a <= pos < b:
                pos = min(e_end, b)
                hit = True
                break
        if not hit:
            return False
        covered = pos
    return covered >= e_end


def _concat_header_body(
    ids: list[int],
    labels: list[int],
    header_tok: int,
    body_start: int,
    body_end: int,
    max_seq: int,
) -> tuple[list[int], list[int], list[tuple[int, int]]]:
    """Build header + body[body_start:body_end], trimmed to max_seq; return abs cover ranges."""
    header_tok = max(0, min(header_tok, len(ids)))
    body_start = max(header_tok, min(body_start, len(ids)))
    body_end = max(body_start, min(body_end, len(ids)))
    body_budget = max_seq - header_tok
    if body_budget < 1:
        # Header alone overflows: keep rightmost max_seq of header (still masked).
        w_ids = ids[max(0, header_tok - max_seq) : header_tok][:max_seq]
        w_lab = labels[max(0, header_tok - max_seq) : header_tok][:max_seq]
        abs_start = max(0, header_tok - max_seq)
        return w_ids, w_lab, [(abs_start, abs_start + len(w_ids))]
    body_len = body_end - body_start
    if body_len > body_budget:
        # Prefer keeping the right end of the requested body (edit/submit often late).
        body_start = body_end - body_budget
    w_ids = ids[:header_tok] + ids[body_start:body_end]
    w_lab = labels[:header_tok] + labels[body_start:body_end]
    if len(w_ids) > max_seq:
        overflow = len(w_ids) - max_seq
        body_start += overflow
        w_ids = ids[:header_tok] + ids[body_start:body_end]
        w_lab = labels[:header_tok] + labels[body_start:body_end]
    abs_ranges = [(0, header_tok)]
    if body_start < body_end:
        abs_ranges.append((body_start, body_start + (len(w_ids) - header_tok)))
    return w_ids[:max_seq], w_lab[:max_seq], abs_ranges


def build_sft_windows(
    text: str,
    ids: list[int],
    labels: list[int],
    backend: str,
    tok: Any,
    max_seq: int,
    *,
    multi_window: bool = True,
    max_windows_per_row: int = 2,
    allow_mid_edit_windows: bool = False,
) -> list[dict[str, Any]]:
    """Turn-aligned Head+Tail windows (optional mid_edit) for edit/submit coverage.

    Default P6.5: at most ``max_windows_per_row`` windows (head+tail), no mid_edit
    thrashing windows. When multi_window=False, only header+tail (still capped).
    """
    n = len(ids)
    edits = find_edit_token_spans(text, backend, tok)
    bounds = turn_token_boundaries(text, backend, tok)
    header_tok = header_token_len(text, backend, tok)
    # Reserve at least 256 body tokens when possible.
    if header_tok >= max_seq - 256:
        header_tok = max(0, max_seq - 256)
    max_windows = max(1, int(max_windows_per_row))

    windows: list[dict[str, Any]] = []

    if n <= max_seq:
        return [
            {
                "input_ids": ids,
                "labels": labels,
                "kind": "full",
                "abs_ranges": [(0, n)],
                "n_tokens": n,
            }
        ]

    # Head localization window: prefix turn-aligned to <= max_seq
    head_end = snap_token_to_turn(max_seq, bounds + [n], prefer="floor")
    if head_end < 1:
        head_end = min(max_seq, n)
    head_end = min(head_end, max_seq, n)
    if max_windows >= 1:
        windows.append(
            {
                "input_ids": ids[:head_end],
                "labels": labels[:head_end],
                "kind": "head",
                "abs_ranges": [(0, head_end)],
                "n_tokens": head_end,
            }
        )

    # Tail: header + last body tokens (turn-aligned) — final edits + verify + submit.
    # Expand left to fully cover chronological must-cover edits that still fit in
    # the body budget with the sequence end (avoids 1–N token boundary misses).
    body_budget = max(1, max_seq - header_tok)
    raw_tail_start = max(header_tok, n - body_budget)
    detailed_for_tail = find_edit_calls_detailed(text, backend, tok)
    must_cover = _chronological_must_cover_edits(detailed_for_tail)
    expand_starts = [
        int(ed["tok_start"])
        for ed in must_cover
        if int(ed["tok_end"]) - int(ed["tok_start"]) <= body_budget
        and (n - int(ed["tok_start"])) <= body_budget
    ]
    if expand_starts:
        raw_tail_start = min([raw_tail_start] + expand_starts)
        raw_tail_start = max(header_tok, raw_tail_start)
    tail_start = snap_token_to_turn(raw_tail_start, bounds, prefer="floor")
    if tail_start < header_tok:
        tail_start = header_tok
    # If turn-snap pushed past a must-cover start, pull back to the token boundary.
    for ed in must_cover:
        es = int(ed["tok_start"])
        if es < tail_start and (n - es) <= body_budget:
            tail_start = es
    w_ids, w_lab, abs_ranges = _concat_header_body(
        ids, labels, header_tok, tail_start, n, max_seq
    )
    if max_windows >= 2 or not windows:
        windows.append(
            {
                "input_ids": w_ids,
                "labels": w_lab,
                "kind": "tail",
                "abs_ranges": abs_ranges,
                "n_tokens": len(w_ids),
            }
        )
    elif max_windows == 1:
        # Prefer tail-only when capped at 1 (surgical final-edit focus).
        windows = [
            {
                "input_ids": w_ids,
                "labels": w_lab,
                "kind": "tail",
                "abs_ranges": abs_ranges,
                "n_tokens": len(w_ids),
            }
        ]

    def covered_by_existing(e_start: int, e_end: int) -> bool:
        for w in windows:
            if _window_covers_edit(w["abs_ranges"], e_start, e_end):
                return True
        return False

    # mid_edit windows: cover edits missed by head+tail. When enabled, drop the
    # head window first so remaining slots (under max_windows_per_row) go to
    # mid_edit coverage while keeping tail for final-winning edits.
    if multi_window and allow_mid_edit_windows:
        windows = [w for w in windows if w.get("kind") != "head"]
        uncovered = [
            ed
            for ed in edits
            if not covered_by_existing(int(ed["start"]), int(ed["end"]))
        ]
        for ed in uncovered:
            if len(windows) >= max_windows:
                break
            es, ee = int(ed["start"]), int(ed["end"])
            if covered_by_existing(es, ee):
                continue
            abs_end = min(n, max(ee, es + 1))
            abs_start = max(header_tok, abs_end - body_budget)
            if abs_start > es:
                local_header = max(0, max_seq - (ee - es))
                w_ids, w_lab, abs_ranges = _concat_header_body(
                    ids, labels, local_header, es, ee, max_seq
                )
            else:
                abs_start = snap_token_to_turn(abs_start, bounds, prefer="floor")
                if abs_start > es:
                    abs_start = max(header_tok, es)
                w_ids, w_lab, abs_ranges = _concat_header_body(
                    ids, labels, header_tok, abs_start, abs_end, max_seq
                )
            if not _window_covers_edit(abs_ranges, es, ee):
                force_budget = max_seq
                force_start = max(0, ee - force_budget)
                w_ids = ids[force_start:ee][:max_seq]
                w_lab = labels[force_start:ee][:max_seq]
                abs_ranges = [(force_start, force_start + len(w_ids))]
            # Skip useless mid windows that still don't cover the target edit.
            if not _window_covers_edit(abs_ranges, es, ee):
                continue
            windows.append(
                {
                    "input_ids": w_ids,
                    "labels": w_lab,
                    "kind": "mid_edit",
                    "edit_name": ed["name"],
                    "abs_ranges": abs_ranges,
                    "n_tokens": len(w_ids),
                }
            )

    return windows[:max_windows]


def edit_coverage_for_windows(
    edits: list[dict[str, Any]], windows: list[dict[str, Any]]
) -> dict[str, Any]:
    in_win = 0
    trunc = 0
    partial = 0
    for ed in edits:
        es, ee = int(ed["start"]), int(ed["end"])
        if any(_window_covers_edit(w["abs_ranges"], es, ee) for w in windows):
            in_win += 1
            continue
        # Partial if any window overlaps the edit span
        overlapped = False
        for w in windows:
            for a, b in w["abs_ranges"]:
                if a < ee and b > es:
                    overlapped = True
                    break
            if overlapped:
                break
        if overlapped:
            partial += 1
        trunc += 1
    total = len(edits)
    return {
        "edit_total": total,
        "edit_fully_in_window": in_win,
        "edit_truncated_beyond": trunc,
        "edit_partial_at_boundary": partial,
        "edit_in_window_pct": round(100.0 * in_win / total, 2) if total else 100.0,
        "edit_truncated_pct": round(100.0 * trunc / total, 2) if total else 0.0,
        "n_windows": len(windows),
    }


def build_grpo_windows(
    text: str,
    ids: list[int],
    labels: list[int],
    backend: str,
    tok: Any,
    max_seq: int,
    *,
    max_windows_per_row: int = 2,
    allow_mid_edit_windows: bool = False,
) -> list[dict[str, Any]]:
    """GRPO windows: default head+tail only (P6.5); optional mid_edit if enabled."""
    return build_sft_windows(
        text,
        ids,
        labels,
        backend,
        tok,
        max_seq,
        multi_window=True,
        max_windows_per_row=max_windows_per_row,
        allow_mid_edit_windows=allow_mid_edit_windows,
    )


def build_grpo_header_tail_window(
    text: str,
    ids: list[int],
    labels: list[int],
    backend: str,
    tok: Any,
    max_seq: int,
) -> dict[str, Any]:
    """Backward-compat: return the primary header+tail (or full) window only."""
    wins = build_sft_windows(
        text,
        ids,
        labels,
        backend,
        tok,
        max_seq,
        multi_window=False,
        max_windows_per_row=2,
        allow_mid_edit_windows=False,
    )
    for w in reversed(wins):
        if w["kind"] in {"full", "tail"}:
            return w
    return wins[0]


def save_peft_adapter_bf16(model: Any, out_dir: Path) -> dict[str, Any]:
    """Cast LoRA/DoRA adapter weights to bfloat16 before save (~0.95GB vs ~1.9GB)."""
    import torch

    cast = 0
    for name, p in model.named_parameters():
        if "lora_" in name or "lora_A" in name or "lora_B" in name or "lora_magnitude" in name:
            if p.data.dtype != torch.bfloat16:
                p.data = p.data.to(dtype=torch.bfloat16)
                cast += 1
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out_dir))
    weight_path = out_dir / "adapter_model.safetensors"
    size_b = weight_path.stat().st_size if weight_path.is_file() else 0
    return {
        "adapter_dtype": "bfloat16",
        "n_params_cast": cast,
        "adapter_bytes": size_b,
        "adapter_gib": round(size_b / (1024**3), 4),
    }


def dry_run(cfg: dict[str, Any], data_path: Path) -> dict[str, Any]:
    model_dir = resolve_model_path(cfg)
    backend, tok = load_tokenizer_backend(model_dir)
    rows = load_sft_rows(data_path)
    max_seq = int(cfg["max_seq_length"])
    per_row: list[dict[str, Any]] = []
    n_trunc = 0
    n_ok = 0
    n_windows = 0
    window_lens: list[int] = []
    supervised_token_count = 0
    total_token_count = 0
    lengths: list[int] = []
    mask_issues: list[str] = []
    leak_tool_response_tokens = 0
    supervised_in_tool_call_tokens = 0
    source_role_chars: dict[str, int] = {"system": 0, "user": 0, "assistant": 0, "tool": 0}
    audit_samples: list[dict[str, Any]] = []
    edit_total = 0
    edit_in = 0
    edit_trunc = 0
    final_winning_total = 0
    final_winning_in = 0
    superseded_total = 0
    superseded_supervised = 0
    masked_superseded_tokens = 0
    n_mid_edit_windows = 0
    n_rows_with_escaped_docstring = 0
    max_windows = int(cfg.get("max_windows_per_row", 2))
    allow_mid = bool(cfg.get("allow_mid_edit_windows", False))
    do_mask_super = bool(cfg.get("mask_superseded_edits", True))

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
        labels_full = build_labels_assistant_only(text, ids_full, backend, tok)
        labels_full, windows, detailed, mask_stats = prepare_surgical_labels_and_windows(
            text,
            ids_full,
            labels_full,
            backend,
            tok,
            max_seq,
            mask_superseded=do_mask_super,
            max_windows_per_row=max_windows,
            allow_mid_edit_windows=allow_mid,
        )
        if any(e.get("has_escaped_docstring") for e in detailed):
            n_rows_with_escaped_docstring += 1
        masked_superseded_tokens += int(mask_stats["superseded_active_tokens_masked"])
        leak = classify_supervised_leakage(text, ids_full, labels_full, backend, tok)
        leak_tool_response_tokens += int(leak["supervised_in_tool_response_blocks"])
        supervised_in_tool_call_tokens += int(leak["supervised_in_tool_call_blocks"])
        if leak["tool_response_leak"]:
            mask_issues.append(
                f"{row['task_id']}: tool_response_leak_tokens={leak['supervised_in_tool_response_blocks']}"
            )
        if any(w.get("kind") == "mid_edit" for w in windows):
            n_mid_edit_windows += sum(1 for w in windows if w.get("kind") == "mid_edit")
        if len(windows) > max_windows:
            mask_issues.append(
                f"{row['task_id']}: windows_exceed_cap n={len(windows)} cap={max_windows}"
            )
        edits = [{"name": e["name"], "start": e["tok_start"], "end": e["tok_end"]} for e in detailed]
        cov = edit_coverage_for_windows(edits, windows)
        edit_total += cov["edit_total"]
        edit_in += cov["edit_fully_in_window"]
        edit_trunc += cov["edit_truncated_beyond"]
        finals = [e for e in detailed if e.get("role") == "final_winning"]
        supers = [e for e in detailed if e.get("superseded")]
        final_spans = [(int(e["tok_start"]), int(e["tok_end"])) for e in finals]
        super_spans = [(int(e["tok_start"]), int(e["tok_end"])) for e in supers]
        final_cov = edit_coverage_for_windows(
            [{"name": e["name"], "start": e["tok_start"], "end": e["tok_end"]} for e in finals],
            windows,
        )
        final_winning_total += len(finals)
        final_winning_in += int(final_cov["edit_fully_in_window"])
        superseded_total += len(supers)
        superseded_supervised += count_supervised_on_spans(labels_full, super_spans)
        n_windows += len(windows)
        for w in windows:
            if len(w["input_ids"]) > max_seq:
                mask_issues.append(
                    f"{row['task_id']}: window_overflow kind={w['kind']} n={len(w['input_ids'])}"
                )
            n_active = sum(1 for x in w["labels"] if x != IGNORE_INDEX)
            supervised_token_count += n_active
            total_token_count += len(w["input_ids"])
            window_lens.append(len(w["input_ids"]))
            # Window labels must not unmask tool_response
            w_leak = classify_supervised_leakage(
                text, w["input_ids"], w["labels"], backend, tok
            )
            # Note: classify uses full text offsets against window ids — only trust
            # tool_response_leak on full-seq labels; re-check via abs span below.
            _ = w_leak
        # Assistant tool-call JSON / template tool_call markers must have some supervised tokens
        n_active_full = sum(1 for x in labels_full if x != IGNORE_INDEX)
        if "<|tool_call>" in text or "tool_calls" in json.dumps(row["messages"]):
            if n_active_full == 0:
                mask_issues.append(f"{row['task_id']}: no_supervised_tokens")
        n_ok += 1
        row_rep = {
            "task_id": row["task_id"],
            "n_messages": len(row["messages"]),
            "n_tokens": n,
            "n_windows": len(windows),
            "window_kinds": [w["kind"] for w in windows],
            "n_supervised_full": n_active_full,
            "truncated_raw": truncated,
            "edit_coverage": cov,
            "final_winning_edits": len(finals),
            "final_winning_in_window": int(final_cov["edit_fully_in_window"]),
            "superseded_edits": len(supers),
            "superseded_supervised_tokens": count_supervised_on_spans(labels_full, super_spans),
            "mask_superseded_stats": mask_stats,
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

    edit_in_pct = round(100.0 * edit_in / edit_total, 2) if edit_total else 100.0
    final_in_pct = (
        round(100.0 * final_winning_in / final_winning_total, 2) if final_winning_total else 100.0
    )
    ga = int(cfg.get("gradient_accumulation_steps", 8))
    epochs = float(cfg.get("num_train_epochs", 1))
    approx_opt_steps = max(1, int((n_windows / max(ga, 1)) * epochs))
    windowing_name = (
        "turn_aligned_head_tail"
        if not allow_mid
        else "turn_aligned_head_tail_mid_edit"
    )
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
        "mask_superseded_edits": do_mask_super,
        "max_windows_per_row": max_windows,
        "allow_mid_edit_windows": allow_mid,
        "n_rows": len(rows),
        "n_tasks": len({r.get("task_id") for r in rows}),
        "n_formatted_ok": n_ok,
        "n_truncated_raw": n_trunc,
        "n_windows": n_windows,
        "n_mid_edit_windows": n_mid_edit_windows,
        "approx_optimizer_steps": approx_opt_steps,
        "num_train_epochs": epochs,
        "gradient_accumulation_steps": ga,
        "windowing": windowing_name,
        "edit_coverage": {
            "edit_total": edit_total,
            "edit_fully_in_window": edit_in,
            "edit_truncated_beyond": edit_trunc,
            "edit_in_window_pct": edit_in_pct,
            "edit_truncated_pct": round(100.0 * edit_trunc / edit_total, 2) if edit_total else 0.0,
        },
        "surgical_edit_coverage": {
            "final_winning_edits_total": final_winning_total,
            "final_winning_edits_in_window": final_winning_in,
            "final_winning_edits_in_window_pct": final_in_pct,
            "superseded_edits_total": superseded_total,
            "superseded_failed_edits_supervised": superseded_supervised,
            "superseded_active_tokens_masked": masked_superseded_tokens,
            "n_rows_with_escaped_docstring": n_rows_with_escaped_docstring,
        },
        "mask_issues": mask_issues,
        "token_stats": {
            "min": int(min(lengths)) if lengths else 0,
            "median": int(statistics.median(lengths)) if lengths else 0,
            "p95": pct(lengths, 95) if lengths else 0,
            "max": int(max(lengths)) if lengths else 0,
            "mean": round(sum(lengths) / len(lengths), 1) if lengths else 0,
        },
        "window_token_stats": {
            "min": int(min(window_lens)) if window_lens else 0,
            "median": int(statistics.median(window_lens)) if window_lens else 0,
            "max": int(max(window_lens)) if window_lens else 0,
            "mean": round(sum(window_lens) / len(window_lens), 1) if window_lens else 0,
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
        # P6.5 default: no mid-edit windows + no supervised superseded tokens.
        # P6.10 strict_le3 (allow_mid_edit_windows=true, mask_superseded=false):
        # require full edit_in_window coverage; mid windows and unmasked
        # precursor edits in already-clean ≤3-edit histories are expected.
        "pass": (
            n_ok == len(rows)
            and not mask_issues
            and leak_tool_response_tokens == 0
            and (not window_lens or max(window_lens) <= max_seq)
            and (
                (
                    allow_mid
                    and edit_in_pct >= 100.0
                    and final_in_pct >= 100.0
                )
                or (
                    (not allow_mid)
                    and final_in_pct >= 100.0
                    and superseded_supervised == 0
                    and n_mid_edit_windows == 0
                )
            )
        ),
        "per_row": per_row,
        "adapter_plan": {
            "plan_a": "merge_adapters.py TIES → agent/adapters/master_swe_adapter after P5.7",
            "plan_b": "vLLM LoRA load of --out beside gemma-4-31b-it-qat-w4a16-ct",
        },
        "adapter_save_dtype": "bfloat16",
    }
    return report


def _is_vision_module_name(name: str) -> bool:
    n = name.lower()
    return any(
        tok in n
        for tok in (
            "vision_tower",
            "vision_model",
            "multi_modal",
            "vision_projector",
            "mm_projector",
        )
    )


def collect_lm_peft_target_names(model: Any, leaf_names: list[str]) -> list[str]:
    """Full module paths for LM leaf targets only (never vision_*)."""
    leaves = set(leaf_names)
    out: list[str] = []
    for name, _mod in model.named_modules():
        if _is_vision_module_name(name):
            continue
        leaf = name.rsplit(".", 1)[-1]
        if leaf in leaves:
            out.append(name)
    return out


def build_peft_config(
    cfg: dict[str, Any],
    target_modules: list[str] | None = None,
    *,
    force_lora: bool = False,
):
    import inspect

    from peft import LoraConfig, TaskType

    use_dora = (cfg["peft_type"] == "dora") and (not force_lora)
    if force_lora and cfg.get("peft_type") == "dora":
        print(
            "CT_FORCE_LORA reason=pack_quantized_meta_proxy_no_dense_magnitude",
            flush=True,
        )
    targets = list(target_modules) if target_modules is not None else list(cfg["target_modules"])
    if not targets:
        raise SystemExit("PEFT target_modules empty after LM filter — refusing vision-only match")
    kwargs: dict[str, Any] = {
        "r": int(cfg["rank"]),
        "lora_alpha": int(cfg["lora_alpha"]),
        "lora_dropout": float(cfg.get("lora_dropout", 0.05)),
        "target_modules": targets,
        "bias": "none",
        "task_type": TaskType.CAUSAL_LM,
        "use_dora": use_dora,
    }
    sig = inspect.signature(LoraConfig.__init__)
    return LoraConfig(**{k: v for k, v in kwargs.items() if k in sig.parameters})


def install_chunked_causal_lm_loss(model: Any, chunk_size: int = 256) -> None:
    """Avoid vocab×seq float32 logits materialization (262k×12k×4 ≈ 12GiB).

    Stock ForCausalLMLoss does ``logits.float()`` on the full tensor; on 48GB that
    OOMs after a successful CT forward. Chunk along the sequence axis instead.
    """
    import torch
    import torch.nn.functional as F

    def _chunked_loss(
        logits: Any,
        labels: Any,
        vocab_size: int | None = None,
        **kwargs: Any,
    ) -> Any:
        del vocab_size  # unused; kept for HF signature compatibility
        ignore_index = int(kwargs.get("ignore_index", IGNORE_INDEX))
        # Align with causal LM shift.
        shift_logits = logits[..., :-1, :].contiguous()
        shift_labels = labels[..., 1:].contiguous()
        flat_logits = shift_logits.reshape(-1, shift_logits.size(-1))
        flat_labels = shift_labels.reshape(-1)
        # Keep loss on the autograd graph even when a collated row has zero
        # supervised tokens (assistant-only mask can yield empty labels).
        total = flat_logits.sum() * 0.0
        ntok = 0
        step = max(1, int(chunk_size))
        for start in range(0, flat_logits.size(0), step):
            end = min(start + step, flat_logits.size(0))
            chunk_labels = flat_labels[start:end]
            # Skip unsupervised chunks entirely — do not materialize float32
            # softmax/logits for tokens with labels == -100 (system/user/tool_response).
            if int((chunk_labels != ignore_index).sum().item()) == 0:
                continue
            valid = chunk_labels != ignore_index
            chunk_logits = flat_logits[start:end][valid].float()
            chunk_tgt = chunk_labels[valid]
            total = total + F.cross_entropy(chunk_logits, chunk_tgt, reduction="sum")
            ntok += int(chunk_tgt.numel())
            del chunk_logits, chunk_tgt, chunk_labels, valid
        if ntok == 0:
            return total
        return total / ntok

    model.loss_function = _chunked_loss  # type: ignore[method-assign]
    print(f"CHUNKED_CE_LOSS installed chunk_size={chunk_size}", flush=True)


def patch_ct_pack_quantized_forward(model: Any) -> int:
    """Make pack-quantized CT Linears runnable without full-model decompress.

    transformers/compressed-tensors≥0.17 registers a first-forward hook that
    decompresses *all* packs into dense bf16 (~62GB for 31B) — OOM on 48GB.
    Stock quantized_forward also assumes ``self.weight`` exists, which
    COMPRESSED packs do not have (only weight_packed/scale/shape).

    Fix: remove the decompress-all hook and replace per-module forward with
    on-the-fly unpack→dequant→F.linear (peak ≈ packed footprint + one layer).
    """
    import torch
    import torch.nn.functional as F

    from compressed_tensors.compressors import ModelCompressor, PackedQuantizationCompressor
    from compressed_tensors.quantization import QuantizationStatus
    from compressed_tensors.utils.module import get_direct_state_dict

    try:
        mc = ModelCompressor.from_pretrained_model(model)
        mc.remove_decompression_hook(model)
        print("CT_DECOMPRESS_HOOK_REMOVED", flush=True)
    except Exception as exc:
        print(f"CT_DECOMPRESS_HOOK_REMOVE_SKIP err={exc}", flush=True)
    hook = getattr(model, "ct_decompress_hook", None)
    if hook is not None:
        try:
            hook.remove()
            print("CT_DECOMPRESS_HOOK_ATTR_REMOVED", flush=True)
        except Exception as exc:
            print(f"CT_DECOMPRESS_HOOK_ATTR_SKIP err={exc}", flush=True)
        try:
            delattr(model, "ct_decompress_hook")
        except Exception:
            pass

    patched = 0
    for _name, mod in model.named_modules():
        params = getattr(mod, "_parameters", None)
        if params is None or "weight_packed" not in params:
            continue
        if not hasattr(mod, "quantization_scheme"):
            continue
        scheme = mod.quantization_scheme

        def _make_fwd(scheme_ref: Any):
            def fwd(self, x):  # type: ignore[no-untyped-def]
                status = getattr(self, "quantization_status", None)
                if (
                    status is not None
                    and status >= QuantizationStatus.COMPRESSED
                    and "weight_packed" in self._parameters
                    and "weight" not in self._parameters
                ):
                    sd = get_direct_state_dict(self)
                    dec = PackedQuantizationCompressor.decompress(sd, scheme_ref)
                    w = dec["weight"]
                    if not isinstance(w, torch.Tensor):
                        w = w.data if hasattr(w, "data") else torch.as_tensor(w)
                    w = w.to(dtype=x.dtype, device=x.device)
                    # w is a non-leaf decompress buffer (not an nn.Parameter); freeze it
                    # explicitly so autograd only tracks x (and LoRA on the PEFT wrapper).
                    return F.linear(x, w.requires_grad_(False), self.bias)
                return torch.nn.Linear.forward(self, x)

            return fwd

        mod.forward = _make_fwd(scheme).__get__(mod, type(mod))
        patched += 1
    print(f"CT_PACK_OTF_FORWARD n={patched}", flush=True)
    if patched < 1:
        raise SystemExit("CT_PACK_OTF_FORWARD patched 0 modules — CT forward will fail")
    return patched


def replace_gemma4_clippable_with_nn_linear_proxies(model: Any) -> int:
    """Swap Gemma4ClippableLinear / weightless CT Linear → nn.Linear proxies for PEFT.

    PEFT only accepts torch.nn.Linear. W4A16-CT Gemma-4 LM layers are pack-quantized
    Linears without a dense ``weight`` Parameter; proxies subclass nn.Linear, keep
    CT forward (see patch_ct_pack_quantized_forward), and expose a meta weight for
    LoRA init. DoRA needs real magnitudes — CT path forces LoRA (see build_peft_config).
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
            # Meta weight only — allocating full dense (out,in) for ~410 LM layers
            # OOMs host RAM on 48GB pods. Forward uses CT clip_mod; Trainer must
            # NOT .to() these (see MODEL_TO_NOOP / TRAINER_SKIP_MOVE).
            nn.Module.__init__(self)
            self.in_features = int(in_f)
            self.out_features = int(out_f)
            self.bias = None
            self.register_parameter(
                "weight",
                nn.Parameter(
                    torch.empty((out_f, in_f), device="meta", dtype=torch.bfloat16),
                    requires_grad=False,
                ),
            )
            object.__setattr__(self, "_clip_mod", clip_mod)

        def forward(self, x: Any) -> Any:  # type: ignore[override]
            return object.__getattribute__(self, "_clip_mod")(x)

        def to(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            # Keep meta weight on meta; never materialize.
            return self

        def cuda(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            return self

    def _needs_proxy(module: Any) -> bool:
        name = type(module).__name__
        if name == "_ClippableLinearProxy":
            return False
        # Only wrap Gemma4ClippableLinear. Pack-quantized CT `nn.Linear` already
        # subclasses Linear (PEFT-injectable) once OTF forward is patched; wrapping
        # those with a meta-weight proxy poisons autograd (MmBackward meta vs cuda).
        return name == "Gemma4ClippableLinear"

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
        # Text SFT: skip vision tower (same leaf names would waste VRAM + OOM proxies).
        if "vision_tower" in name or "vision_model" in name or "multi_modal" in name:
            continue
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

    # Idempotent resume: adapter already on disk (e.g. prior run died after save).
    adapter_cfg = out_dir / "adapter_config.json"
    adapter_weights = out_dir / "adapter_model.safetensors"
    if adapter_cfg.is_file() and adapter_weights.is_file():
        meta_path = out_dir / "train_qlora_meta.json"
        if not meta_path.is_file():
            meta_path.write_text(
                json.dumps(
                    {
                        "base_model": cfg.get("base_model"),
                        "peft_type": cfg.get("peft_type"),
                        "rank": cfg.get("rank"),
                        "max_seq_length": cfg.get("max_seq_length"),
                        "resume": "skipped_existing_adapter",
                        "adapter_bytes": adapter_weights.stat().st_size,
                    },
                    indent=2,
                )
                + "\n"
            )
        print(f"SFT_SKIP_EXISTING {out_dir} bytes={adapter_weights.stat().st_size}", flush=True)
        return

    model_dir = resolve_model_path(cfg)
    if not (model_dir / "config.json").is_file():
        raise SystemExit(f"GPU train needs full model weights under {model_dir}")

    # Gemma4 tokenizer_config may set extra_special_tokens as a list; some
    # transformers builds expect a dict. Prefer use_fast + sanitize on failure.
    try:
        tokenizer = AutoTokenizer.from_pretrained(str(model_dir), trust_remote_code=True)
    except AttributeError as exc:
        if "keys" not in str(exc):
            raise
        # Use module-level json (do NOT local-import — shadows json for whole train_gpu).
        cfg_path = model_dir / "tokenizer_config.json"
        raw = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
        est = raw.get("extra_special_tokens")
        if isinstance(est, list):
            raw["extra_special_tokens"] = {f"extra_{i}": t for i, t in enumerate(est) if isinstance(t, str)}
            cfg_path.write_text(json.dumps(raw, indent=2) + "\n")
            print("TOKENIZER_CFG patched extra_special_tokens list→dict", flush=True)
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

    # Text-only SFT: drop multimodal towers so PEFT/Trainer have VRAM headroom.
    def _drop_vision(root: Any, label: str) -> int:
        dropped = 0
        for attr in (
            "vision_tower",
            "vision_model",
            "multi_modal_projector",
            "mm_projector",
            "vision_projector",
        ):
            if hasattr(root, attr) and getattr(root, attr) is not None:
                try:
                    setattr(root, attr, None)
                    dropped += 1
                    print(f"DROP_VISION {label}.{attr}", flush=True)
                except Exception as exc:
                    print(f"DROP_VISION_FAIL {label}.{attr} err={exc}", flush=True)
        return dropped

    n_drop = _drop_vision(model, "model")
    if hasattr(model, "model"):
        n_drop += _drop_vision(model.model, "model.model")
    if hasattr(model, "vision_tower"):
        n_drop += _drop_vision(model, "top")
    try:
        import torch

        torch.cuda.empty_cache()
    except Exception:
        pass
    print(f"DROP_VISION_TOTAL n={n_drop}", flush=True)

    if is_ct:
        # Must run before first forward / PEFT: strip decompress-all hook + OTF pack matmul.
        patch_ct_pack_quantized_forward(model)

    # Large-vocab CE: always chunk (safe on 80GB too; required on 48GB at long seq).
    install_chunked_causal_lm_loss(
        model, chunk_size=int(cfg.get("ce_loss_chunk_size", 256))
    )

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
    n_proxy = 0
    if is_ct or any(type(m).__name__ == "Gemma4ClippableLinear" for _, m in model.named_modules()):
        n_proxy = replace_gemma4_clippable_with_nn_linear_proxies(model)
        patch_peft_for_gemma4_clippable()
    if (not is_ct) and n_proxy < 1 and any(
        type(m).__name__ == "Gemma4ClippableLinear" for _, m in model.named_modules()
    ):
        raise SystemExit("CLIPPABLE_PROXY_REPLACE replaced 0 modules — PEFT cannot attach")
    lm_targets = collect_lm_peft_target_names(model, list(cfg["target_modules"]))
    print(f"PEFT_LM_TARGETS n={len(lm_targets)} sample={lm_targets[:3]}", flush=True)
    if len(lm_targets) < 8:
        raise SystemExit(f"PEFT_LM_TARGETS too few ({len(lm_targets)}); check module naming")
    # DoRA needs dense base weight magnitudes; pack-quantized CT has none until decompress.
    peft_cfg = build_peft_config(cfg, target_modules=lm_targets, force_lora=bool(is_ct))
    # Preserve accelerate device_map across PEFT wrap so Trainer skips model.to().
    pre_dmap = getattr(model, "hf_device_map", None)
    model = get_peft_model(model, peft_cfg)
    if pre_dmap is not None:
        model.hf_device_map = pre_dmap
        base = model.get_base_model() if hasattr(model, "get_base_model") else None
        if base is not None and getattr(base, "hf_device_map", None) is None:
            try:
                base.hf_device_map = pre_dmap
            except Exception:
                pass
    # CT W4A16 + adapters are already device-placed. Never let Trainer/Accelerate
    # call model.to(cuda) — that either OOMs (CPU proxy densess) or breaks meta.
    try:
        model.is_loaded_in_4bit = True
    except Exception:
        pass

    def _trainer_skip_move(self, model, device):  # type: ignore[no-untyped-def]
        print(f"TRAINER_SKIP_MOVE device={device}", flush=True)
        return model

    Trainer._move_model_to_device = _trainer_skip_move  # type: ignore[method-assign]

    def _noop_to(*_a: Any, **_k: Any) -> Any:
        return model

    # Belt-and-suspenders: neutralize .to/.cuda on the PeftModel itself.
    model.to = _noop_to  # type: ignore[method-assign]
    model.cuda = _noop_to  # type: ignore[method-assign]
    try:
        from accelerate import Accelerator

        _orig_prep = Accelerator.prepare_model

        def _prep_no_move(self, model, *args, **kwargs):  # type: ignore[no-untyped-def]
            print("ACCEL_SKIP_PREPARE_MODEL", flush=True)
            return model

        Accelerator.prepare_model = _prep_no_move  # type: ignore[method-assign]
        print("ACCEL_PREPARE_NOOP installed", flush=True)
    except Exception as exc:
        print(f"ACCEL_PREPARE_NOOP_SKIP err={exc}", flush=True)
    print("MODEL_TO_NOOP installed", flush=True)

    # Keep proxy weights on CPU (not meta). Forward uses _clip_mod on GPU; .to is no-op.
    try:
        model.print_trainable_parameters()
    except Exception:
        pass

    rows = load_sft_rows(data_path)
    max_seq = int(cfg["max_seq_length"])
    backend = "transformers"
    max_windows = int(cfg.get("max_windows_per_row", 2))
    allow_mid = bool(cfg.get("allow_mid_edit_windows", False))
    do_mask_super = bool(cfg.get("mask_superseded_edits", True))

    encoded: list[dict[str, Any]] = []
    for row in rows:
        norm = normalize_messages_for_gemma(row["messages"])
        text = apply_chat_template(norm, model_dir)
        ids_full = encode_text(backend, tokenizer, text)
        labels_full = build_labels_assistant_only(text, ids_full, backend, tokenizer)
        labels_full, windows, _detailed, _mask_stats = prepare_surgical_labels_and_windows(
            text,
            ids_full,
            labels_full,
            backend,
            tokenizer,
            max_seq,
            mask_superseded=do_mask_super,
            max_windows_per_row=max_windows,
            allow_mid_edit_windows=allow_mid,
        )
        for w in windows:
            ids = w["input_ids"]
            labels = w["labels"]
            encoded.append(
                {
                    "input_ids": ids,
                    "attention_mask": [1] * len(ids),
                    "labels": labels,
                }
            )
    print(
        f"SFT_WINDOWS rows={len(rows)} windows={len(encoded)} max_seq={max_seq} "
        f"max_windows={max_windows} mask_superseded={int(do_mask_super)} mid_edit={int(allow_mid)}",
        flush=True,
    )

    ds = Dataset.from_list(encoded)
    out_dir.mkdir(parents=True, exist_ok=True)
    import inspect

    ga = int(cfg.get("gradient_accumulation_steps", 8))
    epochs = float(cfg.get("num_train_epochs", 1))
    approx_steps = max(1, int((len(encoded) / max(ga, 1)) * epochs))
    ta_kwargs: dict[str, Any] = {
        "output_dir": str(out_dir / "checkpoints"),
        "num_train_epochs": epochs,
        "per_device_train_batch_size": int(cfg.get("per_device_train_batch_size", 1)),
        "gradient_accumulation_steps": ga,
        "learning_rate": float(cfg["learning_rate"]),
        "lr_scheduler_type": str(cfg.get("lr_scheduler_type", "cosine")),
        "warmup_ratio": float(cfg.get("warmup_ratio", 0.03)),
        "warmup_steps": max(1, int(float(cfg.get("warmup_ratio", 0.03)) * approx_steps)),
        "weight_decay": float(cfg.get("weight_decay", 0.0)),
        "max_grad_norm": float(cfg.get("max_grad_norm", 1.0)),
        "logging_steps": int(cfg.get("logging_steps", 10)),
        "save_steps": int(cfg.get("save_steps", 50)),
        "bf16": bool(cfg.get("bf16", True)),
        "gradient_checkpointing": bool(cfg.get("gradient_checkpointing", True)),
        "gradient_checkpointing_kwargs": {
            "use_reentrant": bool(cfg.get("gradient_checkpointing_use_reentrant", False))
        },
        "report_to": [],
        "seed": int(cfg.get("seed", 42)),
        "remove_unused_columns": False,
        "optim": str(cfg.get("optim", "adamw_torch")),
    }
    # Prefer expandable segments on tight 48GB CT+LoRA runs.
    try:
        import os

        os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    except Exception:
        pass
    # Transformers versions differ (warmup_ratio removed in some builds).
    sig = inspect.signature(TrainingArguments.__init__)
    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if not accepts_var_kw:
        ta_kwargs = {k: v for k, v in ta_kwargs.items() if k in sig.parameters}
    if "warmup_ratio" not in ta_kwargs and "warmup_steps" not in ta_kwargs:
        # last resort: no warmup knobs
        pass
    elif "warmup_ratio" in ta_kwargs and "warmup_steps" in ta_kwargs:
        # Prefer ratio when supported; else steps-only.
        if "warmup_ratio" in sig.parameters or accepts_var_kw:
            ta_kwargs.pop("warmup_steps", None)
        else:
            ta_kwargs.pop("warmup_ratio", None)
    print(f"TRAINING_ARGS keys={sorted(ta_kwargs)}", flush=True)
    args = TrainingArguments(**ta_kwargs)
    collator = DataCollatorForSeq2Seq(tokenizer=tokenizer, padding=True, label_pad_token_id=IGNORE_INDEX)
    trainer = Trainer(model=model, args=args, train_dataset=ds, data_collator=collator)
    train_result = trainer.train()
    adapter_dir = out_dir
    adapter_dir.mkdir(parents=True, exist_ok=True)
    save_meta = save_peft_adapter_bf16(model, adapter_dir)
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
        "n_windows": len(encoded),
        "windowing": (
            "turn_aligned_head_tail"
            if not bool(cfg.get("allow_mid_edit_windows", False))
            else "turn_aligned_head_tail_mid_edit"
        ),
        "mask_superseded_edits": bool(cfg.get("mask_superseded_edits", True)),
        "max_windows_per_row": int(cfg.get("max_windows_per_row", 2)),
        "adapter_save": save_meta,
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
        cov = report.get("edit_coverage") or {}
        print(
            f"DRY_RUN_PASS={report['pass']} rows={report['n_formatted_ok']}/{report['n_rows']} "
            f"windows={report.get('n_windows')} raw_trunc={report.get('n_truncated_raw')} "
            f"edit_in_window_pct={cov.get('edit_in_window_pct')} "
            f"tool_response_leak={report['label_stats']['tool_response_leak']} "
            f"approx_steps={report.get('approx_optimizer_steps')} "
            f"active_ratio={report['label_stats']['active_ratio']}",
            flush=True,
        )
        return 0 if report["pass"] else 2

    out_dir = args.out if args.out.is_absolute() else Path.cwd() / args.out
    # Refuse accidental writes into frozen scaffold prompts; adapters/ is P5.6-owned.
    train_gpu(cfg, data_path, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
