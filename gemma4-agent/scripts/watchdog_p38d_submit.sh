#!/usr/bin/env bash
# Auto-submit P3.8d max_out=8192 when Kaggle has no PENDING slot.
set +e
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export KAGGLE_API_TOKEN
KAGGLE_API_TOKEN=$(cat "$HOME/.kaggle/access_token")

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
LOG="$ROOT/gemma4-agent/docs/watchdog_p38d_submit.log"
ZIP="$ROOT/gemma4-agent/results/submission_p38d_maxout8192.zip"
SHA=b8e154030329403e3e1a027afe03d32bdbc3d4c68963469aa267e1270e635936
: > "$LOG"

for i in $(seq 1 400); do
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  SUBS=$(python3 -m kaggle competitions submissions -c gemma-4-developer-agent 2>&1 | head -10)
  {
    echo "$TS poll=$i"
    echo "$SUBS" | head -5
  } | tee -a "$LOG"

  if ! echo "$SUBS" | grep -q PENDING; then
    cp "$ZIP" /tmp/submission.zip
    if python3 -m kaggle competitions submit -c gemma-4-developer-agent -f /tmp/submission.zip \
      -m "P3.8d max_out=8192: kept wall4/60/temp0.0 (decode vs Verra 0.18; G3 sampling only)" 2>&1 | tee -a "$LOG"; then
      REF=$(python3 -m kaggle competitions submissions -c gemma-4-developer-agent 2>&1 | awk 'NR==4{print $1}')
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | done | P3.8d_max_out | agent=cursor | hash=$SHA | metrics=ref=$REF,max_output_tokens=8192,submitted=1,auto=1 | next=watch_score | blocker=none" >> "$ROOT/docs/experiment_log.md"
      echo "$TS SUBMIT_OK ref=$REF" | tee -a "$LOG"
      exit 0
    fi
    echo "$TS SUBMIT_FAIL — will retry" | tee -a "$LOG"
  fi
  sleep 120
done
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) | timeout" | tee -a "$LOG"
exit 2
