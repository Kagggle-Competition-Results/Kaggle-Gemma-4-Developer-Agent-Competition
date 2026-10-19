#!/bin/bash
# P5.1a-r8: exact P3.14a agent BoN N=16 @0.75 wave1 (Gate1A retarget).
set -uo pipefail
exec >> /workspace/p52_wave2_bootstrap.log 2>&1
echo "=== P5.2-wave2 bootstrap $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
nvidia-smi | head -n 3 || true
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_IGNORE_REQUIRES_PYTHON=1
export PIP_BREAK_SYSTEM_PACKAGES=1
export VLLM_USE_FLASHINFER_SAMPLER=0
export AWS_DEFAULT_REGION=us-east-1
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
if [ -f /workspace/p51_aws_creds.env ]; then
  # Reuse the local operator creds file when the pod starts without AWS env.
  # This keeps S3 syncs deterministic across restarts.
  # shellcheck disable=SC1091
  set -a; source /workspace/p51_aws_creds.env; set +a
fi
pip install -q awscli 2>/dev/null || true
hash -r || true
BUCKET=gemma4-dev-agent-data-650825122703
ROOT=/workspace/gemma4-p51
STATUS_FILE=/workspace/p52_wave2.status
VENV_ROOT=/workspace/gemma4-p51/.venv_bootstrap
BOOTSTRAP_PY=/usr/bin/python3.12
PYPI_INDEX_URL=https://pypi.org/simple
MODEL_ROOT=/models
mkdir -p "$ROOT" "$MODEL_ROOT"

set_status() {
  printf '%s\n' "$1" > "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
  echo "STATUS=$1"
}

persist_p52_early() {
  local rc=0
  aws s3 sync traces/bon/ "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" --only-show-errors || rc=1
  aws s3 cp /workspace/p52_wave2_bootstrap.log "s3://$BUCKET/results/p52_wave2_sieve/p52_wave2_bootstrap.log" || rc=1
  aws s3 cp /workspace/p51r8_p314a_bon.log "s3://$BUCKET/results/p52_wave2_sieve/p51r8_p314a_bon.log" || rc=1
  aws s3 cp /workspace/oracle_rich_3882.json "s3://$BUCKET/results/p52_wave2_sieve/oracle_rich_3882.json" || rc=1
  return "$rc"
}

rm -f "$STATUS_FILE"
set_status BOOTSTRAPPING

# Pick torch CUDA index from driver (12.8→cu128, ≥13→cu130). r5 failed cu130-on-12.8.
python3 - <<'PY'
import re, subprocess
from pathlib import Path
out=subprocess.check_output(["nvidia-smi"], text=True)
m=re.search(r"CUDA Version:\s*([0-9.]+)", out)
ver=float(m.group(1)) if m else 0.0
idx = "cu130" if ver >= 13.0 else "cu128"
Path("/tmp/torch_cu_index.env").write_text(f"export TORCH_CU_INDEX={idx}\nexport CUDA_DRIVER_VER={ver}\n")
print(f"cuda_driver={ver} torch_index={idx}")
PY
# shellcheck disable=SC1091
source /tmp/torch_cu_index.env

aws s3 sync "s3://$BUCKET/tools/gemma4-agent-agent/" "$ROOT/agent/" --only-show-errors || true
aws s3 sync "s3://$BUCKET/tools/gemma4-agent-configs/" "$ROOT/configs/" --only-show-errors || true
aws s3 sync "s3://$BUCKET/tools/gemma4-agent-scripts/" "$ROOT/scripts/" --only-show-errors || true

# Prefer locally uploaded overrides (launch scp) over stale S3 tools copies.
for f in patch_swegemma_subprocess.py rejection_sample.py star_correct.py host_drift.py gold_gate_wave1.py ensure_subprocess_host_deps.py; do
  if [ -f "/workspace/$f" ]; then
    cp -f "/workspace/$f" "$ROOT/scripts/$f"
  fi
