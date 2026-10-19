#!/bin/bash
# P5.1a-r7d: freeze-agent BoN N=8 @0.2 after r7c zero-yield; non-rich first.
set -uo pipefail
exec >> /workspace/p51r7d_bootstrap.log 2>&1
echo "=== P5.1a-r7d bootstrap $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
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
STATUS_FILE=/workspace/p51r7d.status
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

persist_bon_artifacts() {
  local rc=0
  aws s3 sync traces/bon/ "s3://$BUCKET/results/p51a_r7d/traces/bon/" --only-show-errors || rc=1
  aws s3 cp /workspace/p51r7d_bootstrap.log "s3://$BUCKET/results/p51a_r7d/p51r7d_bootstrap.log" || rc=1
  aws s3 cp /workspace/p51r7d_bon.log "s3://$BUCKET/results/p51a_r7d/p51r7d_bon.log" || rc=1
  aws s3 cp /workspace/oracle_rich_3882.json "s3://$BUCKET/results/p51a_r7d/oracle_rich_3882.json" || rc=1
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
aws s3 cp "s3://$BUCKET/tools/scripts/rejection_sample.py" "$ROOT/scripts/rejection_sample.py"
aws s3 cp "s3://$BUCKET/tools/scripts/star_correct.py" "$ROOT/scripts/star_correct.py"
aws s3 cp "s3://$BUCKET/tools/scripts/ensure_subprocess_host_deps.py" "$ROOT/scripts/ensure_subprocess_host_deps.py"
aws s3 cp "s3://$BUCKET/tools/splits.json" "$ROOT/configs/splits.json"
aws s3 cp "s3://$BUCKET/tools/p51a_r7d_tasks.txt" "$ROOT/configs/p51a_r7d_tasks.txt"
aws s3 sync "s3://$BUCKET/tools/wheels_harness/" "$ROOT/wheels/" --only-show-errors
if [ -d /workspace/agent_override ]; then
  cp -R /workspace/agent_override/. "$ROOT/agent/"
fi
for override in rejection_sample.py star_correct.py ensure_subprocess_host_deps.py pin_subprocess_task_deps.py; do
  if [ -f "/workspace/$override" ]; then
    cp "/workspace/$override" "$ROOT/scripts/$override"
  fi
done
# Prefer S3 pin if present
aws s3 cp "s3://$BUCKET/tools/scripts/pin_subprocess_task_deps.py" "$ROOT/scripts/pin_subprocess_task_deps.py" --only-show-errors || true
mkdir -p "$ROOT/data/snapshots" "$ROOT/data/graphs" "$ROOT/data/embeddings" "$ROOT/data/wheels"
aws s3 cp "s3://$BUCKET/competition/full/tasks.jsonl" "$ROOT/data/tasks.jsonl"
aws s3 sync "s3://$BUCKET/competition/full/wheels/" "$ROOT/data/wheels/" --only-show-errors
"$BOOTSTRAP_PY" - <<'PY'
import json
from pathlib import Path

root = Path("/workspace/gemma4-p51")
tasks_txt = root / "configs" / "p51a_r7d_tasks.txt"
tasks_jsonl = root / "data" / "tasks.jsonl"
want = {line.strip() for line in tasks_txt.read_text().splitlines() if line.strip()}
sync_paths: list[str] = []
seen_graph = set()
seen_embed = set()

for line in tasks_jsonl.read_text().splitlines():
    task = json.loads(line)
    if task["instance_id"] not in want:
        continue
    sync_paths.append(f"snapshots/{task['instance_id']}.tgz")
    repo_stem = task["repo"].split("/")[-1]
    graph_key = f"graphs/{repo_stem}_{task['base_commit']}.json"
    embed_key = f"embeddings/{repo_stem}_{task['base_commit']}.npz"
    if graph_key not in seen_graph:
        sync_paths.append(graph_key)
        seen_graph.add(graph_key)
    if embed_key not in seen_embed:
        sync_paths.append(embed_key)
        seen_embed.add(embed_key)

# Always include the oracle probe snapshot used by the preflight gate.
sync_paths.append("snapshots/rich_3882.tgz")

(root / "configs" / "p51a_r7d_sync_paths.txt").write_text("\n".join(sync_paths) + "\n")
print(f"task_scoped_sync_paths={len(sync_paths)}")
PY
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  aws s3 cp "s3://$BUCKET/competition/full/$rel" "$ROOT/data/$rel" --only-show-errors
done < "$ROOT/configs/p51a_r7d_sync_paths.txt"
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
  "protobuf>=5.29.6" "fastapi[standard]<0.137.0,>=0.133.0" "starlette>=1.0.1" "openai>=2.25.0" \
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
python3 -m pip install -q --no-deps "$ROOT/data/wheels/starlette-0.46.2-py3-none-any.whl" || true
python3 -c "import pytest,starlette; print('verify_host', pytest.__version__, starlette.__version__)"

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
async def main():
  res=await verify_task(task=task, config=cfg, docker=mgr, snapshot_path=snap, agent_patch=task.patch, start_time=time.perf_counter())
  Path("/workspace/oracle_rich_3882.json").write_text(json.dumps({"resolved":bool(res.resolved),"test_exit_code":res.test_exit_code,"error":res.error}, indent=2))
  print("ORACLE_rich_3882", res.resolved, res.test_exit_code)
  return res.resolved
raise SystemExit(0 if asyncio.run(main()) else 11)
PY
ORACLE_RC=$?
echo ORACLE_RC=$ORACLE_RC
[ "$ORACLE_RC" = "0" ] || {
  aws s3 cp /workspace/p51r7d_bootstrap.log "s3://$BUCKET/results/p51a_r7d/p51r7d_bootstrap.log" || true
  aws s3 cp /workspace/oracle_rich_3882.json "s3://$BUCKET/results/p51a_r7d/oracle_rich_3882.json" || true
  set_status ORACLE_FAIL
  exit 11
}
echo BOOTSTRAP_DONE
set_status BOOTSTRAP_DONE

export P31_ROOT=/workspace/gemma4-p51 P5_ROOT=/workspace/gemma4-p51
export MODEL_PATH="$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct"
export BON_TEMPERATURE=0.2 VLLM_USE_FLASHINFER_SAMPLER=0 SWE_SANDBOX=subprocess
cd /workspace/gemma4-p51
rm -rf traces/bon; mkdir -p traces/bon
: > /workspace/p51r7d_bon.log
echo "=== BON START temp0.2 N=8 $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee /workspace/p51r7d_bon.log
set_status BON_RUNNING
set +e
"$VENV_PY" scripts/rejection_sample.py --tasks configs/p51a_r7d_tasks.txt --n 8 --agent-dir agent --out traces/bon --data data --model "$MODEL_PATH" 2>&1 | tee -a /workspace/p51r7d_bon.log
BON_RC=${PIPESTATUS[0]}
set -uo pipefail
echo "BON_RC=$BON_RC" | tee -a /workspace/p51r7d_bon.log
if [ "$BON_RC" != "0" ]; then
  echo BON_FAIL | tee -a /workspace/p51r7d_bon.log
  set_status PERSISTING_FAILURE
  if persist_bon_artifacts; then
    set_status BON_FAIL
  else
    set_status S3_PERSIST_FAIL
  fi
  exit 13
fi
echo BON_COMPLETE | tee -a /workspace/p51r7d_bon.log
set_status PERSISTING_SUCCESS
if persist_bon_artifacts; then
  set_status S3_PERSIST_DONE
else
  set_status S3_PERSIST_FAIL
  exit 14
fi
