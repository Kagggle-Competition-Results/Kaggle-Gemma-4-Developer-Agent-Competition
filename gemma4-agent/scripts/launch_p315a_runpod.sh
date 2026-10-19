#!/usr/bin/env bash
# Launch P3.15a 12B smoke on RunPod GPU; S3 for storage only (no AWS compute).
# Injects AWS_* into pod env from ~/.aws/credentials (never writes a temp creds file).
set -euo pipefail

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
POD_NAME="${POD_NAME:-p315a-12b-$(date -u +%m%d-%H%M%S)}"
GPU_TYPE="${GPU_TYPE:-NVIDIA A40}"
IMAGE_NAME="${IMAGE_NAME:-runpod/pytorch:1.4.1-rc.171-cu1300-torch2130-ubuntu2404}"
CONTAINER_DISK_GB="${CONTAINER_DISK_GB:-80}"
MIN_VCPU_COUNT="${MIN_VCPU_COUNT:-8}"
MIN_MEMORY_GB="${MIN_MEMORY_GB:-30}"
META_JSON="${META_JSON:-/tmp/p315a_ssh.json}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/casmi_runpod}"
PUBLIC_KEY_FILE="${PUBLIC_KEY_FILE:-$HOME/.ssh/casmi_runpod.pub}"
BOOTSTRAP_SCRIPT="${BOOTSTRAP_SCRIPT:-$ROOT/gemma4-agent/scripts/p315a_bootstrap.sh}"
AGENT_OVERRIDE_DIR="${AGENT_OVERRIDE_DIR:-$ROOT/gemma4-agent/results/p315a_018_model_12b_only/agent}"
OVERRIDE_FILES="${OVERRIDE_FILES:-$ROOT/gemma4-agent/scripts/run_p31_smoke.py:$ROOT/gemma4-agent/scripts/ensure_subprocess_host_deps.py}"
WAIT_POLLS="${WAIT_POLLS:-36}"
WAIT_SECONDS="${WAIT_SECONDS:-10}"
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
  echo "missing RunPod API key" >&2
  exit 2
}

require_file "$SSH_KEY"
require_file "$PUBLIC_KEY_FILE"
require_file "$BOOTSTRAP_SCRIPT"
require_file "$AGENT_OVERRIDE_DIR/agent.yaml"
require_file "$HOME/.aws/credentials"

RUNPOD_API_KEY="$(resolve_runpod_api_key)"
export RUNPOD_API_KEY GPU_TYPE IMAGE_NAME CONTAINER_DISK_GB MIN_VCPU_COUNT MIN_MEMORY_GB POD_NAME PUBLIC_KEY_FILE META_JSON WAIT_POLLS WAIT_SECONDS

if [ -z "$EXISTING_POD_ID" ]; then
  EXISTING_POD_ID="$(
    python3 - <<'PY'
import configparser, json, os, urllib.parse, urllib.request
from pathlib import Path

def esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')

api_key = os.environ["RUNPOD_API_KEY"]
pubkey = Path(os.environ["PUBLIC_KEY_FILE"]).read_text().strip()
cfg = configparser.ConfigParser()
cfg.read(Path.home() / ".aws" / "credentials")
sec = cfg["default"] if "default" in cfg else cfg[cfg.sections()[0]]
ak = esc(sec["aws_access_key_id"])
sk = esc(sec["aws_secret_access_key"])
query = f'''mutation {{ podFindAndDeployOnDemand(input: {{ cloudType: SECURE, gpuCount: 1, volumeInGb: 0, containerDiskInGb: {int(os.environ["CONTAINER_DISK_GB"])}, minVcpuCount: {int(os.environ["MIN_VCPU_COUNT"])}, minMemoryInGb: {int(os.environ["MIN_MEMORY_GB"])}, gpuTypeId: "{esc(os.environ["GPU_TYPE"])}", name: "{esc(os.environ["POD_NAME"])}", imageName: "{esc(os.environ["IMAGE_NAME"])}", dockerArgs: "", ports: "22/tcp", volumeMountPath: "/workspace", env: [{{ key: "PUBLIC_KEY", value: "{esc(pubkey)}" }}, {{ key: "AWS_ACCESS_KEY_ID", value: "{ak}" }}, {{ key: "AWS_SECRET_ACCESS_KEY", value: "{sk}" }}, {{ key: "AWS_DEFAULT_REGION", value: "us-east-1" }}, {{ key: "P31_SKU", value: "{esc(os.environ["GPU_TYPE"])}" }}] }}) {{ id imageName machineId machine {{ podHostId }} }} }}'''
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
                "cost_per_hr": pod.get("costPerHr"),
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

ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o BatchMode=yes -p "$PORT" root@"$HOST" \
  "chmod +x /workspace/$BOOTSTRAP_BASENAME && nohup bash /workspace/$BOOTSTRAP_BASENAME >/tmp/p315a_bootstrap_launcher.log 2>&1 </dev/null & sleep 4; echo launched; [ -f /workspace/p315a.status ] && echo STATUS=\$(cat /workspace/p315a.status) || true; tail -n 30 /workspace/p315a_bootstrap.log 2>/dev/null || true; echo ====; pgrep -af p315a_bootstrap || true"

printf 'pod=%s\n' "$POD_ID"
printf 'ssh=%s:%s\n' "$HOST" "$PORT"
printf 'meta=%s\n' "$META_JSON"
printf 'ssh_cmd=ssh -i %q -p %q root@%q\n' "$SSH_KEY" "$PORT" "$HOST"
