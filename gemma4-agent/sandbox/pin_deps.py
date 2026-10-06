#!/usr/bin/env python3
"""Constraint-aware offline wheel install for soundness verify.

Version: 2026-10-06.5 — see docs/harness_verify.md

Mounted into the sandbox as /tmp/pin_deps.py by soundness_check.run_verify.
Do NOT install every /wheels/*.whl (newest starlette breaks FastAPI).
Never downgrade image pytest (<8.3.4 lacks version_tuple; breaks executing).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    tomllib = None  # type: ignore

wheels = Path("/wheels")
reqs: list[str] = []

# Keep image pytest — task wheels often ship older pytest without version_tuple.
_SKIP_PIN = frozenset({"pytest"})


def add_req(s: str) -> None:
    s = (s or "").strip()
    if not s or s.startswith(("#", "-")):
        return
    s = re.sub(r";.*$", "", s)  # environment markers
    s = s.strip()
    if s:
        reqs.append(s)


pp = Path("/workspace/pyproject.toml")
if tomllib and pp.is_file():
    data = tomllib.loads(pp.read_text(encoding="utf-8"))
    proj = data.get("project") or {}
    for dep in proj.get("dependencies") or []:
        add_req(dep)
    for _group, deps in (proj.get("optional-dependencies") or {}).items():
        for dep in deps or []:
            add_req(dep)

for rf in list(Path("/workspace").glob("requirements*.txt")) + list(
    Path("/workspace").glob("**/requirements*.txt")
):
    if ".git" in rf.parts or "docker" in rf.parts:
        continue
    try:
        for line in rf.read_text(errors="replace").splitlines():
            add_req(line)
    except Exception:
        pass

seen: set[str] = set()
uniq: list[str] = []
for r in reqs:
    key = re.split(r"[=<>!~\[]", r, maxsplit=1)[0].strip().lower()
    if key in seen:
        continue
    if key in _SKIP_PIN:
        print(f"pin_skip {key} (keep image)", flush=True)
        continue
    seen.add(key)
    uniq.append(r)

print(f"pin_deps candidates={len(uniq)}", flush=True)
ok = fail = 0
for req in uniq:
    cmd_base = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-index",
        f"--find-links={wheels}",
        "--no-build-isolation",
    ]
    r = subprocess.run(cmd_base + [req], capture_output=True, text=True)
    if r.returncode != 0:
        bare = re.sub(r"\[.*?\]", "", req).strip()
        r2 = subprocess.run(
            cmd_base + ["--no-deps", bare or req], capture_output=True, text=True
        )
        if r2.returncode != 0:
            fail += 1
            print(f"pin_fail {req[:80]}", flush=True)
            continue
    ok += 1

# Best-effort fill from /wheels only — never unconstrained starlette.
EXTRA = [
    "httpx",
    "httpcore",
    "h11",
    "anyio",
    "sniffio",
    "certifi",
    "idna",
    "attrs",
    "annotated-types",
    "typing-extensions",
    "iniconfig",
    "packaging",
    "pluggy",
    "pygments",
    "markdown-it-py",
    "mdurl",
    "rich",
    "click",
    "sqlalchemy",
    "sqlmodel",
    "charset-normalizer",
    "urllib3",
]
for name in EXTRA:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            f"--find-links={wheels}",
            "--no-build-isolation",
            "--no-deps",
            name,
        ],
        capture_output=True,
        text=True,
    )

print(f"pin_deps ok={ok} fail={fail}", flush=True)
try:
    import starlette

    print(f"starlette={starlette.__version__}", flush=True)
except Exception as e:
    print(f"starlette_import={e}", flush=True)
try:
    import pydantic
    import pydantic_core

    print(
        f"pydantic={pydantic.__version__} pydantic_core={pydantic_core.__version__}",
        flush=True,
    )
except Exception as e:
    print(f"pydantic_import={e}", flush=True)
try:
    import httpx

    print(f"httpx={httpx.__version__}", flush=True)
except Exception as e:
    print(f"httpx_import={e}", flush=True)
