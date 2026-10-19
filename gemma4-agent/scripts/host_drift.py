#!/usr/bin/env python3
"""Host-oracle drift helpers for A40 subprocess vs soundness Docker.

Used by rescue_drift_keepers.py and Wave2 rejection_sample drift immunity.

Hard rules:
  - NEVER treat FAIL_TO_PASS / test_patch-added tests as ignorable host drift.
  - If gold itself fails a FAIL_TO_PASS on this host, refuse drift rescue for
    that task (oracle is broken — usually missing PYTHONPATH=src or bad deps).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Node ids may contain spaces inside parametrize brackets (requests_7205).
# Capture rest of line; strip optional " - <reason>" suffix.
FAILED_NODE_RE = re.compile(
    r"^FAILED\s+(.+?)(?:\s+-\s+.+)?\s*$",
    re.MULTILINE,
)
ERROR_NODE_RE = re.compile(
    r"^ERROR\s+(.+?)(?:\s+-\s+.+)?\s*$",
    re.MULTILINE,
)

SCRATCH_NAME_RE = re.compile(
    r"(^|/)(repro|repro_.*|netrc_test.*|test_netrc\.py|tmp_.*|\.netrc)(/|$)",
    re.IGNORECASE,
)


def parse_failed_node_ids(test_output: str) -> list[str]:
    """Extract pytest node ids from FAILED/ERROR summary lines."""
    text = test_output or ""
    out: list[str] = []
    seen: set[str] = set()
    for cre in (FAILED_NODE_RE, ERROR_NODE_RE):
        for m in cre.finditer(text):
            node = m.group(1).strip()
            if node and node not in seen:
                seen.add(node)
                out.append(node)
    return out


def patch_files(diff: str) -> list[str]:
    files: list[str] = []
    for ln in (diff or "").splitlines():
        if ln.startswith("diff --git "):
            parts = ln.split()
            if len(parts) >= 4:
                b = parts[3]
                files.append(b[2:] if b.startswith("b/") else b)
        elif ln.startswith("+++ b/"):
            files.append(ln[6:])
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f and f != "/dev/null" and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def patch_is_clean(diff: str) -> bool:
    """True if submitted patch has no scratch/repro/.netrc junk files."""
    for f in patch_files(diff):
        base = f.split("/")[-1]
        if base.startswith(".") and base in {".netrc"}:
            return False
        if SCRATCH_NAME_RE.search(f):
            return False
        if not (
            f.startswith("src/")
            or f.startswith("tests/")
            or (f.endswith(".py") and "/" not in f)
        ):
            if "/" not in f and f.endswith(".py"):
                return False
            if not f.startswith(("src/", "tests/")):
                return False
    return True


def test_patch_target_names(test_patch: str) -> list[str]:
    """Test function names newly added in test_patch (+ def / + async def)."""
    names: list[str] = []
    for ln in (test_patch or "").splitlines():
        if not ln.startswith("+"):
            continue
        m = re.match(r"\+\s*(?:async\s+)?def\s+(test_\w+)\s*\(", ln)
        if m:
            names.append(m.group(1))
    return names


def test_patch_files(test_patch: str) -> list[str]:
    files: list[str] = []
    for ln in (test_patch or "").splitlines():
        if ln.startswith("--- a/"):
            files.append(ln[6:])
        elif ln.startswith("+++ b/"):
            files.append(ln[6:])
    seen: set[str] = set()
    out: list[str] = []
    for f in files:
        if f and f != "/dev/null" and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def is_fail_to_pass_node(node: str, test_patch: str) -> bool:
    """True if node is a test added/modified by test_patch (FAIL_TO_PASS)."""
    return bool(target_tests_failed([node], test_patch))


def target_tests_failed(failed_nodes: list[str], test_patch: str) -> list[str]:
    """Return failed node ids that correspond to tests introduced by test_patch.

    Also treats whole-file ERROR/FAILED lines (no ::) as FAIL_TO_PASS when that
    file is exclusively introduced/touched by test_patch.
    """
    names = set(test_patch_target_names(test_patch))
    tfiles = set(test_patch_files(test_patch))
    hits: list[str] = []
    for node in failed_nodes:
        short = node.split("::")[-1].split("[")[0]
        path = node.split("::", 1)[0]
        if short in names:
            hits.append(node)
            continue
        if names and any(n in node for n in names):
            hits.append(node)
            continue
        # Collection/file-level failure on a test_patch file with no :: node id
        if "::" not in node and node in tfiles:
            hits.append(node)
            continue
        if path in tfiles and short.startswith("test_") and short in names:
            hits.append(node)
    return hits


def filter_baseline_excluding_ftp(
    baseline_nodes: list[str], test_patch: str
) -> tuple[list[str], list[str]]:
    """Split baseline into (collateral_only, forbidden_ftp_hits)."""
    ftp: list[str] = []
    collateral: list[str] = []
    for n in baseline_nodes:
        if is_fail_to_pass_node(n, test_patch):
            ftp.append(n)
        else:
            collateral.append(n)
    return collateral, ftp


def load_gold_baseline(path: Path | None) -> dict[str, list[str]]:
    """Load ignorable collateral failures only.

    Skips tasks with oracle_usable=false / import_path_broken / gold_fails_ftp.
    Never returns FAIL_TO_PASS nodes (stripped when test_patch available in
    structured details; list-only entries kept as-is but rescue still checks FTP).
    """
    if path is None or not path.is_file():
        return {}
    data = json.loads(path.read_text())
    audit = data.get("_audit") or {}
    unusable = set(audit.get("oracle_unusable_tasks") or [])
    out: dict[str, list[str]] = {}
    for tid, val in (data or {}).items():
        if tid.startswith("_"):
            continue
        tid_s = str(tid)
        if tid_s in unusable:
            continue
        detail = (data.get("_details") or {}).get(tid_s) or {}
        if detail.get("oracle_usable") is False:
            continue
        if detail.get("gold_fails_ftp") or detail.get("import_path_broken"):
            continue
        if isinstance(val, list):
            nodes = [str(x) for x in val]
        elif isinstance(val, dict):
            if val.get("oracle_usable") is False:
                continue
            nodes = [
                str(x)
                for x in (val.get("failing_tests") or val.get("failed_node_ids") or [])
            ]
        else:
            continue
        # Prefer collateral-only list when audit annotated it
        collateral = detail.get("collateral_failures")
        if isinstance(collateral, list):
            nodes = [str(x) for x in collateral]
        # Empty collateral + gold pass → no drift immunity needed (omit task)
        if not nodes:
            continue
        out[tid_s] = nodes
    return out


def drift_immune_resolved(
    *,
    raw_resolved: bool,
    test_output: str,
    agent_patch: str,
    test_patch: str,
    task_id: str,
    gold_baseline: dict[str, list[str]],
) -> tuple[bool, dict]:
    """Return (resolved, debug) applying host-drift compensation.

    Rule: if raw harness says resolved → True.
    Else if task has gold baseline *collateral* failures on this host:
      - FAIL_TO_PASS / test_patch targets must not appear in failed nodes
      - FAIL_TO_PASS must not appear in the baseline either (oracle broken)
      - every failed node must be ⊆ gold baseline collateral failures
      - submitted patch must be clean (no scratch files)
    """
    debug: dict = {
        "raw_resolved": bool(raw_resolved),
        "drift_applied": False,
        "reason": "",
    }
    if raw_resolved:
        debug["reason"] = "raw_resolved"
        return True, debug

    if task_id not in gold_baseline:
        debug["reason"] = "no_gold_baseline_or_oracle_unusable"
        return False, debug

    baseline_raw = list(gold_baseline.get(task_id) or [])
    baseline, ftp_in_base = filter_baseline_excluding_ftp(baseline_raw, test_patch)
    if ftp_in_base:
        debug["reason"] = "gold_baseline_contains_fail_to_pass"
        debug["ftp_in_baseline"] = ftp_in_base
        return False, debug
    if not baseline:
        debug["reason"] = "no_collateral_baseline"
        return False, debug

    failed = parse_failed_node_ids(test_output)
    debug["failed_nodes"] = failed
    debug["baseline_nodes"] = baseline

    target_fails = target_tests_failed(failed, test_patch)
    if target_fails:
        debug["reason"] = "fail_to_pass_still_failing"
        debug["target_fails"] = target_fails
        return False, debug

    # Belt-and-suspenders: any failed node matching test_patch file+new name
    base_set = set(baseline)

    def covered(node: str) -> bool:
        if is_fail_to_pass_node(node, test_patch):
            return False
        if node in base_set:
            return True
        bare = node.split("[")[0]
        if bare in base_set:
            return True
        for b in base_set:
            if b.split("[")[0] == bare:
                return True
        return False

    regressions = [n for n in failed if not covered(n)]
    if regressions:
        debug["reason"] = "new_regressions_vs_gold"
        debug["regressions"] = regressions
        return False, debug

    if not patch_is_clean(agent_patch):
        debug["reason"] = "dirty_scratch_patch"
        debug["patch_files"] = patch_files(agent_patch)
        return False, debug

    debug["drift_applied"] = True
    debug["reason"] = "host_drift_match_gold_baseline"
    return True, debug


def fail_bucket_from_meta(meta: dict) -> str:
    if meta.get("resolved") or meta.get("rescue"):
        return "keeper"
    err = str(meta.get("error") or "")
    tail = str(meta.get("test_output_tail") or meta.get("test_output") or "")
    el = err.lower()
    patched = bool(meta.get("patch") or (meta.get("patch_chars") or 0) > 0)
    if "timeout" in el or "max_time" in el:
        return "timeout"
    if "test_patch" in el or "ModuleNotFound" in err:
        return "harness_err"
    if "old_string" in el or "SyntaxError" in tail or "IndentationError" in tail:
        return "syntax_apply"
    if not patched:
        return "no_edit"
    return "assertion_fail"
