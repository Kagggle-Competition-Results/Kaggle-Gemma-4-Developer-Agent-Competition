#!/usr/bin/env bash
# Launch P6.1b clean smoke (≤$1.20/hr). Meta → /tmp/p61b_ssh.json
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
GA="$ROOT/gemma4-agent"
POD_NAME="${POD_NAME:-p61b-smoke-$(date -u +%m%d-%H%M%S)}"
GPU_TYPE="${GPU_TYPE:-NVIDIA A40}"
CLOUD_TYPE="${CLOUD_TYPE:-SECURE}"
IMAGE_NAME="${IMAGE_NAME:-runpod/pytorch:1.4.1-rc.171-cu1300-torch2130-ubuntu2404}"
CONTAINER_DISK_GB="${CONTAINER_DISK_GB:-80}"
MIN_VCPU_COUNT="${MIN_VCPU_COUNT:-8}"
MIN_MEMORY_GB="${MIN_MEMORY_GB:-48}"
META_JSON="${META_JSON:-/tmp/p61b_ssh.json}"
AWS_CREDS_FILE="${AWS_CREDS_FILE:-/tmp/p51_aws_creds.env}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/casmi_runpod}"
PUBLIC_KEY_FILE="${PUBLIC_KEY_FILE:-$HOME/.ssh/casmi_runpod.pub}"
BOOTSTRAP_SCRIPT="${BOOTSTRAP_SCRIPT:-$GA/scripts/p61b_clean_smoke_bootstrap.sh}"
WATCHDOG_SCRIPT="${WATCHDOG_SCRIPT:-$GA/scripts/watchdog_p61b.sh}"
WAIT_POLLS="${WAIT_POLLS:-40}"
WAIT_SECONDS="${WAIT_SECONDS:-15}"
START_WATCHDOG="${START_WATCHDOG:-1}"
EXISTING_POD_ID="${EXISTING_POD_ID:-}"
MAX_USD_PER_HR="${MAX_USD_PER_HR:-1.20}"

require_file() { [ -f "$1" ] || { echo "missing file: $1" >&2; exit 2; }; }

resolve_runpod_api_key() {
  if [ -n "${RUNPOD_API_KEY:-}" ]; then printf '%s' "$RUNPOD_API_KEY"; return 0; fi
  for cand in "$HOME/.runpod_api_key_session" "$HOME/.casmi/runpod_api_key"; do
    if [ -f "$cand" ]; then tr -d '\n' < "$cand"; return 0; fi
  done
  echo "missing RunPod API key" >&2; exit 2
}

require_file "$SSH_KEY"
require_file "$PUBLIC_KEY_FILE"
require_file "$BOOTSTRAP_SCRIPT"
require_file "$WATCHDOG_SCRIPT"
require_file "$GA/results/p6_candidate_agent_v2/agent/agent.yaml"
require_file "$GA/results/p6_candidate_agent_v2/agent/adapters/master_swe_adapter/adapter_model.safetensors"
require_file "$GA/scripts/run_p56c_smoke.py"
require_file "$GA/configs/splits.json"

RUNPOD_API_KEY="$(resolve_runpod_api_key)"
export RUNPOD_API_KEY GPU_TYPE IMAGE_NAME CONTAINER_DISK_GB MIN_VCPU_COUNT MIN_MEMORY_GB POD_NAME PUBLIC_KEY_FILE META_JSON WAIT_POLLS WAIT_SECONDS CLOUD_TYPE MAX_USD_PER_HR

# Pack clean candidate (no AppleDouble)
AGENT_TGZ=/tmp/p6_candidate_agent_v2.tgz
echo "packing candidate agent → $AGENT_TGZ"
(
  cd "$GA/results/p6_candidate_agent_v2"
  find . \( -name '._*' -o -name '.DS_Store' -o -name '.gitkeep' \) -delete 2>/dev/null || true
  tar --exclude='._*' --exclude='.DS_Store' --exclude='__pycache__' -czf "$AGENT_TGZ" agent
)
ls -lh "$AGENT_TGZ"
# Verify no AppleDouble inside tarball
if tar -tzf "$AGENT_TGZ" | grep -E '(^|/)\._|\.DS_Store'; then
  echo "tarball contains AppleDouble/DS_Store — abort" >&2
  exit 3
fi
echo "TGZ_CLEAN appledouble=0"

if [ -z "$EXISTING_POD_ID" ]; then
  EXISTING_POD_ID="$(
    python3 - <<'PY'
import json, os, urllib.parse, urllib.request
from pathlib import Path

def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')

api_key = os.environ["RUNPOD_API_KEY"]
pubkey = Path(os.environ["PUBLIC_KEY_FILE"]).read_text().strip()
cloud = os.environ.get("CLOUD_TYPE", "SECURE")
query = f'''mutation {{ podFindAndDeployOnDemand(input: {{ cloudType: {cloud}, gpuCount: 1, volumeInGb: 0, containerDiskInGb: {int(os.environ["CONTAINER_DISK_GB"])}, minVcpuCount: {int(os.environ["MIN_VCPU_COUNT"])}, minMemoryInGb: {int(os.environ["MIN_MEMORY_GB"])}, gpuTypeId: "{esc(os.environ["GPU_TYPE"])}", name: "{esc(os.environ["POD_NAME"])}", imageName: "{esc(os.environ["IMAGE_NAME"])}", dockerArgs: "", ports: "22/tcp", volumeMountPath: "/workspace", env: [{{ key: "PUBLIC_KEY", value: "{esc(pubkey)}" }}] }}) {{ id imageName machineId machine {{ podHostId }} }} }}'''
url = "https://api.runpod.io/graphql?api_key=" + urllib.parse.quote(api_key)
req = urllib.request.Request(url, data=json.dumps({"query": query}).encode(), headers={"content-type": "application/json", "User-Agent": "Mozilla/5.0"}, method="POST")
with urllib.request.urlopen(req, timeout=180) as resp:
    data = json.loads(resp.read().decode())
