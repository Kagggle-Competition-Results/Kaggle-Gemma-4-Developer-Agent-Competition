#!/bin/bash
# P6.1b — clean 10-task smoke with staged p6_candidate_agent_v2 + master_swe_adapter v2.
# Plan B: vLLM --enable-lora. G4 smoke-only. Strips AppleDouble. Does not touch frozen agent/.
set -uo pipefail
exec >> /workspace/p61b_smoke_bootstrap.log 2>&1
echo "=== P6.1b clean smoke bootstrap $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
nvidia-smi | head -n 5 || true

export COPYFILE_DISABLE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_IGNORE_REQUIRES_PYTHON=1
export PIP_BREAK_SYSTEM_PACKAGES=1
export AWS_DEFAULT_REGION=us-east-1
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
export LD_LIBRARY_PATH="/usr/lib/x86_64-linux-gnu:/usr/local/cuda/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export CUDA_DEVICE_ORDER=PCI_BUS_ID
export VLLM_USE_FLASHINFER_SAMPLER=0

if [ -f /workspace/p51_aws_creds.env ]; then
  # shellcheck disable=SC1091
  set -a; source /workspace/p51_aws_creds.env; set +a
fi
pip install -q awscli 2>/dev/null || true
hash -r || true

BUCKET=gemma4-dev-agent-data-650825122703
S3_PREFIX="s3://$BUCKET/results/p56c_full_edit_pipeline"
OUT=/workspace/p61b_out
SMOKE_ROOT=/workspace/p61b_smoke
VENV_ROOT="$SMOKE_ROOT/.venv_bootstrap"
BOOTSTRAP_PY=/usr/bin/python3.12
PYPI_INDEX_URL=https://pypi.org/simple
MODEL_ROOT=/models
STATUS_FILE=/workspace/p61b_smoke.status
mkdir -p "$OUT" "$SMOKE_ROOT" "$MODEL_ROOT"

set_status() {
  printf '%s\n' "$1" > "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
  echo "STATUS=$1"
}

