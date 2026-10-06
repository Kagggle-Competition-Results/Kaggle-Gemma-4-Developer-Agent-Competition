#!/usr/bin/env python3
"""P1.1 — Soundness: empty patch must fail; oracle (gold) patch must pass.

Uses the competition sandbox Docker image + HARNESS Phase-2 style verify:
  pin deps from pyproject (version-aware) → setup fast-path → gold? → test_patch → pytest.

**Do not** install every `/wheels/*.whl` (newest starlette breaks FastAPI). See
`docs/harness_verify.md` (versioned runbook).

Preferred: `swegemma` CLI when installed. Fallback: docker `gemma4-sandbox:base`.

Columns: task_id,empty_pass,oracle_pass,error,usable

Spot/cloud: `--resume --s3-uri s3://…/results/soundness.csv` checkpoints after each task.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

IMAGE = os.environ.get("SOUNDNESS_IMAGE", "gemma4-sandbox:base")
# Competition wheels are manylinux x86_64; force amd64 on Apple Silicon.
DOCKER_PLATFORM = os.environ.get("SOUNDNESS_DOCKER_PLATFORM", "linux/amd64")
# Keep in sync with docs/harness_verify.md + sandbox/ extras.
HARNESS_VERIFY_DOC_VERSION = "2026-10-06.5"
_SANDBOX_DIR = Path(__file__).resolve().parent.parent / "sandbox"


def _load_extras() -> tuple[str, ...]:
    path = _SANDBOX_DIR / "extras.txt"
    if path.is_file():
        pkgs = [
            ln.strip()
            for ln in path.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        return tuple(pkgs)
    return (
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
    )


SANDBOX_EXTRA_PIP = _load_extras()


def _pin_deps_source() -> str:
    """Prefer checked-in sandbox/pin_deps.py (repo backup); fallback kept for S3-only replicas."""
    path = _SANDBOX_DIR / "pin_deps.py"
    if path.is_file():
        return path.read_text()
    return _PIN_DEPS_PY_FALLBACK


# Fallback only if sandbox/pin_deps.py missing (e.g. bare S3 script copy).
_PIN_DEPS_PY_FALLBACK = r'''
import re, subprocess, sys
from pathlib import Path
try:
    import tomllib
except ImportError:
    tomllib = None
wheels = Path("/wheels")
reqs: list[str] = []
def add_req(s: str) -> None:
    s = (s or "").strip()
    if not s or s.startswith(("#", "-")): return
    s = re.sub(r";.*$", "", s).strip()
    if s: reqs.append(s)
pp = Path("/workspace/pyproject.toml")
if tomllib and pp.is_file():
    data = tomllib.loads(pp.read_text(encoding="utf-8"))
    proj = data.get("project") or {}
    for dep in proj.get("dependencies") or []: add_req(dep)
    for _g, deps in (proj.get("optional-dependencies") or {}).items():
        for dep in deps or []: add_req(dep)
for rf in list(Path("/workspace").glob("requirements*.txt")) + list(Path("/workspace").glob("**/requirements*.txt")):
    if ".git" in rf.parts or "docker" in rf.parts: continue
    try:
        for line in rf.read_text(errors="replace").splitlines(): add_req(line)
    except Exception: pass
seen=set(); uniq=[]
for r in reqs:
    key=re.split(r"[=<>!~\[]", r, maxsplit=1)[0].strip().lower()
    if key in seen: continue
    seen.add(key); uniq.append(r)
print(f"pin_deps candidates={len(uniq)}", flush=True)
ok=fail=0
for req in uniq:
    cmd=[sys.executable,"-m","pip","install","--no-index",f"--find-links={wheels}","--no-build-isolation"]
    if subprocess.run(cmd+[req], capture_output=True, text=True).returncode!=0:
        bare=re.sub(r"\[.*?\]","",req).strip()
        if subprocess.run(cmd+["--no-deps", bare or req], capture_output=True, text=True).returncode!=0:
            fail+=1; print(f"pin_fail {req[:80]}", flush=True); continue
    ok+=1
for name in ["httpx","httpcore","h11","anyio","sniffio","certifi","idna","attrs","sqlalchemy","sqlmodel","charset-normalizer","urllib3","pygments","markdown-it-py"]:
    subprocess.run([sys.executable,"-m","pip","install","--no-index",f"--find-links={wheels}","--no-build-isolation","--no-deps",name], capture_output=True, text=True)
print(f"pin_deps ok={ok} fail={fail}", flush=True)
try:
    import starlette; print(f"starlette={starlette.__version__}", flush=True)
except Exception as e:
    print(f"starlette_import={e}", flush=True)
'''


def task_id(t: dict) -> str:
    for key in ("instance_id", "task_id", "id", "name"):
        if key in t:
            return str(t[key])
    raise KeyError(f"no task id in keys={list(t)}")


def load_tasks(data: Path) -> list[dict]:
    path = data / "tasks.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"missing {path}")
    tasks = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def test_targets_from_patch(test_patch: str) -> list[str]:
    files: list[str] = []
    for m in re.finditer(r"^\+\+\+ [ab]/(.+)$", test_patch, re.M):
        f = m.group(1).strip()
        if f.endswith(".py"):
            files.append(f)
    # prefer test files
    tests = [f for f in files if "test" in f.replace("\\", "/").lower()]
    return sorted(set(tests or files))


def docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.returncode == 0 and "Server Version" in (r.stdout + r.stderr)
    except Exception:
        return False


def image_exists(name: str) -> bool:
    r = subprocess.run(
        ["docker", "image", "inspect", name],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def build_image_if_needed(data: Path, *, force: bool = False) -> str:
    if image_exists(IMAGE) and not force:
        return IMAGE
    docker_dir = data / "docker"
    if not (docker_dir / "imp.py").is_file() or not (docker_dir / "telnetlib.py").is_file():
        raise FileNotFoundError(f"missing competition docker shims under {docker_dir}")
    build = Path(tempfile.mkdtemp(prefix="soundness_img_"))
    try:
        shutil.copy(docker_dir / "imp.py", build / "imp.py")
        shutil.copy(docker_dir / "telnetlib.py", build / "telnetlib.py")
        our_df = _SANDBOX_DIR / "Dockerfile.gemma4"
        if our_df.is_file():
            # Repo-backed recipe (preferred backup — not AMI).
            shutil.copy(our_df, build / "Dockerfile")
            print(f"docker_build using {_SANDBOX_DIR.name}/Dockerfile.gemma4", flush=True)
        else:
            # Legacy: patch competition Dockerfile.sandbox with extras.txt packages.
            src = docker_dir / "Dockerfile.sandbox"
            if not src.is_file():
                raise FileNotFoundError(f"missing {src} and {our_df}")
            shutil.copy(src, build / "Dockerfile")
            df = (build / "Dockerfile").read_text()
            extras = " ".join(SANDBOX_EXTRA_PIP)
            if "inline_snapshot" not in df:
                needles = [
                    (
                        "&& pip install --no-cache-dir pytest pytest-timeout==2.1.0 typer "
                        "pdm-backend setuptools wheel poetry-core hatchling flit-core editables \\"
                    ),
                    (
                        "&& pip install --no-cache-dir pytest pytest-timeout==2.1.0 typer "
                        "pdm-backend setuptools wheel poetry-core hatchling flit-core editables"
                    ),
                    (
                        '&& pip install --no-cache-dir "pytest>=8.3.4" pytest-timeout==2.1.0 typer '
                        "pdm-backend setuptools wheel poetry-core hatchling flit-core editables \\"
                    ),
                    (
                        '&& pip install --no-cache-dir "pytest>=8.3.4" pytest-timeout==2.1.0 typer '
                        "pdm-backend setuptools wheel poetry-core hatchling flit-core editables"
                    ),
                ]
                patched = False
                for needle in needles:
                    if needle in df:
                        base = needle.rstrip()
                        if base.endswith("\\"):
                            base = base[:-1].rstrip()
                        df = df.replace(needle, f"{base} {extras} \\", 1)
                        (build / "Dockerfile").write_text(df)
                        patched = True
                        break
                if not patched:
                    print("warn: Dockerfile pip line not patched; extras may be missing", flush=True)
        r = subprocess.run(
            ["docker", "build", "--platform", DOCKER_PLATFORM, "-t", IMAGE, "."],
            cwd=build,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"docker build failed: {r.stderr[-2000:]}")
        return IMAGE
    finally:
        shutil.rmtree(build, ignore_errors=True)


def run_verify(
    *,
    data: Path,
    tid: str,
    gold_patch: str | None,
    test_patch: str,
    timeout_s: int = 300,
) -> tuple[bool | None, str]:
    """Return (passed, error). passed None means infra failure."""
    snap = data / "snapshots" / f"{tid}.tgz"
    if not snap.is_file():
        return None, "missing_snapshot"
    setup_py = data / "sandbox" / "setup.py"
    wheels = data / "wheels"
    if not setup_py.is_file():
        return None, "missing_sandbox_setup"
    if not wheels.is_dir():
        return None, "missing_wheels"

    targets = test_targets_from_patch(test_patch)
    if not targets:
        return None, "no_test_targets_in_test_patch"

    work = Path(tempfile.mkdtemp(prefix=f"sound_{tid}_"))
    try:
        subprocess.run(
            ["tar", "-xzf", str(snap), "-C", str(work)],
            check=True,
            capture_output=True,
        )
        gold_path = work / "_gold.patch"
        test_path = work / "_test.patch"
        if gold_patch is not None:
            gold_path.write_text(ensure_trailing_newline(gold_patch))
        test_path.write_text(ensure_trailing_newline(test_patch))
        targets_arg = " ".join(shlex.quote(t) for t in targets)
        pin_path = work / "_pin_deps.py"
        pin_path.write_text(_pin_deps_source())

        script = f"""
