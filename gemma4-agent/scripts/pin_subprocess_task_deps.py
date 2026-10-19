#!/usr/bin/env python3
"""Constraint-aware host pinning for subprocess harvest runs.

Applies per-task offline wheel pins against the current Python environment using
the extracted task workspace and competition wheelhouse. This patches the gap in
SubprocessManager, which inherits host site-packages but skips task setup hooks.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore

_SKIP_PIN = frozenset({"pytest"})
_SANDBOX_DIR = Path(__file__).resolve().parent.parent / "sandbox"
# Wheelhouse-safe host extras for FastAPI/rich verify (may be absent from /wheels).
_DEFAULT_SANDBOX_EXTRAS = (
    "typing_inspection",
    "inline_snapshot",
    "dirty_equals",
    "attrs",
    "python-multipart",
    "email-validator",
    "ujson",
    "orjson",
    "greenlet",
    "pytest-httpbin",
    "py",
)
_EXTRA = (
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
)


def _load_sandbox_extras() -> tuple[str, ...]:
    path = _SANDBOX_DIR / "extras.txt"
    if not path.is_file():
        return _DEFAULT_SANDBOX_EXTRAS
    pkgs = [
        ln.strip()
        for ln in path.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return tuple(pkgs) if pkgs else _DEFAULT_SANDBOX_EXTRAS


def _ensure_sandbox_extras(wheels: Path) -> None:
    """Install sandbox extras from wheels when present, else online (PyPI).

    Competition wheelhouse lacks inline_snapshot / dirty_equals; without this,
    subprocess harvest labels false-fail on FastAPI oracle tests.
    """
    extras = _load_sandbox_extras()
    for name in extras:
        wheel_hit = any(
            wh.name.lower().startswith(name.lower().replace("-", "_") + "-")
            or wh.name.lower().startswith(name.lower().replace("_", "-") + "-")
            for wh in wheels.glob("*.whl")
        )
        if wheel_hit:
            result = _pip_install(
                ["install", "--no-index", f"--find-links={wheels}", "--no-deps", name]
            )
            if result.returncode == 0:
                print(f"extra_wheel_ok {name}", flush=True)
                continue
        result = _pip_install(
            [
                "install",
                "--ignore-installed",
                "--break-system-packages",
                "--root-user-action",
                "ignore",
                name,
            ]
        )
        if result.returncode == 0:
            print(f"extra_online_ok {name}", flush=True)
        else:
            print(f"extra_fail {name}", flush=True)


def _add_req(reqs: list[str], raw: str) -> None:
    raw = (raw or "").strip()
    if not raw or raw.startswith(("#", "-")):
        return
    raw = re.sub(r";.*$", "", raw).strip()
    if raw:
        reqs.append(raw)


def _collect_requirements(workspace: Path) -> list[str]:
    reqs: list[str] = []
    pyproject = workspace / "pyproject.toml"
    if tomllib and pyproject.is_file():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = data.get("project") or {}
        for dep in project.get("dependencies") or []:
            _add_req(reqs, dep)
        for deps in (project.get("optional-dependencies") or {}).values():
            for dep in deps or []:
                _add_req(reqs, dep)

    for req_file in list(workspace.glob("requirements*.txt")) + list(
        workspace.glob("**/requirements*.txt")
    ):
        if ".git" in req_file.parts or "docker" in req_file.parts:
            continue
        try:
            for line in req_file.read_text(errors="replace").splitlines():
                _add_req(reqs, line)
        except Exception:
            continue

    uniq: list[str] = []
    seen: set[str] = set()
    for req in reqs:
        key = re.split(r"[=<>!~\[]", req, maxsplit=1)[0].strip().lower()
        if key in seen or key in _SKIP_PIN:
            continue
        seen.add(key)
        uniq.append(req)
    return uniq


def _pip_install(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Pin host deps for one subprocess task workspace")
    ap.add_argument("--workspace", type=Path, required=True)
    ap.add_argument("--wheels", type=Path, required=True)
    args = ap.parse_args()

    workspace = args.workspace.resolve()
    wheels = args.wheels.resolve()
    if not workspace.is_dir():
        print(f"workspace_missing {workspace}", flush=True)
        return 2
    if not wheels.is_dir():
        print(f"wheels_missing {wheels}", flush=True)
        return 2

    reqs = _collect_requirements(workspace)
    print(f"pin_workspace {workspace}", flush=True)
    print(f"pin_reqs candidates={len(reqs)}", flush=True)

    ok = 0
    fail = 0
    base_cmd = [
        "install",
        "--no-index",
        f"--find-links={wheels}",
        "--no-build-isolation",
    ]
    for req in reqs:
        result = _pip_install(base_cmd + [req])
        if result.returncode != 0:
            bare = re.sub(r"\[.*?\]", "", req).strip() or req
            result = _pip_install(base_cmd + ["--no-deps", bare])
        if result.returncode == 0:
            ok += 1
        else:
            fail += 1
            print(f"pin_fail {req[:120]}", flush=True)

    for name in _EXTRA:
        _pip_install(base_cmd + ["--no-deps", name])

    _ensure_sandbox_extras(wheels)

    print(f"pin_done ok={ok} fail={fail}", flush=True)
    for mod in (
        "starlette",
        "fastapi",
        "httpx",
        "pydantic",
        "inline_snapshot",
        "dirty_equals",
    ):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import importlib; "
                    f"m=importlib.import_module('{mod}'); "
                    "print(getattr(m,'__version__', type(m).__name__))"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            print(f"{mod}={probe.stdout.strip()}", flush=True)
        else:
            print(f"{mod}_import_fail", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