done
if [ -d /workspace/agent_override ] && [ -f /workspace/agent_override/agent.yaml ]; then
  rm -rf "$ROOT/agent"
  mkdir -p "$ROOT/agent"
  cp -a /workspace/agent_override/. "$ROOT/agent/"
  echo "agent_override_installed"
fi
for f in p52_wave2_tasks_requests_first.txt p51f_star_near_miss_tasks.txt splits.json; do
  if [ -f "/workspace/$f" ]; then
    mkdir -p "$ROOT/configs"
    cp -f "/workspace/$f" "$ROOT/configs/$f"
  fi
done

# Prefer local workspace overrides (already copied above); refresh from S3 only if missing.
for f in rejection_sample.py star_correct.py ensure_subprocess_host_deps.py; do
  if [ ! -f "$ROOT/scripts/$f" ]; then
    aws s3 cp "s3://$BUCKET/tools/scripts/$f" "$ROOT/scripts/$f" --only-show-errors || true
  fi
done
if [ ! -f "$ROOT/configs/splits.json" ]; then
  aws s3 cp "s3://$BUCKET/tools/splits.json" "$ROOT/configs/splits.json" --only-show-errors || true
fi
# Also stage splits locally if provided (G4).
if [ -f /workspace/splits.json ]; then
  cp /workspace/splits.json "$ROOT/configs/splits.json"
fi
aws s3 sync "s3://$BUCKET/tools/wheels_harness/" "$ROOT/wheels/" --only-show-errors
if [ -d /workspace/agent_override ]; then
  cp -R /workspace/agent_override/. "$ROOT/agent/"
fi
# Hard-require exact P3.14a package (8min/28calls/Verra prompt) — refuse freeze agent.
python3 - <<'CHK'
from pathlib import Path
root = Path("/workspace/gemma4-p51/agent")
sys_md = (root / "prompts" / "system.md").read_text()
ev_txt = (root / "eval_config.yaml").read_text()
n_lines = len(sys_md.splitlines())
assert "max_time_minutes: 8" in ev_txt, ev_txt
assert "max_tool_calls: 28" in ev_txt, ev_txt
assert "timeout_seconds: 240" in ev_txt, ev_txt
assert n_lines >= 40, f"system.md too short for P3.14a: {n_lines}"
assert "Hard Context Rules" not in sys_md, "P3.14b hard-context crept in"
print("P314A_AGENT_OK", n_lines)
CHK
for override in rejection_sample.py star_correct.py ensure_subprocess_host_deps.py pin_subprocess_task_deps.py \
  patch_swegemma_subprocess.py gold_gate_wave1.py host_drift.py render_trajectories.py; do
  if [ -f "/workspace/$override" ]; then
    cp "/workspace/$override" "$ROOT/scripts/$override"
  fi
done
# Hard-require host_drift (rejection_sample imports it); fail early rather than BON_FAIL+watchdog kill.
if [ ! -f "$ROOT/scripts/host_drift.py" ]; then
  echo "FATAL: missing $ROOT/scripts/host_drift.py (copy to /workspace/ before bootstrap)" >&2
  set_status MISSING_HOST_DRIFT
  exit 16
fi
# Prefer S3 pin if present
aws s3 cp "s3://$BUCKET/tools/scripts/pin_subprocess_task_deps.py" "$ROOT/scripts/pin_subprocess_task_deps.py" --only-show-errors || true
mkdir -p "$ROOT/data/snapshots" "$ROOT/data/graphs" "$ROOT/data/embeddings" "$ROOT/data/wheels" "$ROOT/configs"
aws s3 cp "s3://$BUCKET/competition/full/tasks.jsonl" "$ROOT/data/tasks.jsonl"
aws s3 sync "s3://$BUCKET/competition/full/wheels/" "$ROOT/data/wheels/" --only-show-errors
# Wave2 gold+BoN (59) ∪ STaR-5 near-miss ∪ oracle probe rich_3882
"$BOOTSTRAP_PY" - <<'PY'
import json
from pathlib import Path