set +e
cd /workspace
# Constraint-aware wheel install (do NOT install every /wheels/*.whl — newest starlette wins).
python3 /tmp/pin_deps.py >/tmp/pin_deps.log 2>&1
tail -20 /tmp/pin_deps.log
# Fast-path setup only: git exclude / pytest / editable. Avoid setup.py's unpinned max-version pick.
export SWE_PREBAKED=1
python3 /tmp/setup_sandbox.py >/tmp/setup.log 2>&1
pip install --no-index --find-links=/wheels --no-build-isolation --no-deps -e /workspace >/tmp/editable.log 2>&1
# Re-assert pins after editable (in case setup touched env)
python3 /tmp/pin_deps.py >>/tmp/pin_deps.log 2>&1
if [ -f /tmp/gold.patch ]; then
  git apply --unsafe-paths -p1 /tmp/gold.patch >/tmp/gold_apply.log 2>&1 \\
    || git apply --unsafe-paths -p0 /tmp/gold.patch >>/tmp/gold_apply.log 2>&1
  GOLD_RC=$?
  if [ "$GOLD_RC" -ne 0 ]; then
    echo "gold_apply_failed"
    cat /tmp/gold_apply.log | tail -20
    exit 97
  fi
fi
git apply --unsafe-paths -p1 /tmp/test.patch >/tmp/test_apply.log 2>&1 \\
  || git apply --unsafe-paths -p0 /tmp/test.patch >>/tmp/test_apply.log 2>&1
