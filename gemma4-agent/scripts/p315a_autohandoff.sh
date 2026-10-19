#!/usr/bin/env bash
# After P3.15a smoke finishes: pull KPIs, terminate leftover pod if needed,
# then auto-start next step (P5.1a-r7 31B clean BoN if 12B fails Pass@1 gate).
set -euo pipefail
ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
LOG="$ROOT/gemma4-agent/docs/p315a_autohandoff.log"
META="${META_JSON:-/tmp/p315a_ssh.json}"
KEY="$HOME/.ssh/casmi_runpod"
BUCKET=gemma4-dev-agent-data-650825122703
GATE="${P315A_PASS_GATE:-0.14}"
FLAG="$ROOT/gemma4-agent/results/p315a_12b_smoke/HANDOFF_DONE"
mkdir -p "$(dirname "$LOG")" "$ROOT/gemma4-agent/results/p315a_12b_smoke"
exec >>"$LOG" 2>&1
echo "=== p315a_autohandoff $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

if [ -f "$FLAG" ]; then
  echo "already_handed_off"
  exit 0
fi

ssh_status() {
  local host port
  host=$(python3 -c "import json;print(json.load(open('$META'))['host'])")
  port=$(python3 -c "import json;print(json.load(open('$META'))['port'])")
  ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=15 -o BatchMode=yes -p "$port" root@"$host" \
    'cat /workspace/p315a.status 2>/dev/null || echo none' 2>/dev/null || echo ssh_fail
}

for i in $(seq 1 180); do
  st=$(ssh_status | tr -d '\r' | tail -1)
  echo "poll_$i status=$st $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  case "$st" in
    SMOKE_COMPLETE|SMOKE_FAIL|MODEL_FAIL|VLLM_IMPORT_FAIL) break ;;
  esac
  # Also succeed if S3 already has final smoke.json and pod gone
  if aws s3 ls "s3://$BUCKET/results/p315a_12b_smoke/smoke.json" >/dev/null 2>&1; then
    if [ "$st" = "ssh_fail" ] || [ "$st" = "none" ]; then
      echo "s3_smoke_json_present"
      break
    fi
  fi
  sleep 90
done

aws s3 sync "s3://$BUCKET/results/p315a_12b_smoke/" \
  "$ROOT/gemma4-agent/results/p315a_12b_smoke/" --region us-east-1 || true

# Prefer local results from live pod if S3 lag
if [ -f "$META" ]; then
  host=$(python3 -c "import json;print(json.load(open('$META'))['host'])")
  port=$(python3 -c "import json;print(json.load(open('$META'))['port'])")
  scp -i "$KEY" -P "$port" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    root@"$host":/workspace/gemma4-p315a/results/smoke.json \
    "$ROOT/gemma4-agent/results/p315a_12b_smoke/smoke.json" 2>/dev/null || true
  scp -i "$KEY" -P "$port" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    root@"$host":/workspace/gemma4-p315a/results/smoke_summary.csv \
    "$ROOT/gemma4-agent/results/p315a_12b_smoke/smoke_summary.csv" 2>/dev/null || true
fi

SMOKE_JSON="$ROOT/gemma4-agent/results/p315a_12b_smoke/smoke.json"
if [ ! -f "$SMOKE_JSON" ]; then
  echo "blocker=no_smoke_json"
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "${TS} | note | P3.15a_autohandoff | agent=assistant | hash=none | metrics=fail=no_smoke_json | next=manual | blocker=no_smoke_json" >> "$ROOT/docs/experiment_log.md"
  exit 2
fi

read -r PASS RESOLVED MEAN EXTRAP < <(python3 - <<PY
import json
from pathlib import Path
d=json.loads(Path("$SMOKE_JSON").read_text())
print(d.get("pass_at_1", 0), d.get("resolved", 0), d.get("mean_seconds", 0), d.get("extrapolated_hours_120", 0))
PY
)
echo "pass_at_1=$PASS resolved=$RESOLVED mean_s=$MEAN extrap_h=$EXTRAP gate=$GATE"

SHA=$(shasum -a 256 "$SMOKE_JSON" | awk '{print $1}')
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "${TS} | done | P3.15a_runpod | agent=assistant | hash=${SHA} | metrics=pass_at_1=${PASS},resolved=${RESOLVED},mean_s=${MEAN},extrap_h=${EXTRAP},g3=model_12b_only,baseline=exact_018 | next=auto_handoff | blocker=none" >> "$ROOT/docs/experiment_log.md"

