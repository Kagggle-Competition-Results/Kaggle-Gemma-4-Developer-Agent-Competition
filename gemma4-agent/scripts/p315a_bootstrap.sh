#!/bin/bash
# P3.15a: exact public 0.18 scaffold + gemma-4-12b smoke on RunPod (S3 storage only).
set -uo pipefail
exec >> /workspace/p315a_bootstrap.log 2>&1
echo "=== P3.15a bootstrap $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
nvidia-smi | head -n 3 || true
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_IGNORE_REQUIRES_PYTHON=1
export PIP_BREAK_SYSTEM_PACKAGES=1
export VLLM_USE_FLASHINFER_SAMPLER=0
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
export HF_HOME="${HF_HOME:-/workspace/hf_home}"
export HUGGINGFACE_HUB_CACHE="${HUGGINGFACE_HUB_CACHE:-$HF_HOME/hub}"

# Prefer pod env AWS_* (injected at create). Optional local override file.
if [ -f /workspace/p51_aws_creds.env ]; then
  # shellcheck disable=SC1091
  set -a; source /workspace/p51_aws_creds.env; set +a
fi

pip install -q awscli 2>/dev/null || true
hash -r || true
BUCKET=gemma4-dev-agent-data-650825122703
ROOT=/workspace/gemma4-p315a
STATUS_FILE=/workspace/p315a.status
VENV_ROOT=/workspace/gemma4-p315a/.venv_bootstrap
BOOTSTRAP_PY=/usr/bin/python3.12
PYPI_INDEX_URL=https://pypi.org/simple
MODEL_ROOT=/models
HF_REPO=google/gemma-4-12B-it-qat-w4a16-ct
MODEL_DIR="$MODEL_ROOT/gemma-4-12B-it-qat-w4a16-ct"
mkdir -p "$ROOT" "$MODEL_ROOT" "$HF_HOME"

set_status() {
  printf '%s\n' "$1" > "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
  echo "STATUS=$1"
}

persist() {
  local rc=0
  aws s3 sync "$ROOT/results/" "s3://$BUCKET/results/p315a_12b_smoke/" --only-show-errors || rc=1
  aws s3 cp /workspace/p315a_bootstrap.log "s3://$BUCKET/results/p315a_12b_smoke/p315a_bootstrap.log" || rc=1
  aws s3 cp /workspace/p315a_smoke.log "s3://$BUCKET/results/p315a_12b_smoke/p315a_smoke.log" || rc=1
  return "$rc"
}

rm -f "$STATUS_FILE"
set_status BOOTSTRAPPING

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

# Agent = exact 0.18 + 12b (prebuilt on S3). Scripts/configs from tools.
aws s3 sync "s3://$BUCKET/tools/p315a_agent/" "$ROOT/agent/" --only-show-errors
aws s3 sync "s3://$BUCKET/tools/gemma4-agent-configs/" "$ROOT/configs/" --only-show-errors || true
aws s3 sync "s3://$BUCKET/tools/gemma4-agent-scripts/" "$ROOT/scripts/" --only-show-errors || true
aws s3 cp "s3://$BUCKET/tools/splits.json" "$ROOT/configs/splits.json"
aws s3 cp "s3://$BUCKET/tools/scripts/run_p31_smoke.py" "$ROOT/scripts/run_p31_smoke.py" || true
aws s3 cp "s3://$BUCKET/tools/scripts/ensure_subprocess_host_deps.py" "$ROOT/scripts/ensure_subprocess_host_deps.py" || true
aws s3 sync "s3://$BUCKET/tools/wheels_harness/" "$ROOT/wheels/" --only-show-errors || true
if [ -d /workspace/agent_override ]; then
  cp -R /workspace/agent_override/. "$ROOT/agent/"
fi
for override in run_p31_smoke.py ensure_subprocess_host_deps.py; do
  if [ -f "/workspace/$override" ]; then
    cp "/workspace/$override" "$ROOT/scripts/$override"
  fi
done

mkdir -p "$ROOT/data/snapshots" "$ROOT/data/graphs" "$ROOT/data/embeddings" "$ROOT/data/wheels" "$ROOT/results"
aws s3 cp "s3://$BUCKET/competition/full/tasks.jsonl" "$ROOT/data/tasks.jsonl"
aws s3 sync "s3://$BUCKET/competition/full/wheels/" "$ROOT/data/wheels/" --only-show-errors

"$BOOTSTRAP_PY" - <<'PY'
import json
from pathlib import Path

