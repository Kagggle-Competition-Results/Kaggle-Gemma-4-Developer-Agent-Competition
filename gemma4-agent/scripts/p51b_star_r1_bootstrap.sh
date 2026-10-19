#!/bin/bash
# P5.1b-star-r1: STaR recovery on r6 near-misses (patch>0, zero keepers).
set -uo pipefail
exec >> /workspace/p51b_star_r1_bootstrap.log 2>&1
echo "=== P5.1b-star-r1 bootstrap $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
nvidia-smi | head -n 3 || true
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_IGNORE_REQUIRES_PYTHON=1
export VLLM_USE_FLASHINFER_SAMPLER=0
export AWS_DEFAULT_REGION=us-east-1
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
pip install -q awscli 2>/dev/null || true
hash -r || true
BUCKET=gemma4-dev-agent-data-650825122703
ROOT=/workspace/gemma4-p51
STATUS_FILE=/workspace/p51b_star_r1.status
mkdir -p "$ROOT" /workspace/models

set_status() {
  printf '%s\n' "$1" > "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
  echo "STATUS=$1"
}

persist_star_artifacts() {
  local rc=0
  aws s3 sync traces/star/ "s3://$BUCKET/results/p51b_star_r1/traces/star/" --only-show-errors || rc=1
  aws s3 cp /workspace/p51b_star_r1_bootstrap.log "s3://$BUCKET/results/p51b_star_r1/p51b_star_r1_bootstrap.log" || rc=1
  aws s3 cp /workspace/p51b_star_r1.log "s3://$BUCKET/results/p51b_star_r1/p51b_star_r1.log" || rc=1
  aws s3 cp /workspace/oracle_rich_3882.json "s3://$BUCKET/results/p51b_star_r1/oracle_rich_3882.json" || rc=1
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

aws s3 sync "s3://$BUCKET/tools/gemma4-agent-agent/" "$ROOT/agent/" --only-show-errors || true
aws s3 sync "s3://$BUCKET/tools/gemma4-agent-configs/" "$ROOT/configs/" --only-show-errors || true
aws s3 sync "s3://$BUCKET/tools/gemma4-agent-scripts/" "$ROOT/scripts/" --only-show-errors || true
aws s3 cp "s3://$BUCKET/tools/scripts/rejection_sample.py" "$ROOT/scripts/rejection_sample.py"
aws s3 cp "s3://$BUCKET/tools/scripts/star_correct.py" "$ROOT/scripts/star_correct.py"
aws s3 cp "s3://$BUCKET/tools/scripts/ensure_subprocess_host_deps.py" "$ROOT/scripts/ensure_subprocess_host_deps.py"
aws s3 cp "s3://$BUCKET/tools/splits.json" "$ROOT/configs/splits.json"
aws s3 cp "s3://$BUCKET/tools/p51b_star_r1_tasks.txt" "$ROOT/configs/p51b_star_r1_tasks.txt"
aws s3 sync "s3://$BUCKET/tools/wheels_harness/" "$ROOT/wheels/" --only-show-errors
aws s3 sync "s3://$BUCKET/competition/full/" "$ROOT/data/" --only-show-errors
if [ ! -f /workspace/models/gemma-4-31B-it-qat-w4a16-ct/config.json ]; then
  aws s3 sync "s3://$BUCKET/models/gemma-4-31B-it-qat-w4a16-ct/" \
    /workspace/models/gemma-4-31B-it-qat-w4a16-ct/ --only-show-errors
fi

pip install -q -U "numpy<2.3" "scipy<1.15" pandas || true
# shellcheck disable=SC1091
source /tmp/torch_cu_index.env
TORCH_IDX_URL="https://download.pytorch.org/whl/${TORCH_CU_INDEX}"
echo "pip_torch_index=$TORCH_IDX_URL"
pip install -q --index-url "$TORCH_IDX_URL" torch torchvision torchaudio
pip install -q "vllm==0.31.0" || pip install -q vllm
pip install -q --index-url "$TORCH_IDX_URL" --force-reinstall torch torchvision torchaudio
pip install -q litellm pyyaml httpx pydantic tenacity \
  google-genai google-auth Authlib requests oauthlib docker \
  opentelemetry-api opentelemetry-sdk nest_asyncio aiohttp cloudpickle \
  google-cloud-storage sqlalchemy aiosqlite
pip install -q --no-deps --ignore-requires-python "$ROOT"/wheels/*.whl || true
pip install -q Authlib

python3 - <<'PY'
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
python3 -c "import pytest,starlette; print('verify_host', pytest.__version__, starlette.__version__)"

python3 - <<'PY'
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
  aws s3 cp /workspace/p51b_star_r1_bootstrap.log "s3://$BUCKET/results/p51b_star_r1/p51b_star_r1_bootstrap.log" || true
  aws s3 cp /workspace/oracle_rich_3882.json "s3://$BUCKET/results/p51b_star_r1/oracle_rich_3882.json" || true
  set_status ORACLE_FAIL
  exit 11
}
echo BOOTSTRAP_DONE
set_status BOOTSTRAP_DONE

export P31_ROOT=/workspace/gemma4-p51 P5_ROOT=/workspace/gemma4-p51
export MODEL_PATH=/workspace/models/gemma-4-31B-it-qat-w4a16-ct
export VLLM_USE_FLASHINFER_SAMPLER=0 SWE_SANDBOX=subprocess
cd /workspace/gemma4-p51
rm -rf traces/star; mkdir -p traces/star
: > /workspace/p51b_star_r1.log
echo "=== STAR START max_retries=1 tail=20 $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee /workspace/p51b_star_r1.log
set_status STAR_RUNNING
set +e
python3 scripts/star_correct.py \
  --tasks configs/p51b_star_r1_tasks.txt \
  --max-retries 1 \
  --error-tail-lines 20 \
  --agent-dir agent \
  --out traces/star \
  --data data \
  --model "$MODEL_PATH" 2>&1 | tee -a /workspace/p51b_star_r1.log
STAR_RC=${PIPESTATUS[0]}
set -uo pipefail
echo "STAR_RC=$STAR_RC" | tee -a /workspace/p51b_star_r1.log
if [ "$STAR_RC" != "0" ]; then
  echo STAR_FAIL | tee -a /workspace/p51b_star_r1.log
  set_status PERSISTING_FAILURE
  if persist_star_artifacts; then
    set_status STAR_FAIL
  else
    set_status S3_PERSIST_FAIL
  fi
  exit 13
fi
echo STAR_COMPLETE | tee -a /workspace/p51b_star_r1.log
set_status PERSISTING_SUCCESS
if persist_star_artifacts; then
  set_status S3_PERSIST_DONE
else
  set_status S3_PERSIST_FAIL
  exit 14
fi
