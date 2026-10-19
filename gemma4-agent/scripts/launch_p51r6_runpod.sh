#!/usr/bin/env bash
# One-shot local launcher for P5.1a-r6 RunPod reruns.
# Creates or reuses a pod, waits for SSH, copies local overrides, starts bootstrap,
# and optionally starts the local watchdog.
set -euo pipefail

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
POD_NAME="${POD_NAME:-p51a-r6-rerun-$(date -u +%m%d-%H%M%S)}"
GPU_TYPE="${GPU_TYPE:-NVIDIA A40}"
IMAGE_NAME="${IMAGE_NAME:-runpod/pytorch:1.4.1-rc.171-cu1300-torch2130-ubuntu2404}"
CONTAINER_DISK_GB="${CONTAINER_DISK_GB:-50}"
MIN_VCPU_COUNT="${MIN_VCPU_COUNT:-8}"
MIN_MEMORY_GB="${MIN_MEMORY_GB:-30}"
META_JSON="${META_JSON:-/tmp/p51r6_ssh.json}"
AWS_CREDS_FILE="${AWS_CREDS_FILE:-/tmp/p51_aws_creds.env}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/casmi_runpod}"
PUBLIC_KEY_FILE="${PUBLIC_KEY_FILE:-$HOME/.ssh/casmi_runpod.pub}"
BOOTSTRAP_SCRIPT="${BOOTSTRAP_SCRIPT:-$ROOT/gemma4-agent/scripts/p51r6_bootstrap.sh}"
WATCHDOG_SCRIPT="${WATCHDOG_SCRIPT:-$ROOT/gemma4-agent/scripts/watchdog_p51r6.sh}"
AGENT_OVERRIDE_DIR="${AGENT_OVERRIDE_DIR:-$ROOT/gemma4-agent/agent}"
OVERRIDE_FILES="${OVERRIDE_FILES:-$ROOT/gemma4-agent/scripts/rejection_sample.py:$ROOT/gemma4-agent/scripts/star_correct.py:$ROOT/gemma4-agent/scripts/ensure_subprocess_host_deps.py}"
WAIT_POLLS="${WAIT_POLLS:-30}"
WAIT_SECONDS="${WAIT_SECONDS:-10}"
START_WATCHDOG="${START_WATCHDOG:-1}"
EXISTING_POD_ID="${EXISTING_POD_ID:-}"

require_file() {
  local path="$1"
  [ -f "$path" ] || { echo "missing file: $path" >&2; exit 2; }
}

resolve_runpod_api_key() {
  if [ -n "${RUNPOD_API_KEY:-}" ]; then
    printf '%s' "$RUNPOD_API_KEY"
    return 0
  fi
  local cand
  for cand in "$HOME/.runpod_api_key_session" "$HOME/.casmi/runpod_api_key"; do
    if [ -f "$cand" ]; then
      tr -d '\n' < "$cand"
      return 0
    fi
  done
  echo "missing RunPod API key (env RUNPOD_API_KEY or ~/.runpod_api_key_session)" >&2
  exit 2
}

require_file "$SSH_KEY"
require_file "$PUBLIC_KEY_FILE"
require_file "$BOOTSTRAP_SCRIPT"
require_file "$WATCHDOG_SCRIPT"
require_file "$AGENT_OVERRIDE_DIR/agent.yaml"

RUNPOD_API_KEY="$(resolve_runpod_api_key)"
export RUNPOD_API_KEY GPU_TYPE IMAGE_NAME CONTAINER_DISK_GB MIN_VCPU_COUNT MIN_MEMORY_GB POD_NAME PUBLIC_KEY_FILE META_JSON WAIT_POLLS WAIT_SECONDS

if [ -z "$EXISTING_POD_ID" ]; then
  EXISTING_POD_ID="$(
    python3 - <<'PY'
import json, os, urllib.parse, urllib.request
from pathlib import Path

def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')