# Terminate P3.15a pod if still up
if [ -f "$META" ]; then
  POD=$(python3 -c "import json;print(json.load(open('$META')).get('pod_id',''))")
  if [ -n "$POD" ]; then
    python3 - <<PY
import json, urllib.parse, urllib.request
from pathlib import Path
key=Path.home().joinpath(".runpod_api_key_session").read_text().strip()
pod="$POD"
q=f'mutation {{ podTerminate(input: {{ podId: "{pod}" }}) }}'
url="https://api.runpod.io/graphql?api_key="+urllib.parse.quote(key)
req=urllib.request.Request(url, data=json.dumps({"query":q}).encode(), headers={"content-type":"application/json","User-Agent":"Mozilla/5.0"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        print("terminate", resp.read().decode()[:200])
except Exception as e:
    print("terminate_err", e)
PY
  fi
fi

# Decision
NEXT=$(python3 - <<PY
gate=float("$GATE")
pas=float("$PASS")
print("harvest" if pas < gate else "p315b")
PY
)
echo "decision=$NEXT"

if [ "$NEXT" = "harvest" ]; then
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "${TS} | note | P3.15a_gate | agent=assistant | hash=none | metrics=pass_at_1=${PASS},gate=${GATE},decision=FAIL_keep_31b | next=P5.1a-r7 | blocker=none" >> "$ROOT/docs/experiment_log.md"
  echo "${TS} | claim | P5.1a-r7 | agent=assistant | hash=none | metrics=g3=env_parity_only,model=31b,trigger=p315a_fail_gate | next=launch_runpod | blocker=none" >> "$ROOT/docs/experiment_log.md"
  export POD_NAME="p51a-r7-$(date -u +%m%d-%H%M%S)"
  export META_JSON=/tmp/p51r7_ssh.json
  export START_WATCHDOG=1
  # Freeze agent only — never working-tree prompts (r7 contamination lesson).
  export AGENT_OVERRIDE_DIR=""
  export BOOTSTRAP_SCRIPT="$ROOT/gemma4-agent/scripts/p51r6_bootstrap.sh"
  export WATCHDOG_SCRIPT="$ROOT/gemma4-agent/scripts/watchdog_p51r6.sh"
  export OVERRIDE_FILES="$ROOT/gemma4-agent/scripts/rejection_sample.py:$ROOT/gemma4-agent/scripts/star_correct.py:$ROOT/gemma4-agent/scripts/ensure_subprocess_host_deps.py:$ROOT/gemma4-agent/scripts/pin_subprocess_task_deps.py"
  # Inject AWS via env at create (launch script for r6 uses creds file optionally)
  if [ ! -f /tmp/p51_aws_creds.env ]; then
    python3 - <<'PY'
import configparser
from pathlib import Path
cfg=configparser.ConfigParser(); cfg.read(Path.home()/".aws"/"credentials")
sec=cfg["default"] if "default" in cfg else cfg[cfg.sections()[0]]
Path("/tmp/p51_aws_creds.env").write_text(
    f"export AWS_ACCESS_KEY_ID={sec['aws_access_key_id']}\n"
    f"export AWS_SECRET_ACCESS_KEY={sec['aws_secret_access_key']}\n"
    f"export AWS_DEFAULT_REGION=us-east-1\n"
)
Path("/tmp/p51_aws_creds.env").chmod(0o600)
print("wrote_aws_creds_env")
PY
  fi
  bash "$ROOT/gemma4-agent/scripts/launch_p51r6_runpod.sh" || {
    echo "launch_r7_fail"
    TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    echo "${TS} | note | P5.1a-r7 | agent=assistant | hash=none | metrics=launch_fail=1 | next=manual_relaunch | blocker=launch_fail" >> "$ROOT/docs/experiment_log.md"
    exit 3
  }
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "${TS} | note | P5.1a-r7 | agent=assistant | hash=none | metrics=launched=1,meta=/tmp/p51r7_ssh.json,after_p315a_fail | next=watch_bon | blocker=none" >> "$ROOT/docs/experiment_log.md"
  echo "AGENT_HANDOFF_DONE {\"next\":\"P5.1a-r7\",\"pass_at_1\":$PASS}"
else
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  echo "${TS} | note | P3.15a_gate | agent=assistant | hash=none | metrics=pass_at_1=${PASS},gate=${GATE},decision=HOLD_consider_P3.15b | next=P3.15b_or_operator | blocker=none" >> "$ROOT/docs/experiment_log.md"
  echo "AGENT_HANDOFF_DONE {\"next\":\"P3.15b\",\"pass_at_1\":$PASS}"
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "$FLAG"
echo "=== p315a_autohandoff complete $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
