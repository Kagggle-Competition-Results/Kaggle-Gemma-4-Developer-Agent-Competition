#!/usr/bin/env bash
# P5.2c autonomous 30m poll loop until S3_PERSIST_DONE (exit 42 from tick script).
set +e
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy || true
ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
TICK_SCRIPT="$ROOT/gemma4-agent/scripts/p52c_poll_tick.sh"
LOG="$ROOT/gemma4-agent/docs/p52c_30m_poll.log"
PIDFILE=/tmp/p52c_poll_loop.pid
INTERVAL_SEC="${POLL_INTERVAL_SEC:-1800}"
START_TICK="${START_TICK:-12}"

echo $$ > "$PIDFILE"
mkdir -p "$(dirname "$LOG")"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | p52c_poll_loop start interval=${INTERVAL_SEC}s start_tick=$START_TICK" | tee -a "$LOG"

tick=$START_TICK
while true; do
  echo "AGENT_LOOP_TICK_p52c {\"tick\":$tick,\"pod\":\"mza13vx6cc2m6g\",\"meta\":\"/tmp/p52_wave2_ssh.json\"}" | tee -a "$LOG"
  TICK=$tick bash "$TICK_SCRIPT" >>"$LOG" 2>&1
  rc=$?
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | tick=$tick rc=$rc" | tee -a "$LOG"
  if [ "$rc" = "42" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | S3_PERSIST_DONE finalized — exit loop" | tee -a "$LOG"
    rm -f "$PIDFILE"
    exit 0
  fi
  tick=$((tick + 1))
  sleep "$INTERVAL_SEC"
done
