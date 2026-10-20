#!/bin/bash
# P5.6c / P6.1 — 10-task smoke on harvest_agent_p314a + master_swe_adapter (Plan B LoRA).
# Called from p56c_train_bootstrap after GATE_DONE. Does not touch frozen agent/.
set -uo pipefail
BUCKET=gemma4-dev-agent-data-650825122703
ROOT=/workspace/gemma4-p56c
OUT="$ROOT/results/p56c_full_edit_pipeline"
MASTER="$OUT/master_swe_adapter"
SMOKE_ROOT=/workspace/p56c_smoke
VENV_ROOT="$SMOKE_ROOT/.venv_bootstrap"
BOOTSTRAP_PY=/usr/bin/python3.12
PYPI_INDEX_URL=https://pypi.org/simple
export VLLM_USE_FLASHINFER_SAMPLER=0
export AWS_DEFAULT_REGION=us-east-1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_IGNORE_REQUIRES_PYTHON=1
export PIP_BREAK_SYSTEM_PACKAGES=1
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"

if [ -f /workspace/p51_aws_creds.env ]; then
  # shellcheck disable=SC1091
  set -a; source /workspace/p51_aws_creds.env; set +a
fi

test -f "$MASTER/adapter_model.safetensors" || { echo "missing master adapter"; exit 2; }
test -f /workspace/p56c_harvest_agent_p314a.tgz -o -d /workspace/harvest_agent_p314a || {
  echo "missing harvest agent tarball"; exit 3;
}

mkdir -p "$SMOKE_ROOT" "$SMOKE_ROOT/results" "$SMOKE_ROOT/data/snapshots" \
  "$SMOKE_ROOT/data/graphs" "$SMOKE_ROOT/data/embeddings" "$SMOKE_ROOT/data/wheels" \
  "$SMOKE_ROOT/scripts" "$SMOKE_ROOT/configs" "$SMOKE_ROOT/wheels"

# Working agent copy (NOT gemma4-agent/agent/)
rm -rf "$SMOKE_ROOT/agent"
if [ -f /workspace/p56c_harvest_agent_p314a.tgz ]; then
  tar -xzf /workspace/p56c_harvest_agent_p314a.tgz -C "$SMOKE_ROOT"
  mv "$SMOKE_ROOT/harvest_agent_p314a" "$SMOKE_ROOT/agent"
elif [ -d /workspace/harvest_agent_p314a ]; then
  cp -a /workspace/harvest_agent_p314a "$SMOKE_ROOT/agent"
fi
# Strip macOS AppleDouble / resource-fork junk (._*) — ADK rejects extension ''.
find "$SMOKE_ROOT/agent" -name '._*' -delete 2>/dev/null || true
find "$SMOKE_ROOT" -maxdepth 2 -name '._*' -delete 2>/dev/null || true
mkdir -p "$SMOKE_ROOT/agent/adapters"
rm -rf "$SMOKE_ROOT/agent/adapters/master_swe_adapter"
cp -a "$MASTER" "$SMOKE_ROOT/agent/adapters/master_swe_adapter"
echo "SMOKE_ADAPTER_MOUNTED PlanB enable_lora path=agent/adapters/master_swe_adapter"

cp /workspace/splits.json "$SMOKE_ROOT/configs/splits.json" 2>/dev/null || \
  cp "$ROOT/configs/splits.json" "$SMOKE_ROOT/configs/splits.json"
for f in run_p56c_smoke.py ensure_subprocess_host_deps.py patch_swegemma_subprocess.py; do
  if [ -f "/workspace/$f" ]; then
    cp "/workspace/$f" "$SMOKE_ROOT/scripts/$f"
  elif [ -f "$ROOT/scripts/$f" ]; then
    cp "$ROOT/scripts/$f" "$SMOKE_ROOT/scripts/$f"
  fi
done

# Smoke-only task assets (G4: smoke split only)
aws s3 cp "s3://$BUCKET/competition/full/tasks.jsonl" "$SMOKE_ROOT/data/tasks.jsonl" --only-show-errors
aws s3 sync "s3://$BUCKET/competition/full/wheels/" "$SMOKE_ROOT/data/wheels/" --only-show-errors
aws s3 sync "s3://$BUCKET/tools/wheels_harness/" "$SMOKE_ROOT/wheels/" --only-show-errors || true
python3 - <<'PY'
import json
from pathlib import Path
root = Path("/workspace/p56c_smoke")
splits = json.loads((root / "configs" / "splits.json").read_text())
smoke = set(splits["smoke"])
holdout = set(splits.get("holdout") or [])
assert not (smoke & holdout), "G4 violation: smoke∩holdout"
tasks_path = root / "data" / "tasks.jsonl"
needed = []
for line in tasks_path.read_text().splitlines():
    if not line.strip():
        continue
    obj = json.loads(line)
    tid = obj.get("instance_id") or obj.get("task_id")
    if tid in smoke:
        needed.append(obj)
assert len(needed) == 10, len(needed)
# Write filtered tasks for clarity
(root / "data" / "tasks_smoke.jsonl").write_text(
    "\n".join(json.dumps(x) for x in needed) + "\n"
)
print("SMOKE_TASKS", sorted(smoke))
# Pull snapshots/graphs/embeddings for smoke tasks only
import subprocess, os
bucket = "gemma4-dev-agent-data-650825122703"
for obj in needed:
    tid = obj["instance_id"]
    snap = f"snapshots/{tid}.tgz"
    subprocess.check_call(
        ["aws", "s3", "cp", f"s3://{bucket}/competition/full/{snap}", f"{root}/data/{snap}", "--only-show-errors"]
    )
    bc = obj.get("base_commit") or ""
    repo = (obj.get("repo") or tid.split("_")[0]).replace("/", "__")
    # graphs/embeddings naming: <repo>_<commit40>.json / .npz
    if bc:
        stem = f"{repo}_{bc[:40]}" if len(bc) >= 40 else f"{repo}_{bc}"
        for kind, ext in (("graphs", "json"), ("embeddings", "npz")):
            rel = f"{kind}/{stem}.{ext}"
            dst = root / "data" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            subprocess.call(
                ["aws", "s3", "cp", f"s3://{bucket}/competition/full/{rel}", str(dst), "--only-show-errors"]
            )