root = Path("/workspace/gemma4-p315a")
splits = json.loads((root / "configs" / "splits.json").read_text())
want = set(splits["smoke"])
tasks_jsonl = root / "data" / "tasks.jsonl"
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
(root / "configs" / "p315a_sync_paths.txt").write_text("\n".join(sync_paths) + "\n")
print(f"smoke_sync_paths={len(sync_paths)} tasks={len(want)}")
PY
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  aws s3 cp "s3://$BUCKET/competition/full/$rel" "$ROOT/data/$rel" --only-show-errors
done < "$ROOT/configs/p315a_sync_paths.txt"

# 12B weights: prefer S3 cache, else HF download then push to S3 for reuse.
set_status MODEL_SYNC
if [ ! -f "$MODEL_DIR/config.json" ]; then
  if aws s3 ls "s3://$BUCKET/models/gemma-4-12B-it-qat-w4a16-ct/config.json" >/dev/null 2>&1; then
    aws s3 sync "s3://$BUCKET/models/gemma-4-12B-it-qat-w4a16-ct/" "$MODEL_DIR/" --only-show-errors
  else
    echo "HF download $HF_REPO -> $MODEL_DIR"
    pip install -q "huggingface_hub>=0.30" || true
    python3 - <<PY
from huggingface_hub import snapshot_download
snapshot_download(repo_id="$HF_REPO", local_dir="$MODEL_DIR", local_dir_use_symlinks=False)
print("hf_download_ok")
PY
    aws s3 sync "$MODEL_DIR/" "s3://$BUCKET/models/gemma-4-12B-it-qat-w4a16-ct/" --only-show-errors || true
  fi
fi
test -f "$MODEL_DIR/config.json" || { set_status MODEL_FAIL; exit 12; }
echo "model_ok $MODEL_DIR"

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
mkdir -p /tmp/vllm_local_wheels
"$VENV_PIP" download -q --index-url "$PYPI_INDEX_URL" --no-deps --dest /tmp/vllm_local_wheels "vllm==0.31.0"
"$VENV_PIP" install -q --no-deps /tmp/vllm_local_wheels/vllm-0.31.0-*.whl
"$VENV_PIP" install -q --no-deps --ignore-requires-python "$ROOT"/wheels/*.whl || true
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" Authlib

"$VENV_PY" - <<'PY'
import vllm, swegemma, adk_submission, torch, torchvision
print("imports_ok", vllm.__version__, torch.__version__, torch.version.cuda, "tv", torchvision.__version__)
assert torch.cuda.is_available(), "no_cuda"
x=torch.zeros(1, device="cuda")
print("cuda_ok", x.device)
from transformers.image_utils import is_valid_image  # noqa: F401
import vllm.entrypoints.openai.api_server  # noqa: F401
print("vllm_api_import_ok")
PY
[ $? -eq 0 ] || { set_status VLLM_IMPORT_FAIL; exit 13; }

python3 "$ROOT/scripts/ensure_subprocess_host_deps.py" --data "$ROOT/data"
python3 -m pip install -q --no-deps "$ROOT/data/wheels/starlette-0.46.2-py3-none-any.whl" || true
python3 -c "import pytest,starlette,inline_snapshot,dirty_equals; print('verify_host_ok')"

set_status SMOKE_RUNNING
cd "$ROOT"
export P31_ROOT="$ROOT"
export MODEL_PATH="$MODEL_DIR"
export SMOKE_TICKET=P3.15a
export SMOKE_BUDGET_ID=exact_018_verra
export SMOKE_ABLATION=model_12b_only
export SMOKE_TARGET_MODEL=gemma-4-12b-it-qat-w4a16-ct
export P31_SKU="${P31_SKU:-NVIDIA_A40}"
: > /workspace/p315a_smoke.log
echo "=== SMOKE START $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee /workspace/p315a_smoke.log
"$VENV_PY" scripts/run_p31_smoke.py 2>&1 | tee -a /workspace/p315a_smoke.log
SMOKE_RC=${PIPESTATUS[0]}
echo "SMOKE_RC=$SMOKE_RC" | tee -a /workspace/p315a_smoke.log

set_status PERSISTING
persist || true
if [ "$SMOKE_RC" -ne 0 ]; then
  set_status SMOKE_FAIL
  echo S3_PERSIST_DONE
  exit "$SMOKE_RC"
fi
set_status SMOKE_COMPLETE
echo S3_PERSIST_DONE
echo "=== P3.15a done $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