root = Path("/workspace/gemma4-p51")
want: set[str] = set()
for name in ("p52_wave2_tasks_requests_first.txt", "p51f_star_near_miss_tasks.txt"):
    p = root / "configs" / name
    if p.exists():
        want |= {line.strip() for line in p.read_text().splitlines() if line.strip()}
want.add("rich_3882")
tasks_jsonl = root / "data" / "tasks.jsonl"
sync_paths: list[str] = []
seen_graph = set()
seen_embed = set()
seen_snap = set()

for line in tasks_jsonl.read_text().splitlines():
    task = json.loads(line)
    if task["instance_id"] not in want:
        continue
    snap = f"snapshots/{task['instance_id']}.tgz"
    if snap not in seen_snap:
        sync_paths.append(snap)
        seen_snap.add(snap)
    repo_stem = task["repo"].split("/")[-1]
    graph_key = f"graphs/{repo_stem}_{task['base_commit']}.json"
    embed_key = f"embeddings/{repo_stem}_{task['base_commit']}.npz"
    if graph_key not in seen_graph:
        sync_paths.append(graph_key)
        seen_graph.add(graph_key)
    if embed_key not in seen_embed:
        sync_paths.append(embed_key)
        seen_embed.add(embed_key)

if "snapshots/rich_3882.tgz" not in seen_snap:
    sync_paths.append("snapshots/rich_3882.tgz")

(root / "configs" / "p52_wave2_sync_paths.txt").write_text("\n".join(sync_paths) + "\n")
print(f"task_scoped_sync_paths={len(sync_paths)} want_tasks={len(want)}")
PY
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  aws s3 cp "s3://$BUCKET/competition/full/$rel" "$ROOT/data/$rel" --only-show-errors
done < "$ROOT/configs/p52_wave2_sync_paths.txt"
if [ ! -f "$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct/config.json" ]; then
  aws s3 sync "s3://$BUCKET/models/gemma-4-31B-it-qat-w4a16-ct/" \
    "$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct/" --only-show-errors
fi

if [ ! -x "$BOOTSTRAP_PY" ]; then
  BOOTSTRAP_PY=/usr/bin/python3
fi
if [ -x "$VENV_ROOT/bin/python" ] && ! "$VENV_ROOT/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  rm -rf "$VENV_ROOT"
fi
if [ ! -x "$VENV_ROOT/bin/python" ]; then
  "$BOOTSTRAP_PY" -m venv "$VENV_ROOT"
fi
VENV_PY="$VENV_ROOT/bin/python"
VENV_PIP="$VENV_ROOT/bin/pip"
echo "bootstrap_venv=$VENV_ROOT"
"$VENV_PY" --version
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" -U pip setuptools wheel
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" -U "numpy<2.3" "scipy<1.15" pandas || true
# shellcheck disable=SC1091
source /tmp/torch_cu_index.env
TORCH_IDX_URL="https://download.pytorch.org/whl/${TORCH_CU_INDEX}"
echo "pip_torch_index=$TORCH_IDX_URL"
mkdir -p /tmp/torch_probe /tmp/cuda_probe
"$VENV_PIP" download -q --no-deps --index-url "$TORCH_IDX_URL" --dest /tmp/torch_probe \
  "torch==2.13.0" "torchaudio==2.11.0" "torchvision==0.28.0"
"$VENV_PIP" install -q --force-reinstall /tmp/torch_probe/torch-2.13.0+*.whl /tmp/torch_probe/torchaudio-2.11.0+*.whl
"$VENV_PIP" download -q --no-deps --index-url "$PYPI_INDEX_URL" --dest /tmp/cuda_probe \
  "cuda-python==13.4.1" "cuda-bindings==13.4.1"