print("SMOKE_DATA_OK")
PY

# Fresh venv for vLLM (train stack may conflict)
if [ -x "$VENV_ROOT/bin/python" ] && ! "$VENV_ROOT/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  rm -rf "$VENV_ROOT"
fi
if [ ! -x "$VENV_ROOT/bin/python" ]; then
  "$BOOTSTRAP_PY" -m venv "$VENV_ROOT"
fi
VENV_PY="$VENV_ROOT/bin/python"
VENV_PIP="$VENV_ROOT/bin/pip"
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" -U pip setuptools wheel
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" -U "numpy<2.3" "scipy<1.15" pandas || true

# Match proven harvest CUDA stack
# shellcheck disable=SC1091
if [ -f /tmp/torch_cu_index.env ]; then source /tmp/torch_cu_index.env; fi
TORCH_CU_INDEX="${TORCH_CU_INDEX:-cu124}"
TORCH_IDX_URL="https://download.pytorch.org/whl/${TORCH_CU_INDEX}"
mkdir -p /tmp/torch_probe /tmp/cuda_probe /tmp/vllm_local_wheels
"$VENV_PIP" download -q --no-deps --index-url "$TORCH_IDX_URL" --dest /tmp/torch_probe \
  "torch==2.13.0" "torchaudio==2.11.0" "torchvision==0.28.0" || \
"$VENV_PIP" download -q --no-deps --index-url "$TORCH_IDX_URL" --dest /tmp/torch_probe "torch" "torchaudio" "torchvision"
"$VENV_PIP" install -q --force-reinstall /tmp/torch_probe/torch-*.whl /tmp/torch_probe/torchaudio-*.whl || true
"$VENV_PIP" install -q --no-deps --force-reinstall /tmp/torch_probe/torchvision-*.whl || true
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" litellm pyyaml httpx pydantic tenacity \
  google-genai google-auth Authlib requests oauthlib docker \
  opentelemetry-api opentelemetry-sdk nest_asyncio aiohttp cloudpickle \
  google-cloud-storage sqlalchemy aiosqlite
# Match p51r8 harvest vLLM runtime deps (vllm wheel installed --no-deps).
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
  "humming-kernels[cu13]==0.1.16" diskcache gguf astor
"$VENV_PIP" download -q --index-url "$PYPI_INDEX_URL" --no-deps --dest /tmp/vllm_local_wheels "vllm==0.31.0"
"$VENV_PIP" install -q --no-deps /tmp/vllm_local_wheels/vllm-0.31.0-*.whl
"$VENV_PIP" install -q --no-deps --ignore-requires-python "$SMOKE_ROOT"/wheels/*.whl || true
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" Authlib

"$VENV_PY" - <<'PY'
import cbor2  # noqa: F401 — required by vllm.utils.hashing (API server path)
import vllm, swegemma, adk_submission, torch
print("imports_ok", vllm.__version__, torch.__version__, torch.version.cuda, "cbor2_ok")
assert torch.cuda.is_available(), "no_cuda"
print("cuda_ok")
PY
[ $? -eq 0 ] || exit 13

python3 "$SMOKE_ROOT/scripts/ensure_subprocess_host_deps.py" --data "$SMOKE_ROOT/data" || true
for _py in python3 "$VENV_PY"; do
  "$_py" -m pip install -q --no-deps --force-reinstall \
    "$SMOKE_ROOT/data/wheels/starlette-0.46.2-py3-none-any.whl" || true
  "$_py" -m pip install -q --index-url "$PYPI_INDEX_URL" "pytest-timeout==2.1.0" || true
done
"$VENV_PY" "$SMOKE_ROOT/scripts/patch_swegemma_subprocess.py" || true
python3 "$SMOKE_ROOT/scripts/patch_swegemma_subprocess.py" || true

export P31_ROOT="$SMOKE_ROOT"
export MODEL_PATH="${MODEL_PATH:-/models/gemma-4-31B-it-qat-w4a16-ct}"
export SMOKE_TICKET=P6.1-p56c
export SMOKE_BUDGET_ID=community_budget_fit
export P31_SKU="${P31_SKU:-rtx6000ada}"
export SWE_SANDBOX=subprocess

cd "$SMOKE_ROOT"
"$VENV_PY" scripts/run_p56c_smoke.py 2>&1 | tee "$OUT/p61_smoke_run.log"
cp -f "$SMOKE_ROOT/results/smoke.json" "$OUT/p61_smoke.json" || true
cp -f "$SMOKE_ROOT/results/smoke_summary.csv" "$OUT/p61_smoke_summary.csv" || true
test -f "$OUT/p61_smoke.json" || exit 14
python3 - <<'PY'
import json
from pathlib import Path
p = Path("/workspace/gemma4-p56c/results/p56c_full_edit_pipeline/p61_smoke.json")
d = json.loads(p.read_text())
print("P61_SMOKE_PASS_AT_1", d.get("pass_at_1"), "resolved", d.get("resolved"), "/", d.get("n"))
print("failed_edit_rate", d.get("failed_edit_rate"), "loop_rate", d.get("loop_rate"))
PY
exit 0