TEST_RC=$?
if [ "$TEST_RC" -ne 0 ]; then
  echo "test_apply_failed"
  cat /tmp/test_apply.log | tail -20
  exit 98
fi
python3 -c "import starlette; print('starlette_runtime', starlette.__version__)" 2>/dev/null || true
python3 -s -m pytest {targets_arg} -q --tb=line -o timeout=60 \\
  --junitxml=/tmp/junit.xml >/tmp/pytest.out 2>&1
PY_RC=$?
echo "pytest_exit=$PY_RC"
tail -30 /tmp/pytest.out
exit $PY_RC
"""
        cmd = [
            "docker",
            "run",
            "--rm",
            "--platform",
            DOCKER_PLATFORM,
            "-v",
            f"{work}:/workspace",
            "-v",
            f"{wheels}:/wheels:ro",
            "-v",
            f"{setup_py}:/tmp/setup_sandbox.py:ro",
            "-v",
            f"{pin_path}:/tmp/pin_deps.py:ro",
            "-v",
            f"{test_path}:/tmp/test.patch:ro",
        ]
        if gold_patch is not None:
            cmd += ["-v", f"{gold_path}:/tmp/gold.patch:ro"]
        cmd += [IMAGE, "bash", "-lc", script]

        t0 = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        elapsed = time.time() - t0
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        if r.returncode == 97:
            return None, f"gold_apply_failed:{out[-400:]}"
        if r.returncode == 98:
            return None, f"test_apply_failed:{out[-400:]}"
        # pytest 0 = pass; anything else = fail
        passed = r.returncode == 0
        note = f"rc={r.returncode};{elapsed:.1f}s"
        if not passed:
            note += ";" + out[-300:].replace("\n", " ")
        return passed, note
    except subprocess.TimeoutExpired:
        return None, f"timeout_{timeout_s}s"
    except Exception as e:
        return None, f"{type(e).__name__}:{e}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_task(data: Path, t: dict, timeout_s: int) -> dict:
    tid = task_id(t)
    gold = t.get("patch") or t.get("reference_patch") or t.get("gold_patch")
    test_patch = t.get("test_patch")
    if not gold or not test_patch:
        return {
            "task_id": tid,
            "empty_pass": "",
            "oracle_pass": "",
            "error": "missing_patch_or_test_patch",
            "usable": False,
        }

    empty_ok, empty_err = run_verify(
        data=data, tid=tid, gold_patch=None, test_patch=test_patch, timeout_s=timeout_s
    )
    oracle_ok, oracle_err = run_verify(
        data=data, tid=tid, gold_patch=str(gold), test_patch=test_patch, timeout_s=timeout_s
    )

    errs = []
    if empty_ok is None:
        errs.append(f"empty:{empty_err}")
    if oracle_ok is None:
        errs.append(f"oracle:{oracle_err}")

    # Sound: empty must NOT pass; oracle must pass
    usable = (
        empty_ok is False
        and oracle_ok is True
        and empty_ok is not None
        and oracle_ok is not None
    )
    if empty_ok is True:
        errs.append("empty_unexpected_pass")
    if oracle_ok is False:
        errs.append(f"oracle_fail:{oracle_err}")

    return {
        "task_id": tid,
        "empty_pass": "" if empty_ok is None else str(bool(empty_ok)).lower(),
        "oracle_pass": "" if oracle_ok is None else str(bool(oracle_ok)).lower(),
        "error": ";".join(errs),
        "usable": usable,
    }


def filter_tasks(tasks: list[dict], only: list[str] | None, limit: int | None) -> list[dict]:
    if only:
        want = set(only)
        tasks = [t for t in tasks if task_id(t) in want]
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


FIELDNAMES = ["task_id", "empty_pass", "oracle_pass", "error", "usable"]


def load_split_ids(splits_path: Path, split: str) -> list[str]:
    data = json.loads(splits_path.read_text())
    ids = data.get(split) or data.get("splits", {}).get(split)
    if not ids:
        raise KeyError(f"split {split!r} not in {splits_path}")
    return [str(x) for x in ids]


def load_existing_rows(path: Path) -> dict[str, dict]:
    """Load prior CSV rows keyed by task_id (Spot / crash resume)."""
    if not path.is_file():
        return {}
    out: dict[str, dict] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            tid = (row.get("task_id") or "").strip()
            if not tid:
                continue
            usable = row.get("usable", "")
            if isinstance(usable, str):
                usable = usable.lower() == "true"
            out[tid] = {
                "task_id": tid,
                "empty_pass": row.get("empty_pass", ""),
                "oracle_pass": row.get("oracle_pass", ""),
                "error": row.get("error", ""),
                "usable": usable,
            }
    return out


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    sorted_rows = sorted(rows, key=lambda r: r["task_id"])
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for r in sorted_rows:
            w.writerow(
                {
                    "task_id": r["task_id"],
                    "empty_pass": r["empty_pass"],
                    "oracle_pass": r["oracle_pass"],
                    "error": r["error"],
                    "usable": r["usable"],
                }
            )
    tmp.replace(path)


def sync_out_to_s3(path: Path, s3_uri: str) -> None:
    """Best-effort push of CSV (+ sibling .log if present) to S3 for Spot resume."""
    if not s3_uri:
        return
    try:
        r = subprocess.run(
            ["aws", "s3", "cp", str(path), s3_uri, "--only-show-errors"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            print(f"s3_sync_warn: {(r.stderr or r.stdout or '')[-300:]}", flush=True)
    except Exception as e:
        print(f"s3_sync_warn: {type(e).__name__}:{e}", flush=True)


def pull_resume_from_s3(path: Path, s3_uri: str) -> None:
    if not s3_uri:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["aws", "s3", "cp", s3_uri, str(path), "--only-show-errors"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode == 0:
            print(f"resume_pulled {s3_uri} -> {path}", flush=True)
        else:
            # Missing object is fine on first run
            print(f"resume_pull_skip: {(r.stderr or '')[-200:]}", flush=True)
    except Exception as e:
        print(f"resume_pull_skip: {type(e).__name__}:{e}", flush=True)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=Path("data"))
    p.add_argument("--out", type=Path, default=Path("results/soundness.csv"))
    p.add_argument("--split", type=str, default="", help="smoke|dev|holdout|train_pool")
    p.add_argument("--splits-json", type=Path, default=Path("configs/splits.json"))
    p.add_argument("--task-id", action="append", default=[])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip task_ids already present in --out (and pull --s3-uri first if set)",
    )
    p.add_argument(
        "--s3-uri",
        type=str,
        default=os.environ.get("SOUNDNESS_S3_URI", ""),
        help="s3://bucket/key.csv — pull on --resume; push after each completed task",
    )
    p.add_argument(
        "--s3-every",
        type=int,
        default=1,
        help="Push to --s3-uri every N newly completed tasks (default 1)",
    )
    p.add_argument(
        "--rebuild-image",
        action="store_true",
        help="Force rebuild sandbox image (e.g. after SANDBOX_EXTRA_PIP change)",
    )
    args = p.parse_args()

    args.data = args.data.resolve()
    args.out = args.out.resolve()
    if args.splits_json:
        args.splits_json = args.splits_json.resolve()

    if shutil.which("swegemma"):
        print("note: swegemma found but docker fallback used for explicit empty/oracle rows", file=sys.stderr)

    if not docker_available():
        print("docker daemon unavailable", file=sys.stderr)
        return 2

    try:
        build_image_if_needed(args.data, force=args.rebuild_image)
    except Exception as e:
        print(f"image build failed: {e}", file=sys.stderr)
        return 2

    if args.resume and args.s3_uri:
        pull_resume_from_s3(args.out, args.s3_uri)

    tasks = load_tasks(args.data)
    only = list(args.task_id)
    if args.split:
        only = load_split_ids(args.splits_json, args.split)
    tasks = filter_tasks(tasks, only or None, args.limit)

    done: dict[str, dict] = load_existing_rows(args.out) if args.resume else {}
    if done:
        before = len(tasks)
        tasks = [t for t in tasks if task_id(t) not in done]
        print(f"resume skip={before - len(tasks)} remaining={len(tasks)} from={args.out}", flush=True)

    rows: dict[str, dict] = dict(done)
    print(
        f"soundness tasks={len(tasks)} workers={args.workers} image={IMAGE} "
        f"harness_verify={HARNESS_VERIFY_DOC_VERSION}",
        flush=True,
    )
    if not tasks:
        write_rows(args.out, list(rows.values()))
        if args.s3_uri:
            sync_out_to_s3(args.out, args.s3_uri)
    else:
        write_rows(args.out, list(rows.values()))  # ensure header exists for Spot mid-flight
        completed_since_sync = 0
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {ex.submit(check_task, args.data, t, args.timeout): task_id(t) for t in tasks}
            for fut in as_completed(futs):
                tid = futs[fut]
                try:
                    row = fut.result()
                except Exception as e:
                    row = {
                        "task_id": tid,
                        "empty_pass": "",
                        "oracle_pass": "",
                        "error": f"worker:{type(e).__name__}:{e}",
                        "usable": False,
                    }
                rows[row["task_id"]] = row
                write_rows(args.out, list(rows.values()))
                completed_since_sync += 1
                print(
                    f"{row['task_id']}: empty={row['empty_pass']} oracle={row['oracle_pass']} "
                    f"usable={row['usable']} err={row['error'][:120]}",
                    flush=True,
                )
                if args.s3_uri and completed_since_sync >= max(1, args.s3_every):
                    sync_out_to_s3(args.out, args.s3_uri)
                    completed_since_sync = 0
        if args.s3_uri:
            sync_out_to_s3(args.out, args.s3_uri)

    all_rows = list(rows.values())
    n = len(all_rows)
    usable = sum(1 for r in all_rows if r["usable"] is True or str(r["usable"]).lower() == "true")
    empty_pass_n = sum(1 for r in all_rows if str(r["empty_pass"]).lower() == "true")
    oracle_pass_n = sum(1 for r in all_rows if str(r["oracle_pass"]).lower() == "true")
    oracle_ran = sum(1 for r in all_rows if str(r["oracle_pass"]).lower() in {"true", "false"})
    empty_ran = sum(1 for r in all_rows if str(r["empty_pass"]).lower() in {"true", "false"})
    oracle_tpr = (oracle_pass_n / oracle_ran) if oracle_ran else 0.0
    empty_fpr = (empty_pass_n / empty_ran) if empty_ran else 0.0
    print(
        f"SUMMARY tasks={n} usable={usable} usable_pct={100*usable/max(n,1):.1f}% "
        f"oracle_tpr={oracle_tpr:.3f} empty_fpr={empty_fpr:.3f} out={args.out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