pod = data.get("data", {}).get("podFindAndDeployOnDemand")
if not pod:
    raise SystemExit(json.dumps(data))
print(pod["id"])
PY
  )"
fi

export EXISTING_POD_ID
python3 - <<'PY'
import json, os, time, urllib.parse, urllib.request
from pathlib import Path

api_key = os.environ["RUNPOD_API_KEY"]
pod_id = os.environ["EXISTING_POD_ID"]
wait_polls = int(os.environ["WAIT_POLLS"])
wait_seconds = int(os.environ["WAIT_SECONDS"])
gpu_type = os.environ["GPU_TYPE"]
image_name = os.environ["IMAGE_NAME"]
pod_name = os.environ["POD_NAME"]
meta_json = Path(os.environ["META_JSON"])
max_hr = float(os.environ.get("MAX_USD_PER_HR", "1.20"))
query = f'''query {{ pod(input: {{ podId: "{pod_id}" }}) {{ id name desiredStatus costPerHr imageName runtime {{ uptimeInSeconds ports {{ ip isIpPublic publicPort privatePort type }} }} }} }}'''
url = "https://api.runpod.io/graphql?api_key=" + urllib.parse.quote(api_key)
for _ in range(wait_polls):
    req = urllib.request.Request(url, data=json.dumps({"query": query}).encode(), headers={"content-type": "application/json", "User-Agent": "Mozilla/5.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    pod = data.get("data", {}).get("pod")
    if pod and pod.get("runtime") and pod["runtime"].get("ports"):
        cost = float(pod.get("costPerHr") or 0)
        if cost > max_hr + 1e-9:
            raise SystemExit(f"pod costPerHr={cost} exceeds max_usd_per_hr={max_hr}")
        ports = [p for p in pod["runtime"]["ports"] if p.get("isIpPublic") and p.get("type") == "tcp"]
        if ports:
            out = {
                "host": ports[0]["ip"],
                "port": ports[0]["publicPort"],
                "pod_id": pod_id,
                "gpu_type": gpu_type,
                "image": pod.get("imageName") or image_name,
                "name": pod.get("name") or pod_name,
                "cost_per_hr": cost,
            }
            meta_json.write_text(json.dumps(out, indent=2))
            print(json.dumps(out))
            raise SystemExit(0)
    time.sleep(wait_seconds)
raise SystemExit(f"timed out waiting for SSH on pod {pod_id}")
PY

HOST="$(python3 -c "import json;print(json.load(open('$META_JSON'))['host'])")"
PORT="$(python3 -c "import json;print(json.load(open('$META_JSON'))['port'])")"
POD_ID="$(python3 -c "import json;print(json.load(open('$META_JSON'))['pod_id'])")"
COST="$(python3 -c "import json;print(json.load(open('$META_JSON')).get('cost_per_hr'))")"

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o BatchMode=yes)
SCP_OPTS=(-i "$SSH_KEY" -P "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20)

echo "uploading P6.1b bundle to $HOST:$PORT ..."
scp "${SCP_OPTS[@]}" \
  "$BOOTSTRAP_SCRIPT" \
  "$GA/scripts/run_p56c_smoke.py" \
  "$GA/scripts/patch_swegemma_subprocess.py" \
  "$GA/scripts/ensure_subprocess_host_deps.py" \
  "$GA/configs/splits.json" \
  "$AGENT_TGZ" \
  root@"$HOST":/workspace/

# rename bootstrap for remote path
ssh "${SSH_OPTS[@]}" -p "$PORT" root@"$HOST" \
  'mv /workspace/p61b_clean_smoke_bootstrap.sh /workspace/p61b_clean_smoke_bootstrap.sh 2>/dev/null || true
   chmod +x /workspace/p61b_clean_smoke_bootstrap.sh
   ls -lh /workspace/p6_candidate_agent_v2.tgz /workspace/p61b_clean_smoke_bootstrap.sh
   find /workspace -name "._*" -delete 2>/dev/null || true
   echo APPLEDOUBLE_PRE=$(find /workspace -name "._*" | wc -l)'

if [ -f "$AWS_CREDS_FILE" ]; then
  scp "${SCP_OPTS[@]}" "$AWS_CREDS_FILE" root@"$HOST":/workspace/p51_aws_creds.env
fi

ssh "${SSH_OPTS[@]}" -p "$PORT" root@"$HOST" \
  'chmod +x /workspace/p61b_clean_smoke_bootstrap.sh
   setsid bash /workspace/p61b_clean_smoke_bootstrap.sh </dev/null >/tmp/p61b_launcher.log 2>&1 &
   sleep 2
   echo launched
   [ -f /workspace/p61b_smoke.status ] && echo STATUS=$(cat /workspace/p61b_smoke.status) || echo STATUS=pending
   pgrep -af p61b_clean_smoke | head -3 || true
   exit 0'

if [ "$START_WATCHDOG" = "1" ]; then
  nohup bash "$WATCHDOG_SCRIPT" >/tmp/launch_p61b_watchdog.log 2>&1 &
  WATCHDOG_PID=$!
else
  WATCHDOG_PID=""
fi

printf 'pod=%s\n' "$POD_ID"
printf 'ssh=%s:%s\n' "$HOST" "$PORT"
printf 'cost_per_hr=%s\n' "$COST"
printf 'meta=%s\n' "$META_JSON"
if [ -n "$WATCHDOG_PID" ]; then
  printf 'watchdog_pid=%s\n' "$WATCHDOG_PID"
fi