api_key = os.environ["RUNPOD_API_KEY"]
pubkey = Path(os.environ["PUBLIC_KEY_FILE"]).read_text().strip()
query = f'''mutation {{ podFindAndDeployOnDemand(input: {{ cloudType: SECURE, gpuCount: 1, volumeInGb: 0, containerDiskInGb: {int(os.environ["CONTAINER_DISK_GB"])}, minVcpuCount: {int(os.environ["MIN_VCPU_COUNT"])}, minMemoryInGb: {int(os.environ["MIN_MEMORY_GB"])}, gpuTypeId: "{esc(os.environ["GPU_TYPE"])}", name: "{esc(os.environ["POD_NAME"])}", imageName: "{esc(os.environ["IMAGE_NAME"])}", dockerArgs: "", ports: "22/tcp", volumeMountPath: "/workspace", env: [{{ key: "PUBLIC_KEY", value: "{esc(pubkey)}" }}] }}) {{ id imageName machineId machine {{ podHostId }} }} }}'''
url = "https://api.runpod.io/graphql?api_key=" + urllib.parse.quote(api_key)
req = urllib.request.Request(url, data=json.dumps({"query": query}).encode(), headers={"content-type": "application/json", "User-Agent": "Mozilla/5.0"}, method="POST")
with urllib.request.urlopen(req, timeout=120) as resp:
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
query = f'''query {{ pod(input: {{ podId: "{pod_id}" }}) {{ id name desiredStatus costPerHr imageName runtime {{ uptimeInSeconds ports {{ ip isIpPublic publicPort privatePort type }} }} }} }}'''
url = "https://api.runpod.io/graphql?api_key=" + urllib.parse.quote(api_key)
for _ in range(wait_polls):
    req = urllib.request.Request(url, data=json.dumps({"query": query}).encode(), headers={"content-type": "application/json", "User-Agent": "Mozilla/5.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    pod = data.get("data", {}).get("pod")
    if pod and pod.get("runtime") and pod["runtime"].get("ports"):
        ports = [p for p in pod["runtime"]["ports"] if p.get("isIpPublic") and p.get("type") == "tcp"]
        if ports:
            out = {
                "host": ports[0]["ip"],
                "port": ports[0]["publicPort"],
                "pod_id": pod_id,
                "gpu_type": gpu_type,
                "image": pod.get("imageName") or image_name,
                "name": pod.get("name") or pod_name,
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
BOOTSTRAP_BASENAME="$(basename "$BOOTSTRAP_SCRIPT")"

IFS=':' read -r -a OVERRIDES <<< "$OVERRIDE_FILES"
COPY_FILES=("$BOOTSTRAP_SCRIPT")
for path in "${OVERRIDES[@]}"; do
  [ -n "$path" ] || continue
  require_file "$path"
  COPY_FILES+=("$path")
done

scp -i "$SSH_KEY" -P "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 \
  "${COPY_FILES[@]}" root@"$HOST":/workspace/
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o BatchMode=yes -p "$PORT" root@"$HOST" \
  "rm -rf /workspace/agent_override && mkdir -p /workspace/agent_override"
scp -r -i "$SSH_KEY" -P "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 \
  "$AGENT_OVERRIDE_DIR"/. root@"$HOST":/workspace/agent_override/

if [ -f "$AWS_CREDS_FILE" ]; then
  scp -i "$SSH_KEY" -P "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 \
    "$AWS_CREDS_FILE" root@"$HOST":/workspace/p51_aws_creds.env
else
  echo "warning: AWS creds file not found at $AWS_CREDS_FILE; remote bootstrap must rely on pod env" >&2
fi

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o BatchMode=yes -p "$PORT" root@"$HOST" \
  "mkdir -p /workspace/gemma4-p51/scripts && chmod +x /workspace/$BOOTSTRAP_BASENAME && nohup bash /workspace/$BOOTSTRAP_BASENAME >/tmp/p51r6_bootstrap_launcher.log 2>&1 </dev/null & sleep 4; echo launched; [ -f /workspace/p51r6.status ] && echo STATUS=\$(cat /workspace/p51r6.status) || true; tail -n 20 /workspace/p51r6_bootstrap.log 2>/dev/null || true; echo ====; pgrep -af p51r6_bootstrap || true"

if [ "$START_WATCHDOG" = "1" ]; then
  nohup bash "$WATCHDOG_SCRIPT" >/tmp/launch_p51r6_runpod_watchdog.log 2>&1 &
  WATCHDOG_PID=$!
else
  WATCHDOG_PID=""
fi

printf 'pod=%s\n' "$POD_ID"
printf 'ssh=%s:%s\n' "$HOST" "$PORT"
printf 'meta=%s\n' "$META_JSON"
if [ -n "$WATCHDOG_PID" ]; then
  printf 'watchdog_pid=%s\n' "$WATCHDOG_PID"
  printf 'watchdog_launcher_log=%s\n' "/tmp/launch_p51r6_runpod_watchdog.log"
fi
printf 'ssh_cmd=ssh -i %q -p %q root@%q\n' "$SSH_KEY" "$PORT" "$HOST"