"$VENV_PIP" install -q --force-reinstall /tmp/cuda_probe/cuda_bindings-13.4.1-*.whl /tmp/cuda_probe/cuda_python-13.4.1-*.whl
"$VENV_PIP" install -q --no-deps --force-reinstall /tmp/torch_probe/torchvision-0.28.0+*.whl
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" litellm pyyaml httpx pydantic tenacity \
  google-genai google-auth Authlib requests oauthlib docker \
  opentelemetry-api opentelemetry-sdk nest_asyncio aiohttp cloudpickle \
  google-cloud-storage sqlalchemy aiosqlite
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" \
  regex cachetools psutil sentencepiece tqdm blake3 py-cpuinfo \
  "transformers<5.18.0,>=5.10.4" "huggingface_hub>=1.31.0" "tokenizers>=0.21.1" "safetensors>=0.6.2" \
  "protobuf>=5.29.6" "fastapi[standard]<0.137.0,>=0.133.0" "openai>=2.25.0" \
  "prometheus_client>=0.18.0" "prometheus-fastapi-instrumentator>=8.0.0" "tiktoken>=0.6.0" \
  "lm-format-enforcer==0.11.3" "llguidance>=1.7.0,<1.8.0" "outlines_core==0.2.14" "lark==1.2.2" \
  "xgrammar==0.2.7" partial-json-parser "jsonschema>=4.23.0" "pyzmq>=25.0.0" msgspec \
  "mistral_common[image]>=1.11.6" "opencv-python-headless>=4.13.0" "six>=1.16.0" \
  "setuptools>=77.0.3,<81.0.0" einops "compressed-tensors==0.17.0" "depyf==0.20.0" \
  watchfiles python-json-logger ninja pybase64 cbor2 ijson setproctitle "oss-harmony>=0.0.11" \
  "anthropic>=0.71.0" "model-hosting-container-standards>=0.1.14,<1.0.0" "mcp>=2.0.0,<3.0.0" \
  "opentelemetry-exporter-otlp>=1.27.0" "opentelemetry-semantic-conventions-ai>=0.4.1" \
  "numba==0.65.0" "flashinfer-python==0.7.0.post1" "apache-tvm-ffi==0.1.11" "tilelang==0.1.12" \
  "nvidia-cudnn-frontend>=1.19.1" "nvtx==0.2.15" "fastsafetensors>=0.3.3" "instanttensor>=0.1.9" \
  "nvidia-cutlass-dsl[cu13]==4.7.1" "quack-kernels==0.6.5" "tokenspeed-mla==0.1.8" \
  "humming-kernels[cu13]==0.1.16"
LOCAL_VLLM_DIR=$(find /tmp -maxdepth 1 -type d -name 'pip-unpack-*' -o -path /tmp/vllm_local_wheels 2>/dev/null | sort | tail -1)
if [ -n "${LOCAL_VLLM_DIR:-}" ] && compgen -G "${LOCAL_VLLM_DIR}/vllm-0.31.0-*.whl" >/dev/null; then
  mapfile -t VLLM_LOCAL_WHEELS < <(find "$LOCAL_VLLM_DIR" -maxdepth 1 -name 'vllm-0.31.0-*.whl' | sort)
  "$VENV_PIP" install -q --no-deps "${VLLM_LOCAL_WHEELS[@]}"
else
  mkdir -p /tmp/vllm_local_wheels
  "$VENV_PIP" download -q --index-url "$PYPI_INDEX_URL" --no-deps --dest /tmp/vllm_local_wheels "vllm==0.31.0"
  "$VENV_PIP" install -q --no-deps /tmp/vllm_local_wheels/vllm-0.31.0-*.whl
