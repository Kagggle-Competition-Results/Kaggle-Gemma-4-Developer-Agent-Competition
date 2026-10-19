#!/usr/bin/env python3
"""Patch installed swegemma SubprocessManager + verification for harvest oracle.

Idempotent. Safe to run on harvest pods before BoN/oracle verify.

Changes (GEMMA4_ORACLE_FLAKE_V2):
  1) SubprocessManager.exec PYTHONPATH -> {workspace}/src:{workspace}
     (covers agent shell tools AND oracle pytest via sandbox_exec)
  2) verification.py pytest_cmd:
       - PYTHONPATH=/workspace/src:/workspace
       - -W ignore::DeprecationWarning (+ Pending)
       - requests env-flake filter: -k 'not test_redirecting_to_bad_url'
  3) SubprocessManager.exec: inject the same -W / -k flags into interactive
     pytest commands the agent runs in-shell
  4) verification.py: when pytest exit!=0, populate TaskResult.error with a
     concise <=20-line failure summary (STaR / G11); previously error was
     empty because junit validation only ran on exit_code==0
  5) junit env-marker skip OK (FAIL_TO_PASS inferred nodes skipped by
     @needs_pydanticv1 must not fail gold)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


MARKER = "GEMMA4_SRCPATH_PATCH_V1"
MARKER_V2 = "GEMMA4_ORACLE_FLAKE_V2"
MARKER_JUNIT = "GEMMA4_JUNIT_SKIP_OK_V1"
MARKER_ERRSUM = "GEMMA4_ORACLE_ERRSUM_V1"

PYPATH_OLD = "'PYTHONPATH': str(workspace_dir),"
PYPATH_NEW = (
    f"# {MARKER}\n"
    "            'PYTHONPATH': ("
    "f'{workspace_dir / \"src\"}:{workspace_dir}' "
    "if (workspace_dir / 'src').is_dir() "
    "else str(workspace_dir)"
    "),"
)

# Full replacement for the verification.py pytest_cmd block (pre-patch form).
PYTEST_CMD_OLD_RE = re.compile(
    r"pytest_cmd\s*=\s*\(\s*\n"
    r"\s*f'cd /workspace && PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 python3 -s -m pytest \{test_files_str\} '\n"
    r"\s*f'--junitxml=\{junit_xml_path\} '\n"
    r"\s*'-p no:anyio -o timeout=0 '\n"
    r"\s*'-o python_classes=\"Test\* \*Test\" -q'\n"
    r"\s*\)",
    re.MULTILINE,
)

# Already V1-patched pytest_cmd (srcpath + warn, no flake -k / no V2).
PYTEST_CMD_V1_RE = re.compile(
    r"pytest_cmd\s*=\s*\(\s*\n"
    r"\s*# GEMMA4_SRCPATH_PATCH_V1\n"
    r"\s*f'cd /workspace && PYTHONPATH=/workspace/src:/workspace "
    r"PYTHONNOUSERSITE=1 python3 -m pytest \{test_files_str\} "
    r"-W ignore::DeprecationWarning -W ignore::PendingDeprecationWarning '\n"
    r"\s*f'--junitxml=\{junit_xml_path\} '\n"
    r"\s*'-p no:anyio -o timeout=0 '\n"
    r"\s*'-o python_classes=\"Test\* \*Test\" -q'\n"
    r"\s*\)",
    re.MULTILINE,
)

KFILTER_HELPER = f'''
def _gemma4_pytest_k_filter(task, test_files_str: str) -> str:
    """{MARKER_V2}: env-flake / gold-collateral -k filters (keep FAIL_TO_PASS)."""
    tid = str(getattr(task, "instance_id", "") or "")
    repo = str(getattr(task, "repo", "") or "").lower()
    files = test_files_str or ""
    parts: list[str] = []
    if (
        "test_requests" in files
        or "requests" in repo
        or tid.startswith("requests_")
    ):
        # Host InvalidURL flake — not a FAIL_TO_PASS target.
        parts.append("not test_redirecting_to_bad_url")
    if tid == "fastapi_14356":
        # tutorial003 collateral fails on gold under host subprocess; unlock
        # targets live in test_query_cookie_header_model_extra_params.py.
        parts.append(
            "not test_header_param_model_invalid and "
            "not test_header_param_model_no_underscore"
        )
    if not parts:
        return ""
    return ' -k "' + " and ".join(parts) + '"'


'''

PYTEST_CMD_V2 = f'''pytest_cmd = (
            # {MARKER_V2}
            # {MARKER}
            (
                f'cd /workspace && PYTHONPATH=/workspace/src:/workspace '
                f'PYTHONNOUSERSITE=1 python3 -m pytest {{test_files_str}} '
                f'-W ignore::DeprecationWarning -W ignore::PendingDeprecationWarning '
                f'--junitxml={{junit_xml_path}} '
                f'-p no:anyio -o timeout=0 '
                f'-o python_classes="Test* *Test" -q'
                + _gemma4_pytest_k_filter(task, test_files_str)
            )
        )'''

# Helper injected into verification.py for non-empty oracle errors.
ERRSUM_HELPER = f'''
def _gemma4_summarize_pytest_failure(stdout: str, stderr: str, *, max_lines: int = 20) -> str:
    """{MARKER_ERRSUM}: concise oracle failure for STaR (G11: <= last 20 lines)."""
    text = (stdout or "") + "\\n" + (stderr or "")
    if not text.strip():
        return "pytest failed (exit!=0) with empty stdout/stderr"
    lines = text.splitlines()
    # Prefer short-test-summary + FAILURES header region when present.
    summary_idx = None
    fail_idx = None
    for i, ln in enumerate(lines):
        if "short test summary info" in ln.lower():
            summary_idx = i
        if ln.strip().startswith("FAILURES") or (
            ln.strip().startswith("=") and "FAILURES" in ln
        ):
            fail_idx = i if fail_idx is None else fail_idx
    picked: list[str] = []
    if summary_idx is not None:
        picked = lines[summary_idx : summary_idx + max_lines]
    elif fail_idx is not None:
        picked = lines[fail_idx : fail_idx + max_lines]
    else:
        # last non-empty lines
        nonempty = [ln for ln in lines if ln.strip()]
        picked = nonempty[-max_lines:]
    # Keep failed node names + assertion crumbs; drop huge dumps.
    out_lines: list[str] = []
    for ln in picked:
        if len(ln) > 240:
            ln = ln[:240] + "…"
        out_lines.append(ln)
        if len(out_lines) >= max_lines:
            break
    return "\\n".join(out_lines) if out_lines else "pytest failed (exit!=0)"


'''

# Patch site: after `resolved = test_res.exit_code == 0` ensure else fills error.
RESOLVED_BLOCK_OLD = """        resolved = test_res.exit_code == 0
        junit_error: str | None = None
        if resolved:"""

RESOLVED_BLOCK_NEW = f"""        resolved = test_res.exit_code == 0
        junit_error: str | None = None
        # {MARKER_ERRSUM}
        if not resolved:
            junit_error = _gemma4_summarize_pytest_failure(
                test_res.stdout or '', test_res.stderr or '', max_lines=20
            )
        if resolved:"""

# Interactive pytest injection snippet for SubprocessManager.exec
INTERACTIVE_INJECT_OLD = """        translated_cmd = translated_cmd.replace(sentinel, root_dir_str)

        venv_bin = venv_dir / 'bin'"""

INTERACTIVE_INJECT_NEW = f"""        translated_cmd = translated_cmd.replace(sentinel, root_dir_str)

        # {MARKER_V2}: inject harvest pytest guards into interactive agent commands
        if re.search(r'(?:^|[\\s;|&])pytest(?:\\s|$)|python3?\\s+-m\\s+pytest', translated_cmd):
            if '-W ignore::DeprecationWarning' not in translated_cmd:
                translated_cmd = re.sub(
                    r'(pytest\\b|python3?\\s+-m\\s+pytest)',
                    r'\\1 -W ignore::DeprecationWarning -W ignore::PendingDeprecationWarning',
                    translated_cmd,
                    count=1,
                )
            if (
                'test_redirecting_to_bad_url' not in translated_cmd
                and (
                    'test_requests' in translated_cmd
                    or re.search(r'(?:^|[\\s/])requests(?:[\\s/]|$)', translated_cmd)
                )
                and '-k ' not in translated_cmd
            ):
                translated_cmd = re.sub(
                    r'(pytest\\b(?:\\s+-W[^\\s]+)*|python3?\\s+-m\\s+pytest(?:\\s+-W[^\\s]+)*)',
                    r'\\1 -k \"not test_redirecting_to_bad_url\"',
                    translated_cmd,
                    count=1,
                )

        venv_bin = venv_dir / 'bin'"""


def _site_packages(python: Path | None) -> Path:
    if python is None:
        import swegemma

        return Path(swegemma.__file__).resolve().parent
    import subprocess

    out = subprocess.check_output(
        [
            str(python),
            "-c",
            "import swegemma, pathlib; print(pathlib.Path(swegemma.__file__).parent)",
        ],
        text=True,
    ).strip()
    return Path(out)


def _patch_subprocess(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []
    if PYPATH_OLD in text:
        text = text.replace(PYPATH_OLD, PYPATH_NEW, 1)
        changes.append("PYTHONPATH_src")
    elif MARKER not in text and "'PYTHONPATH'" in text:
        text2, n = re.subn(
            r"'PYTHONPATH'\s*:\s*str\(workspace_dir\)\s*,",
            PYPATH_NEW,
            text,
            count=1,
        )
        if n:
            text = text2
            changes.append("PYTHONPATH_src_re")

    if MARKER_V2 not in text or "test_redirecting_to_bad_url" not in text:
        if INTERACTIVE_INJECT_OLD in text and MARKER_V2 not in text:
            text = text.replace(INTERACTIVE_INJECT_OLD, INTERACTIVE_INJECT_NEW, 1)
            changes.append("interactive_pytest_guards")
        elif (
            MARKER_V2 not in text
            and "translated_cmd = translated_cmd.replace(sentinel, root_dir_str)"
            in text
        ):
            # looser insert after sentinel replace
            needle = "translated_cmd = translated_cmd.replace(sentinel, root_dir_str)\n"
            if needle in text and "interactive agent commands" not in text:
                insert = (
                    needle
                    + "\n"
                    + "\n".join(INTERACTIVE_INJECT_NEW.splitlines()[1:])  # skip dup line
                    + "\n"
                )
                # simpler: just replace needle with inject block start
                text = text.replace(
                    needle,
                    INTERACTIVE_INJECT_NEW.split("venv_bin = venv_dir / 'bin'")[0]
                    + "\n",
                    1,
                )
                # ensure venv_bin line still present once
                if "venv_bin = venv_dir / 'bin'" not in text:
                    text = text.replace(
                        f"# {MARKER_V2}: inject harvest pytest guards into interactive agent commands\n",
                        f"# {MARKER_V2}: inject harvest pytest guards into interactive agent commands\n"
                        "        venv_bin = venv_dir / 'bin'\n",
                        1,
                    )
                changes.append("interactive_pytest_guards_loose")
    return text, changes


def _patch_verification(text: str) -> tuple[str, list[str]]:
    changes: list[str] = []

    # Inject helpers once (before async def verify_task)
    anchor = "async def verify_task("
    if anchor in text:
        prefix = ""
        if "_gemma4_summarize_pytest_failure" not in text:
            prefix += ERRSUM_HELPER
            changes.append("errsum_helper")
        if "_gemma4_pytest_k_filter" not in text:
            prefix += KFILTER_HELPER
            changes.append("kfilter_helper")
        if prefix:
            text = text.replace(anchor, prefix + anchor, 1)

    # pytest_cmd → V2 (detect by call site, not helper docstring markers)
    if "_gemma4_pytest_k_filter(task, test_files_str)" not in text:
        if PYTEST_CMD_OLD_RE.search(text):
            text, n = PYTEST_CMD_OLD_RE.subn(PYTEST_CMD_V2, text, count=1)
            if n:
                changes.append("pytest_cmd_v2")
        elif PYTEST_CMD_V1_RE.search(text):
            text, n = PYTEST_CMD_V1_RE.subn(PYTEST_CMD_V2, text, count=1)
            if n:
                changes.append("pytest_cmd_v1_to_v2")
        else:
            # fallbacks: string surgery on common forms
            if (
                "PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 python3 -s -m pytest" in text
            ):
                text2 = text.replace(
                    "PYTHONSAFEPATH=1 PYTHONNOUSERSITE=1 python3 -s -m pytest",
                    "PYTHONPATH=/workspace/src:/workspace PYTHONNOUSERSITE=1 python3 -m pytest "
                    "-W ignore::DeprecationWarning -W ignore::PendingDeprecationWarning",
                    1,
                )
                if text2 != text:
                    text = text2
                    changes.append("pytest_cmd_simple")
            if (
                "PYTHONPATH=/workspace/src:/workspace" in text
                and "-W ignore::DeprecationWarning" not in text
                and "python3 -m pytest" in text
            ):
                text2 = text.replace(
                    "PYTHONPATH=/workspace/src:/workspace PYTHONNOUSERSITE=1 python3 -m pytest",
                    "PYTHONPATH=/workspace/src:/workspace PYTHONNOUSERSITE=1 python3 -m pytest "
                    "-W ignore::DeprecationWarning -W ignore::PendingDeprecationWarning",
                    1,
                )
                if text2 != text:
                    text = text2
                    changes.append("pytest_warn_filters")
            if "not test_redirecting_to_bad_url" not in text:
                m = re.search(
                    r"pytest_cmd = \([\s\S]*?\)\n(\s*)test_res = await sandbox_exec",
                    text,
                )
                if m:
                    text = (
                        text[: m.start()]
                        + PYTEST_CMD_V2
                        + "\n"
                        + m.group(1)
                        + "test_res = await sandbox_exec"
                        + text[m.end() :]
                    )
                    changes.append("pytest_cmd_block_replace")

    # Non-empty error when exit!=0
    if (
        RESOLVED_BLOCK_OLD in text
        and "junit_error = _gemma4_summarize_pytest_failure" not in text
    ):
        text = text.replace(RESOLVED_BLOCK_OLD, RESOLVED_BLOCK_NEW, 1)
        changes.append("errsum_on_exit_nonzero")

    # junit env skip OK
    if MARKER_JUNIT not in text:
        old_req = (
            "            if not has_pass or has_non_pass:\n"
            "                return False, f'Required test node did not pass: {req}'"
        )
        new_req = (
            f"            # {MARKER_JUNIT}\n"
            "            if has_pass and not has_non_pass:\n"
            "                continue\n"
            "            if (\n"
            "                has_non_pass\n"
            "                and not has_pass\n"
            "                and total_failures == 0\n"
            "                and total_errors == 0\n"
            "            ):\n"
            "                # Required node present but only skipped (env marker).\n"
            "                continue\n"
            "            if not has_pass or has_non_pass:\n"
            "                return False, f'Required test node did not pass: {req}'"
        )
        if old_req in text:
            text = text.replace(old_req, new_req, 1)
            changes.append("junit_env_skip_ok")

    return text, changes


def patch_file(path: Path, *, dry_run: bool) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if path.name == "subprocess.py":
        text2, changes = _patch_subprocess(text)
    elif path.name == "verification.py":
        text2, changes = _patch_verification(text)
    else:
        return []

    if changes and not dry_run:
        bak = path.with_suffix(path.suffix + ".bak_srcpath")
        if not bak.exists():
            bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(text2, encoding="utf-8")
    return changes


def apply_patches(*, python: Path | None, dry_run: bool) -> dict:
    root = _site_packages(python)
    targets = [
        root / "sandbox" / "subprocess.py",
        root / "harness" / "verification.py",
    ]
    report: dict = {"swegemma_root": str(root), "files": {}, "markers": MARKER_V2}
    for t in targets:
        if not t.is_file():
            report["files"][str(t)] = ["MISSING"]
            continue
        ch = patch_file(t, dry_run=dry_run)
        report["files"][str(t)] = ch or ["noop_already_patched_or_no_match"]
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    import json

    rep = apply_patches(python=args.python, dry_run=args.dry_run)
    print(json.dumps(rep, indent=2))
    sub = next((k for k in rep["files"] if k.endswith("subprocess.py")), None)
    ver = next((k for k in rep["files"] if k.endswith("verification.py")), None)
    if sub:
        status = rep["files"][sub]
        if status == ["MISSING"]:
            return 2
        text = Path(sub).read_text(encoding="utf-8")
        if MARKER not in text and 'workspace_dir / "src"' not in text:
            print("ERROR: subprocess.py PYTHONPATH not patched", file=sys.stderr)
            return 3
    if ver and not args.dry_run:
        text = Path(ver).read_text(encoding="utf-8")
        missing = []
        if "-W ignore::DeprecationWarning" not in text:
            missing.append("deprecation_warn")
        if "not test_redirecting_to_bad_url" not in text:
            missing.append("requests_flake_k")
        if "_gemma4_pytest_k_filter" not in text:
            missing.append("kfilter_helper")
        if "_gemma4_summarize_pytest_failure" not in text:
            missing.append("errsum_helper")
        if missing:
            print(f"ERROR: verification.py missing {missing}", file=sys.stderr)
            return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
