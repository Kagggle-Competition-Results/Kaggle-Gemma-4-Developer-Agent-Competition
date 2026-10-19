#!/usr/bin/env bash
# P5.3f — Non-intrusive incremental BON backup while pod BON_RUNNING.
# Read-only on pod (scp/rsync pull). Does NOT restart harvest or watchdog 20861.
# Pod pipeline only persist_p52() after STAR / BON_COMPLETE / BON_FAIL — mid-BON
# keepers are not on S3; this puller mirrors traces/bon → local for preemption safety.
set +e
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
AGENT="$ROOT/gemma4-agent"
DEST="$AGENT/results/p52_wave2_sieve/traces/bon"
LOG="$AGENT/docs/p52_wave2_bon_incremental_pull.log"
META=/tmp/p52_wave2_ssh.json
KEY="${SSH_KEY:-$HOME/.ssh/casmi_runpod}"
INTERVAL_SEC="${PULL_INTERVAL_SEC:-180}"
PIDFILE=/tmp/p52_wave2_bon_incremental_pull.pid

if [[ -f "$PIDFILE" ]]; then
  old=$(cat "$PIDFILE" 2>/dev/null)
  if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
    echo "already_running pid=$old"
    exit 0
  fi
fi
echo $$ > "$PIDFILE"
mkdir -p "$DEST" "$(dirname "$LOG")"
: >> "$LOG"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | start incremental BON pull → $DEST interval=${INTERVAL_SEC}s meta=$META" | tee -a "$LOG"

while true; do
  # Re-read meta each tick so redeploys do not leave us on a dead host/port.
  HOST=$(python3 -c "import json;print(json.load(open('$META'))['host'])")
  PORT=$(python3 -c "import json;print(json.load(open('$META'))['port'])")
  POD=$(python3 -c "import json;print(json.load(open('$META')).get('pod_id',''))")
  SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o BatchMode=yes -p "$PORT" root@"$HOST")
  SCP=(scp -i "$KEY" -P "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -r)
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  STATUS=$("${SSH[@]}" 'cat /workspace/p52_wave2.status 2>/dev/null' 2>/dev/null | tr -d '\r')
  # Stop when terminal statuses reached (watchdog owns final S3 sync)
  if echo "$STATUS" | grep -Eq 'S3_PERSIST_DONE|BON_FAIL|S3_PERSIST_FAIL'; then
    echo "$TS | status=$STATUS — exit puller" | tee -a "$LOG"
    rm -f "$PIDFILE"
    exit 0
  fi
  # Pull keep/ and any completed meta trees (read-only)
  mkdir -p "$DEST/keep" "$DEST/raw"
  "${SSH[@]}" 'ls /workspace/gemma4-p51/traces/bon/keep 2>/dev/null | wc -l; find /workspace/gemma4-p51/traces/bon -name meta.json 2>/dev/null | wc -l' >>"$LOG" 2>&1
  # rsync via scp of keep + metas if present (tolerate empty)
  "${SCP[@]}" "root@${HOST}:/workspace/gemma4-p51/traces/bon/keep/." "$DEST/keep/" >>"$LOG" 2>&1 || true
  # Prefer rsync if available for raw metas only
  if command -v rsync >/dev/null 2>&1; then
    rsync -az -e "ssh -i $KEY -p $PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10" \
      --include='*/' --include='**/meta.json' --include='**/patch.diff' --exclude='*' \
      "root@${HOST}:/workspace/gemma4-p51/traces/bon/raw/" "$DEST/raw/" >>"$LOG" 2>&1 || true
  fi
  # Also try S3 in case operator later enables mid-run sync (no-op if empty)
  # shellcheck disable=SC1091
  source /tmp/p51_aws_creds.env 2>/dev/null || true
  if command -v aws >/dev/null 2>&1; then
    aws s3 sync "s3://gemma4-dev-agent-data-650825122703/results/p52_wave2_sieve/traces/bon/" \
      "$DEST/" --region us-east-1 --only-show-errors >>"$LOG" 2>&1 || true
  fi
  LOCAL_KEEP=$(ls "$DEST/keep" 2>/dev/null | wc -l | tr -d ' ')
  LOCAL_META=$(find "$DEST" -name meta.json 2>/dev/null | wc -l | tr -d ' ')
  echo "$TS | pod=$POD ssh=$HOST:$PORT status=${STATUS:-?} local_keep=$LOCAL_KEEP local_meta=$LOCAL_META" | tee -a "$LOG"
  sleep "$INTERVAL_SEC"
done