fi
"$VENV_PIP" install -q --no-deps --ignore-requires-python "$ROOT"/wheels/*.whl || true
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" Authlib

"$VENV_PY" - <<'PY'
import vllm, swegemma, adk_submission, torch, torchvision
print("imports_ok", vllm.__version__, torch.__version__, torch.version.cuda, "tv", torchvision.__version__)
assert torch.cuda.is_available(), "no_cuda"
x=torch.zeros(1, device="cuda")
print("cuda_ok", x.device)
# Preflight the import path that killed r5b
from transformers.image_utils import is_valid_image  # noqa: F401
import vllm.entrypoints.openai.api_server  # noqa: F401
print("vllm_api_import_ok")
PY
[ $? -eq 0 ] || { set_status VLLM_IMPORT_FAIL; exit 13; }

# Keep verify deps on the system interpreter because subprocess harness uses host packages.
python3 "$ROOT/scripts/ensure_subprocess_host_deps.py" --data "$ROOT/data"
# CRITICAL: pin starlette on BOTH system python and harvest venv (system_site_packages
# inherits the venv that created SubprocessManager — usually .venv_bootstrap).
# Never leave starlette 1.x from vllm/fastapi extras.
for _py in python3 "$VENV_PY"; do
  "$_py" -m pip install -q --no-deps --force-reinstall \
    "$ROOT/data/wheels/starlette-0.46.2-py3-none-any.whl" || true
  "$_py" -m pip install -q --index-url "$PYPI_INDEX_URL" "pytest-timeout==2.1.0" || true
done
# Src-layout + P5.1e V2 oracle guards: PYTHONPATH=src, DeprecationWarning ignore,
# requests flake -k filter, non-empty oracle error summary (STaR G11).
"$VENV_PY" "$ROOT/scripts/patch_swegemma_subprocess.py" || true
python3 "$ROOT/scripts/patch_swegemma_subprocess.py" || true
# Hard-require V2 markers so Wave2 cannot BON_FAIL on requests env flake / empty errors.
"$VENV_PY" - <<'PY' || { set_status PATCH_V2_FAIL; exit 14; }
from pathlib import Path
import swegemma
root = Path(swegemma.__file__).resolve().parent
ver = (root / "harness" / "verification.py").read_text()
sub = (root / "sandbox" / "subprocess.py").read_text()
need = [
    ("GEMMA4_ORACLE_FLAKE_V2", ver),
    ("not test_redirecting_to_bad_url", ver),
    ("-W ignore::DeprecationWarning", ver),
    ("_gemma4_summarize_pytest_failure", ver),
    ("GEMMA4_SRCPATH_PATCH_V1", sub),
]
missing = [name for name, text in need if name not in text]
if missing:
    raise SystemExit(f"patch_swegemma_subprocess V2 incomplete: {missing}")
print("patch_v2_ok", root)
PY
if [ ! -f "$ROOT/scripts/host_drift.py" ]; then
  echo "FATAL: missing host_drift.py after sync" >&2
  set_status HOST_DRIFT_MISSING
  exit 15
fi
"$VENV_PY" -c "import pytest,starlette; print('verify_venv', pytest.__version__, starlette.__version__)"
python3 -c "import pytest,starlette; print('verify_host', pytest.__version__, starlette.__version__)"
mkdir -p "$ROOT/results/p52_wave2_sieve"

# Hard-require oracle probe snapshot (prior pod ORACLE_FAIL was exit=-1 from missing tgz).
if [ ! -f "$ROOT/data/snapshots/rich_3882.tgz" ]; then
  echo "rich_3882.tgz missing — fetching"
  aws s3 cp "s3://$BUCKET/competition/full/snapshots/rich_3882.tgz" \
    "$ROOT/data/snapshots/rich_3882.tgz" --only-show-errors
fi
SNAP_N=$(ls "$ROOT/data/snapshots" 2>/dev/null | wc -l | tr -d ' ')
echo "pre_oracle_snapshots=${SNAP_N}"
if [ ! -f "$ROOT/data/snapshots/rich_3882.tgz" ]; then
  set_status ORACLE_FAIL
  echo "FATAL: rich_3882.tgz still missing after fetch" >&2
  exit 11
fi

"$VENV_PY" - <<'PY'
import asyncio, json, time
from pathlib import Path
from swegemma.config import EvalConfig
from swegemma.harness.verification import verify_task
from swegemma.models import load_tasks
from swegemma.sandbox.subprocess import SubprocessManager
root=Path("/workspace/gemma4-p51"); data=root/"data"
tasks={t.instance_id:t for t in load_tasks(data/"tasks.jsonl")}
task=tasks["rich_3882"]
class Dummy:
    def get(self,*a,**k): raise KeyError("no model")
cfg=EvalConfig(tasks_path=data/"tasks.jsonl", snapshots_dir=data/"snapshots",
  results_dir=Path("/tmp/oracle_rich_3882"), submission_dir=root/"agent", models=Dummy(),
  sandbox="subprocess", timeout_seconds=300, max_time_minutes=4, max_tool_calls=60, wheels_dir=data/"wheels")
cfg.results_dir.mkdir(parents=True, exist_ok=True)
mgr=SubprocessManager(timeout_seconds=300, system_site_packages=True)
snap=data/"snapshots"/"rich_3882.tgz"
assert snap.is_file() and snap.stat().st_size > 1000, f"bad_snap {snap}"
async def main():
  res=await verify_task(task=task, config=cfg, docker=mgr, snapshot_path=snap, agent_patch=task.patch, start_time=time.perf_counter())
  Path("/workspace/oracle_rich_3882.json").write_text(json.dumps({"resolved":bool(res.resolved),"test_exit_code":res.test_exit_code,"error":(res.error or "")[:800]}, indent=2))
  print("ORACLE_rich_3882", res.resolved, res.test_exit_code)
  return res.resolved
raise SystemExit(0 if asyncio.run(main()) else 11)
PY
ORACLE_RC=$?
echo ORACLE_RC=$ORACLE_RC
[ "$ORACLE_RC" = "0" ] || {
  aws s3 cp /workspace/p52_wave2_bootstrap.log "s3://$BUCKET/results/p52_wave2_sieve/p52_wave2_bootstrap.log" || true
  aws s3 cp /workspace/oracle_rich_3882.json "s3://$BUCKET/results/p52_wave2_sieve/oracle_rich_3882.json" || true
  set_status ORACLE_FAIL
  exit 11
}


# --- P5.2 pipeline: Wave2 gold (soft) → STaR-5 seeded → Stage-1 N=4 sieve ---
WAVE2_TASKS_FILE="$ROOT/configs/p52_wave2_tasks_requests_first.txt"
STAR_TASKS_FILE="$ROOT/configs/p51f_star_near_miss_tasks.txt"
mkdir -p "$ROOT/results/p52_wave2_sieve" traces/star traces/bon

# Pod-loss resume: skip gold/STAR; restore BoN from S3 (never wipe traces/bon).
if [ "${RESUME_BON_ONLY:-0}" = "1" ]; then
  echo "RESUME_BON_ONLY=1 after oracle — skip gold/STAR, hand off to resume script"
  if [ -f /workspace/p52_wave2_resume_bon_only.sh ]; then
    chmod +x /workspace/p52_wave2_resume_bon_only.sh
    echo BOOTSTRAP_DONE
    set_status BOOTSTRAP_DONE
    exec bash /workspace/p52_wave2_resume_bon_only.sh
  fi
  echo "RESUME_BON_ONLY missing resume script — abort before gold/STAR/BON wipe"
  set_status RESUME_FAIL_NO_SCRIPT
  exit 18
fi

set_status GOLD_GATE
"$VENV_PY" "$ROOT/scripts/gold_gate_wave1.py" \
  --root "$ROOT" \
  --tasks "$WAVE2_TASKS_FILE" \
  --out "$ROOT/results/p52_wave2_sieve/gold_gate_wave2_a40.json" || true
GOLD_RC=$?
echo GOLD_GATE_RC=$GOLD_RC
aws s3 cp "$ROOT/results/p52_wave2_sieve/gold_gate_wave2_a40.json" \
  "s3://$BUCKET/results/p52_wave2_sieve/gold_gate_wave2_a40.json" --only-show-errors || true

# Build live BoN task list = gold PASS only (exclude gold fails; do not abort)
"$VENV_PY" - <<'PY'
import json
from pathlib import Path
root = Path("/workspace/gemma4-p51")
rep = json.loads((root/"results/p52_wave2_sieve/gold_gate_wave2_a40.json").read_text())
ordered = [ln.strip() for ln in (root/"configs/p52_wave2_tasks_requests_first.txt").read_text().splitlines() if ln.strip()]
pass_ids = {r["task_id"] for r in rep.get("results") or [] if r.get("resolved")}
fail_ids = [t for t in ordered if t not in pass_ids]
live = [t for t in ordered if t in pass_ids]
out = root/"configs/p52_wave2_bon_live.txt"
out.write_text("\n".join(live) + ("\n" if live else ""))
meta = {
    "n_ordered": len(ordered),
    "n_gold_pass": len(live),
    "n_gold_fail": len(fail_ids),
    "gold_fail": fail_ids,
    "live_head": live[:10],
}
(root/"results/p52_wave2_sieve/gold_gate_live_filter.json").write_text(json.dumps(meta, indent=2)+"\n")
print("GOLD_LIVE", meta["n_gold_pass"], "/", meta["n_ordered"], "fail", meta["n_gold_fail"])
if not live:
    raise SystemExit(16)
PY
LIVE_RC=$?
echo LIVE_FILTER_RC=$LIVE_RC
[ "$LIVE_RC" = "0" ] || { set_status GOLD_GATE_EMPTY; exit 16; }
aws s3 cp "$ROOT/results/p52_wave2_sieve/gold_gate_live_filter.json" \
  "s3://$BUCKET/results/p52_wave2_sieve/gold_gate_live_filter.json" --only-show-errors || true

"$VENV_PY" - <<'G4'
import json
from pathlib import Path
root = Path("/workspace/gemma4-p51")
sp = json.loads((root/"configs"/"splits.json").read_text())
ban = set(sp.get("holdout") or []) | set(sp.get("smoke") or [])
bon_path = root/"configs"/"p52_wave2_bon_live.txt"
star_path = root/"configs"/"p51f_star_near_miss_tasks.txt"
tasks = [ln.strip() for ln in bon_path.read_text().splitlines() if ln.strip()]
star = [ln.strip() for ln in star_path.read_text().splitlines() if ln.strip()]
bon_leaks = [t for t in tasks if t in ban]
star_leaks = [t for t in star if t in ban]
tasks2 = [t for t in tasks if t not in ban]
star2 = [t for t in star if t not in ban]
bon_path.write_text("\n".join(tasks2) + ("\n" if tasks2 else ""))
star_path.write_text("\n".join(star2) + ("\n" if star2 else ""))
meta = {"bon_before": len(tasks), "bon_after": len(tasks2), "bon_leaks": bon_leaks,
        "star_before": len(star), "star_after": len(star2), "star_leaks": star_leaks}
(root/"results/p52_wave2_sieve/g4_filter_live.json").write_text(json.dumps(meta, indent=2)+"\n")
print("G4_FILTER", meta)
if not tasks2:
    raise SystemExit(12)
G4
G4_RC=$?
echo G4_RC=$G4_RC
[ "$G4_RC" = "0" ] || { set_status G4_FAIL; exit 12; }

echo BOOTSTRAP_DONE
set_status BOOTSTRAP_DONE

export P31_ROOT=/workspace/gemma4-p51 P5_ROOT=/workspace/gemma4-p51
export MODEL_PATH="$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct"
export BON_TEMPERATURE=0.75 VLLM_USE_FLASHINFER_SAMPLER=0 SWE_SANDBOX=subprocess
export BON_CONCURRENCY=2 BON_COMMAND_TIMEOUT=75
cd /workspace/gemma4-p51

if [ -d /workspace/star_seed ]; then
  mkdir -p traces/star
  cp -a /workspace/star_seed/. traces/star/ || true
fi

persist_p52() {
  local rc=0
  aws s3 sync traces/star/ "s3://$BUCKET/results/p52_wave2_sieve/traces/star/" --only-show-errors || rc=1
  aws s3 sync traces/bon/ "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" --only-show-errors || rc=1
  aws s3 sync results/p52_wave2_sieve/ "s3://$BUCKET/results/p52_wave2_sieve/" --only-show-errors || rc=1
  aws s3 cp /workspace/p52_wave2_bootstrap.log "s3://$BUCKET/results/p52_wave2_sieve/p52_wave2_bootstrap.log" --only-show-errors || rc=1
  aws s3 cp /workspace/p52_wave2_pipeline.log "s3://$BUCKET/results/p52_wave2_sieve/p52_wave2_pipeline.log" --only-show-errors || true
  return "$rc"
}

: > /workspace/p52_wave2_pipeline.log
echo "=== STAR START $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a /workspace/p52_wave2_pipeline.log
set_status STAR_RUNNING
set +e
"$VENV_PY" scripts/star_correct.py \
  --tasks "$STAR_TASKS_FILE" \
  --agent-dir agent \
  --max-retries 1 \
  --error-tail-lines 20 \
  --out traces/star \
  --data data \
  --model "$MODEL_PATH" 2>&1 | tee -a /workspace/p52_wave2_pipeline.log
STAR_RC=${PIPESTATUS[0]}
set -uo pipefail
echo "STAR_RC=$STAR_RC" | tee -a /workspace/p52_wave2_pipeline.log
persist_p52 || true
if [ "$STAR_RC" != "0" ]; then
  echo STAR_FAIL_CONTINUE | tee -a /workspace/p52_wave2_pipeline.log
fi
echo STAR_COMPLETE | tee -a /workspace/p52_wave2_pipeline.log

rm -rf traces/bon; mkdir -p traces/bon
echo "=== BON START N=4 concurrency=2 early_stop=2 temp=0.75 $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a /workspace/p52_wave2_pipeline.log
echo "LIVE_TASKS=$(wc -l < "$ROOT/configs/p52_wave2_bon_live.txt")" | tee -a /workspace/p52_wave2_pipeline.log
set_status BON_RUNNING
set +e
"$VENV_PY" scripts/rejection_sample.py \
  --tasks "$ROOT/configs/p52_wave2_bon_live.txt" \
  --n 4 \
  --concurrency 2 \
  --command-timeout 75 \
  --agent-dir agent \
  --out traces/bon \
  --data data \
  --model "$MODEL_PATH" 2>&1 | tee -a /workspace/p52_wave2_pipeline.log
BON_RC=${PIPESTATUS[0]}
set -uo pipefail
echo "BON_RC=$BON_RC" | tee -a /workspace/p52_wave2_pipeline.log
if [ "$BON_RC" != "0" ]; then
  echo BON_FAIL | tee -a /workspace/p52_wave2_pipeline.log
  set_status PERSISTING_FAILURE
  if persist_p52; then set_status BON_FAIL; else set_status S3_PERSIST_FAIL; fi
  exit 13
fi
echo BON_COMPLETE | tee -a /workspace/p52_wave2_pipeline.log
set_status PERSISTING_SUCCESS
if persist_p52; then
  echo S3_PERSIST_DONE | tee -a /workspace/p52_wave2_pipeline.log
  set_status S3_PERSIST_DONE
else
  set_status S3_PERSIST_FAIL
  exit 14
fi
