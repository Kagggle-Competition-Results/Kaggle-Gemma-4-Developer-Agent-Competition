#!/usr/bin/env python3
"""Install host-side packages required for swegemma sandbox=subprocess verify.

Why: SubprocessManager no-ops install_test_dependencies / install_editable_package.
Verify inherits host site-packages (system_site_packages=True). Kaggle notebooks
pip-install the metric wheelhouse into the host first; bare RunPod bootstrap does not,
so post-submit pytest fails on correct patches (resolved=False, often empty error).

Do NOT `pip install data/wheels/*.whl` unconstrained — starlette 1.x wins and breaks FastAPI
(see docs/harness_verify.md). This script installs a pinned safe subset from the competition
wheelhouse, then explicitly installs the extra sandbox packages listed in
`gemma4-agent/sandbox/extras.txt` when they are absent from the wheelhouse.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

_SANDBOX_DIR = Path(__file__).resolve().parent.parent / "sandbox"
DEFAULT_EXTRA_REQS = (
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

# Prefer 8.3.4 (harness_verify image pin). Never install every starlette wheel.
CORE_REQS = [
    "pytest==8.3.4",
    "iniconfig",
    "pluggy",
    "packaging",
    "anyio",
    "sniffio",
    "httpx",
    "httpcore",
    "certifi",
    "idna",
    "h11",
]


def find_wheels_dir(data: Path) -> Path | None:
    for cand in (
        data / "wheels",
        data / "_full" / "wheels",
        data.parent / "data" / "_full" / "wheels",
    ):
        if cand.is_dir() and any(cand.glob("*.whl")):
            return cand
    return None


def dist_name(req: str) -> str:
    return re.split(r"[<=>!~\[]", req, maxsplit=1)[0].strip().lower().replace("-", "_")


def load_extra_reqs() -> tuple[str, ...]:
    path = _SANDBOX_DIR / "extras.txt"
    if not path.is_file():
        return DEFAULT_EXTRA_REQS
    pkgs = [
        ln.strip()
        for ln in path.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return tuple(pkgs) if pkgs else DEFAULT_EXTRA_REQS


def wheel_covers(wheels: Path, req: str) -> bool:
    dist = dist_name(req)
    for wh in wheels.glob("*.whl"):
        # PEP 427: {distribution}-{version}-...
        stem = wh.name[:-4]
        parts = stem.split("-")
        if not parts:
            continue
        wh_dist = parts[0].lower().replace("-", "_")
        if wh_dist == dist:
            return True
    return False


def dist_installed(req: str) -> bool:
    try:
        importlib.metadata.version(re.split(r"[<=>!~\[]", req, maxsplit=1)[0].strip())
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def build_install_plan(wheels: Path, reqs: list[str], extra_reqs: tuple[str, ...]) -> tuple[list[str], list[str], list[str], list[str]]:
    wheel_reqs: list[str] = []
    online_reqs: list[str] = []
    installed_reqs: list[str] = []
    skipped_reqs: list[str] = []
    seen: set[str] = set()

    for req in reqs:
        dist = dist_name(req)
        if dist in seen:
            continue
        seen.add(dist)
        if wheel_covers(wheels, req):
            wheel_reqs.append(req)
        elif dist_installed(req):
            installed_reqs.append(req)
        elif req in extra_reqs or req in CORE_REQS:
            online_reqs.append(req)
        else:
            skipped_reqs.append(req)
    return wheel_reqs, online_reqs, installed_reqs, skipped_reqs


def main() -> int:
    ap = argparse.ArgumentParser(description="Ensure host deps for subprocess harvest verify")
    ap.add_argument(
        "--data",
        type=Path,
        default=Path("data"),
        help="competition data root (wheels or _full/wheels)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    wheels = find_wheels_dir(args.data)
    if wheels is None:
        print(f"ERROR: no wheels under {args.data}", file=sys.stderr)
        return 2

    extra_reqs = load_extra_reqs()
    reqs = CORE_REQS + list(extra_reqs)
    wheel_reqs, online_reqs, installed_reqs, skipped_reqs = build_install_plan(wheels, reqs, extra_reqs)
    print("wheels", wheels)
    print("install_wheels", wheel_reqs)
    if online_reqs:
        print("install_online", online_reqs)
    if installed_reqs:
        print("already_installed", installed_reqs)
    if skipped_reqs:
        print("skip_no_wheel", skipped_reqs)

    if not wheel_reqs and not online_reqs and not installed_reqs:
        print("ERROR: no host deps available to install", file=sys.stderr)
        return 2

    if args.dry_run:
        return 0

    if wheel_reqs:
        wheel_cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            f"--find-links={wheels}",
            *wheel_reqs,
        ]
        print("cmd_wheels", " ".join(wheel_cmd))
        subprocess.run(wheel_cmd, check=True)

    if online_reqs:
        online_cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--ignore-installed",
            "--break-system-packages",
            "--root-user-action",
            "ignore",
            *online_reqs,
        ]
        print("cmd_online", " ".join(online_cmd))
        subprocess.run(online_cmd, check=True)

    if importlib.util.find_spec("pytest") is None:
        print("ERROR: pytest still missing after install", file=sys.stderr)
        return 3
    import pytest

    print("ok pytest", getattr(pytest, "__version__", "?"))

    # Hard-pin FastAPI-compatible starlette (never leave 1.x from unconstrained installs).
    if wheels is not None:
        pin_ver = "0.46.2"
        hits = sorted(wheels.glob(f"starlette-{pin_ver}-*.whl"))
        if hits:
            pin_cmd = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--force-reinstall",
                "--break-system-packages",
                "--root-user-action",
                "ignore",
                str(hits[0]),
            ]
            print("cmd_starlette_pin", " ".join(pin_cmd))
            subprocess.run(pin_cmd, check=False)
        # pytest-timeout registers the `timeout` ini option used by some task pytest.ini
        to_hits = sorted(wheels.glob("pytest_timeout-*.whl")) or sorted(
            wheels.glob("pytest-timeout-*.whl")
        )
        if to_hits:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--force-reinstall",
                    "--break-system-packages",
                    "--root-user-action",
                    "ignore",
                    str(to_hits[-1]),
                ],
                check=False,
            )
    try:
        import starlette

        print("ok starlette", getattr(starlette, "__version__", "?"))
    except Exception as e:  # noqa: BLE001
        print("starlette_import_fail", e)
    print(
        "NOTE: SubprocessManager must set PYTHONPATH=workspace/src:workspace "
        "(see scripts/patch_swegemma_subprocess.py)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