s3_sync() {
  local label="$1"
  aws s3 sync "$OUT/" "$S3_PREFIX/" --only-show-errors || true
  aws s3 cp /workspace/p61b_smoke_bootstrap.log "$S3_PREFIX/p61b_smoke_bootstrap.log" --only-show-errors || true
  aws s3 cp "$STATUS_FILE" "$S3_PREFIX/p61b_smoke.status" --only-show-errors || true
  echo "S3_SYNC $label $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

fail() {
  set_status "$1"
  s3_sync "FAIL_$1"
  exit "${2:-13}"
}

rm -f "$STATUS_FILE"
set_status BOOTSTRAPPING

# Torch ABI index
python3 - <<'PY'
import ctypes, re, subprocess
from pathlib import Path
def driver_abi() -> float:
    try:
        lib = ctypes.CDLL("libcuda.so.1")
        ver = ctypes.c_int(0)
        lib.cuDriverGetVersion(ctypes.byref(ver))
        major, rem = divmod(int(ver.value), 1000)
        minor = rem // 10
        return float(f"{major}.{minor}")
    except Exception as exc:
        print(f"cuDriverGetVersion_fail={exc}")
    out = subprocess.check_output(["nvidia-smi"], text=True)
    m = re.search(r"CUDA Version:\s*([0-9.]+)", out)
    return float(m.group(1)) if m else 12.4
ver = driver_abi()
idx = "cu130" if ver >= 13.0 else ("cu128" if ver >= 12.8 else ("cu124" if ver >= 12.4 else "cu121"))
Path("/tmp/torch_cu_index.env").write_text(f"export TORCH_CU_INDEX={idx}\nexport CUDA_DRIVER_VER={ver}\n")
print(f"cuda_driver_abi={ver} torch_index={idx}")
PY
# shellcheck disable=SC1091
source /tmp/torch_cu_index.env

# Stage clean agent (pre-validated locally; COPYFILE_DISABLE tar)
set_status AGENT_STAGE
test -f /workspace/p6_candidate_agent_v2.tgz || fail AGENT_TGZ_MISSING 2
rm -rf "$SMOKE_ROOT/agent" /tmp/p6_cand_unpack
mkdir -p /tmp/p6_cand_unpack
tar -xzf /workspace/p6_candidate_agent_v2.tgz -C /tmp/p6_cand_unpack
# tarball contains agent/ at root or p6_candidate_agent_v2/agent/
if [ -d /tmp/p6_cand_unpack/agent ]; then
  mv /tmp/p6_cand_unpack/agent "$SMOKE_ROOT/agent"
elif [ -d /tmp/p6_cand_unpack/p6_candidate_agent_v2/agent ]; then
  mv /tmp/p6_cand_unpack/p6_candidate_agent_v2/agent "$SMOKE_ROOT/agent"
else
  fail AGENT_LAYOUT_BAD 3
fi
find "$SMOKE_ROOT" \( -name '._*' -o -name '.DS_Store' -o -name '.gitkeep' \) -delete 2>/dev/null || true
find "$SMOKE_ROOT" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
N_DOT=$(find "$SMOKE_ROOT" -name '._*' | wc -l | tr -d ' ')
echo "APPLEDOUBLE_REMAINING=$N_DOT"
test "$N_DOT" = "0" || fail APPLEDOUBLE_REMAINING 4
test -f "$SMOKE_ROOT/agent/adapters/master_swe_adapter/adapter_model.safetensors" || fail ADAPTER_MISSING 5
test -f "$SMOKE_ROOT/agent/adapters/master_swe_adapter/adapter_config.json" || fail ADAPTER_CFG_MISSING 6
echo "SMOKE_ADAPTER_MOUNTED PlanB path=agent/adapters/master_swe_adapter"
ls -lh "$SMOKE_ROOT/agent/adapters/master_swe_adapter/"

# Scripts + splits
mkdir -p "$SMOKE_ROOT/scripts" "$SMOKE_ROOT/configs" "$SMOKE_ROOT/data/snapshots" \
  "$SMOKE_ROOT/data/graphs" "$SMOKE_ROOT/data/embeddings" "$SMOKE_ROOT/data/wheels" \
  "$SMOKE_ROOT/wheels" "$SMOKE_ROOT/results"
for f in run_p56c_smoke.py ensure_subprocess_host_deps.py patch_swegemma_subprocess.py; do
  cp "/workspace/$f" "$SMOKE_ROOT/scripts/$f"
done
cp /workspace/splits.json "$SMOKE_ROOT/configs/splits.json"

# Model sync
set_status MODEL_SYNC
aws s3 sync "s3://$BUCKET/models/gemma-4-31B-it-qat-w4a16-ct/" \
  "$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct/" --only-show-errors \
  || fail MODEL_SYNC_FAIL 14
export MODEL_PATH="$MODEL_ROOT/gemma-4-31B-it-qat-w4a16-ct"
test -f "$MODEL_PATH/config.json" || fail MODEL_MISSING 15
echo "MODEL_OK $MODEL_PATH"

# Smoke data (G4)
set_status DATA_SYNC
aws s3 cp "s3://$BUCKET/competition/full/tasks.jsonl" "$SMOKE_ROOT/data/tasks.jsonl" --only-show-errors
aws s3 sync "s3://$BUCKET/competition/full/wheels/" "$SMOKE_ROOT/data/wheels/" --only-show-errors
aws s3 sync "s3://$BUCKET/tools/wheels_harness/" "$SMOKE_ROOT/wheels/" --only-show-errors || true
python3 - <<'PY'
import json, subprocess
from pathlib import Path
root = Path("/workspace/p61b_smoke")
splits = json.loads((root / "configs" / "splits.json").read_text())
smoke = set(splits["smoke"])
holdout = set(splits.get("holdout") or [])
assert not (smoke & holdout), "G4 violation"
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
(root / "data" / "tasks_smoke.jsonl").write_text("\n".join(json.dumps(x) for x in needed) + "\n")
print("SMOKE_TASKS", sorted(smoke))
bucket = "gemma4-dev-agent-data-650825122703"
for obj in needed:
    tid = obj["instance_id"]
    snap = f"snapshots/{tid}.tgz"
    subprocess.check_call(
        ["aws", "s3", "cp", f"s3://{bucket}/competition/full/{snap}", f"{root}/data/{snap}", "--only-show-errors"]
    )
    bc = obj.get("base_commit") or ""
    repo = (obj.get("repo") or tid.split("_")[0]).replace("/", "__")
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

# vLLM venv (match harvest / p56c deps)
set_status VENV_SETUP
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
TORCH_IDX_URL="https://download.pytorch.org/whl/${TORCH_CU_INDEX}"
mkdir -p /tmp/torch_probe /tmp/vllm_local_wheels
"$VENV_PIP" download -q --no-deps --index-url "$TORCH_IDX_URL" --dest /tmp/torch_probe \
  "torch==2.13.0" "torchaudio==2.11.0" "torchvision==0.28.0" || \
"$VENV_PIP" download -q --no-deps --index-url "$TORCH_IDX_URL" --dest /tmp/torch_probe "torch" "torchaudio" "torchvision"
"$VENV_PIP" install -q --force-reinstall /tmp/torch_probe/torch-*.whl /tmp/torch_probe/torchaudio-*.whl || true
"$VENV_PIP" install -q --no-deps --force-reinstall /tmp/torch_probe/torchvision-*.whl || true
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
  "humming-kernels[cu13]==0.1.16" diskcache gguf astor
"$VENV_PIP" download -q --index-url "$PYPI_INDEX_URL" --no-deps --dest /tmp/vllm_local_wheels "vllm==0.31.0"
"$VENV_PIP" install -q --no-deps /tmp/vllm_local_wheels/vllm-0.31.0-*.whl
"$VENV_PIP" install -q --no-deps --ignore-requires-python "$SMOKE_ROOT"/wheels/*.whl || true
"$VENV_PIP" install -q --index-url "$PYPI_INDEX_URL" Authlib

"$VENV_PY" - <<'PY'
import cbor2  # noqa: F401
import vllm, swegemma, adk_submission, torch
from adk_submission.discovery import validate_directory, discover_adapters
from swegemma.config import build_submission_limits, ALLOWED_ADAPTER_EXTENSIONS
from pathlib import Path
agent = Path("/workspace/p61b_smoke/agent")
limits, _ = build_submission_limits()
sub = validate_directory(agent, limits=limits)
manifest = discover_adapters(agent, adapter_extensions=ALLOWED_ADAPTER_EXTENSIONS)
assert "master_swe_adapter" in manifest.adapters, list(manifest.adapters)
print("imports_ok", vllm.__version__, torch.__version__, torch.version.cuda, "cbor2_ok")
print("ADK_VALIDATE_OK files=", len(sub.all_files), "adapter=", list(manifest.adapters))
assert torch.cuda.is_available(), "no_cuda"
print("cuda_ok")
PY
[ $? -eq 0 ] || fail VENV_IMPORT_FAIL 13

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
export SMOKE_TICKET=P6.1b-clean-smoke
export SMOKE_BUDGET_ID=community_budget_fit
export P31_SKU="${P31_SKU:-l40s}"
export SWE_SANDBOX=subprocess

set_status SMOKE_RUNNING
s3_sync SMOKE_RUNNING
cd "$SMOKE_ROOT"
"$VENV_PY" scripts/run_p56c_smoke.py 2>&1 | tee "$OUT/p61b_smoke_run.log"
cp -f "$SMOKE_ROOT/results/smoke.json" "$OUT/p61b_smoke_clean.json" || fail SMOKE_NO_JSON 14
cp -f "$SMOKE_ROOT/results/smoke_summary.csv" "$OUT/p61b_smoke_summary.csv" || true
# Copy per-task traces if present
if [ -d "$SMOKE_ROOT/results" ]; then
  mkdir -p "$OUT/p61b_task_traces"
  cp -a "$SMOKE_ROOT/results/." "$OUT/p61b_task_traces/" 2>/dev/null || true
fi

python3 - <<'PY'
import json, re
from pathlib import Path
p = Path("/workspace/p61b_out/p61b_smoke_clean.json")
d = json.loads(p.read_text())
summary = Path("/workspace/p61b_out/p61b_smoke_summary.csv")
rows = []
if summary.is_file():
    lines = summary.read_text().splitlines()
    if len(lines) > 1:
        hdr = lines[0].split(",")
        for line in lines[1:]:
            cols = line.split(",")
            rows.append(dict(zip(hdr, cols)))
errors = [r.get("error", "") for r in rows]
# AppleDouble / sandbox extension failures only — NOT Python private attrs like `._format`.
_apple_re = re.compile(
    r"disallowed extension|Sandbox_disallowed_extension|(?:^|[\s\"'`/])\._[^/\s\"'`]+",
    re.I,
)
apple = any(bool(_apple_re.search(e or "")) for e in errors)
mean_s = float(d.get("mean_seconds") or 0)
# Valid = no appledouble sandbox death + real wall time (not ~4s scaffold fail)
valid = (not apple) and mean_s >= 30.0 and int(d.get("n") or 0) == 10
resolved_ids = [r["task_id"] for r in rows if str(r.get("resolved", "")).lower() in {"true", "1"}]
bad_arg = 0
bad_re = re.compile(r"bad.?arg|invalid.?arg|TypeError|unexpected keyword", re.I)
if rows:
    bad_arg = sum(1 for e in errors if bad_re.search(e or "")) / len(rows)
d.update({
    "ticket": "P6.1b-clean-smoke",
    "ablation": "p61b_master_swe_adapter_v2_clean_agent",
    "valid": valid,
    "appledouble_error": apple,
    "resolved_task_ids": resolved_ids,
    "bad_arg_rate": round(bad_arg, 4),
    "baseline_smoke_pass_at_1": 0.20,
    "crashes": 0 if valid else int(d.get("hard_crash_rate") or 0),
})
p.write_text(json.dumps(d, indent=2) + "\n")
print("P61B_PASS_AT_1", d.get("pass_at_1"), "resolved", d.get("resolved"), "/", d.get("n"))
print("failed_edit_rate", d.get("failed_edit_rate"), "loop_rate", d.get("loop_rate"), "mean_s", d.get("mean_seconds"))
print("valid", valid, "appledouble_error", apple, "resolved_ids", resolved_ids)
PY

set_status SMOKE_DONE
s3_sync SMOKE_DONE
set_status S3_PERSIST_DONE
s3_sync S3_PERSIST_DONE
echo "PIPELINE_DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit 0
