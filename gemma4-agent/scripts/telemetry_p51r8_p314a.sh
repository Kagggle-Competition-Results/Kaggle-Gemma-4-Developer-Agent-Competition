#!/usr/bin/env bash
# P5.1a-r8 15-minute harvest telemetry + circuit breakers.
# Polls remote metas/keepers; classifies fail mix; kills GPU on breaker trips.
set +e
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
LOG="$ROOT/gemma4-agent/docs/telemetry_p51r8_p314a.log"
META=/tmp/p51r8_p314a_ssh.json
KEY="$HOME/.ssh/casmi_runpod"
POLL_S="${POLL_S:-900}"
COST_PER_HR="${COST_PER_HR:-0.59}"
STARTED_AT_EPOCH="${STARTED_AT_EPOCH:-$(date +%s)}"

: > "$LOG"

HOST=$(python3 -c "import json;print(json.load(open('$META'))['host'])")
PORT=$(python3 -c "import json;print(json.load(open('$META'))['port'])")
POD=$(python3 -c "import json;print(json.load(open('$META'))['pod_id'])")

terminate_pod() {
  python3 - <<PY
import json, urllib.request
from pathlib import Path
for p in (Path.home()/".runpod_api_key_session", Path.home()/".casmi/runpod_api_key"):
    if p.is_file():
        key = p.read_text().strip(); break
else:
    raise SystemExit("no_runpod_key")
q = 'mutation { podTerminate(input: {podId: "$POD"}) }'
req = urllib.request.Request(
    "https://api.runpod.io/graphql",
    data=json.dumps({"query": q}).encode(),
    headers={"content-type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "Mozilla/5.0"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as r:
    print("terminate", r.read()[:300])
PY
}

i=0
while true; do
  i=$((i + 1))
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ELAPSED_H=$(python3 -c "import time; print(round((time.time()-float('$STARTED_AT_EPOCH'))/3600.0, 3))")
  SPEND=$(python3 -c "print(round(float('$COST_PER_HR')*float('$ELAPSED_H'), 3))")

  REMOTE=$(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=20 -o BatchMode=yes -p "$PORT" root@"$HOST" 'python3 -' <<'PY' 2>/dev/null
import json, os, re
from collections import Counter
from pathlib import Path

root = Path("/workspace/gemma4-p51")
status = Path("/workspace/p51r8_p314a.status").read_text().strip() if Path("/workspace/p51r8_p314a.status").is_file() else "NONE"
metas = list(root.glob("traces/bon/raw/*/n*/meta.json"))
keepers = list((root / "traces/bon/keepers").glob("*")) if (root / "traces/bon/keepers").exists() else []
# unique keepers by task
ktasks = set()
for k in keepers:
    # keepers/<task_id>__nN or similar
    name = k.name
    ktasks.add(name.split("__")[0].split("_n")[0] if "__" in name else name.split(".")[0])

fail = Counter()
task_keepers = Counter()
task_metas = Counter()
for mpath in metas:
    try:
        d = json.loads(mpath.read_text())
    except Exception:
        fail["parse_error"] += 1
        continue
    tid = d.get("task_id") or d.get("instance_id") or mpath.parts[-3]
    task_metas[tid] += 1
    resolved = bool(d.get("resolved") or d.get("oracle_pass") or (d.get("verify") or {}).get("resolved"))
    if resolved:
        task_keepers[tid] += 1
        fail["keeper"] += 1
        continue
    err = str(d.get("error") or d.get("verify_error") or "")
    test_out = str(d.get("test_output_tail") or d.get("test_output") or "")[-2000:]
    patched = bool(d.get("patched") or d.get("n_edits") or d.get("patch"))
    wall = bool(d.get("wall_timeout") or d.get("timed_out") or "timeout" in err.lower() or "max_time" in err.lower())
    harness = "test_patch" in err.lower() or "harness" in err.lower() or "ModuleNotFound" in err
    apply = "old_string" in err.lower() or "syntax" in err.lower() or "apply" in err.lower() or "IndentationError" in test_out
    no_edit = (not patched) and not wall
    if wall:
        fail["wall_timeout"] += 1
    elif harness:
        fail["harness_error"] += 1
    elif apply:
        fail["syntax_apply"] += 1
    elif no_edit:
        fail["no_edit"] += 1
    else:
        fail["assertion_fail"] += 1

n = max(len(metas), 1)
mix = {
    "assertion": round(100.0 * fail["assertion_fail"] / n, 1) if metas else 0.0,
    "no_edit": round(100.0 * fail["no_edit"] / n, 1) if metas else 0.0,
    "timeout": round(100.0 * fail["wall_timeout"] / n, 1) if metas else 0.0,
    "apply": round(100.0 * fail["syntax_apply"] / n, 1) if metas else 0.0,
    "harness": round(100.0 * fail["harness_error"] / n, 1) if metas else 0.0,
}
tasks_with_keeper = sum(1 for t, c in task_keepers.items() if c >= 1)
yield_tasks = round(100.0 * tasks_with_keeper / max(len(task_metas), 1), 1) if task_metas else 0.0
stop_tasks = [t for t, c in task_keepers.items() if c >= 2]
out = {
    "status": status,
    "metas": len(metas),
    "keepers_files": len(keepers),
    "unique_keeper_tasks": tasks_with_keeper,
    "task_yield_pct": yield_tasks,
    "fail": dict(fail),
    "mix": mix,
    "stop_tasks": stop_tasks,
    "task_metas": dict(task_metas),
    "task_keepers": dict(task_keepers),
}
print(json.dumps(out))
PY
)

  SFT_ROWS=$(wc -l < "$ROOT/gemma4-agent/data_train/sft.jsonl" | tr -d ' ')
  SFT_TASKS=$(python3 -c "import json;from pathlib import Path;rows=[json.loads(l) for l in Path('$ROOT/gemma4-agent/data_train/sft.jsonl').read_text().splitlines() if l.strip()];print(len({r.get('task_id') for r in rows}))")

  {
    echo "$TS | poll=$i pod=$POD host=$HOST:$PORT cost_per_hr=$COST_PER_HR elapsed_h=$ELAPSED_H spend_usd~$SPEND"
    echo "REMOTE=$REMOTE"
    echo "SFT=$SFT_ROWS/$SFT_TASKS"
  } | tee -a "$LOG"

  python3 - <<PY | tee -a "$LOG"
import json
from pathlib import Path
raw = '''$REMOTE'''
ts = "$TS"
cost = "$COST_PER_HR"
elapsed = "$ELAPSED_H"
spend = "$SPEND"
sft = "$SFT_ROWS/$SFT_TASKS"
try:
    d = json.loads(raw)
except Exception as e:
    print(f"{ts} | telemetry_parse_fail {e}")
    raise SystemExit(0)

mix = d.get("mix") or {}
metas = int(d.get("metas") or 0)
keepers = int(d.get("unique_keeper_tasks") or 0)
status = d.get("status")
print(f"| Active | P5.1a-r8 A40 \${cost}/hr · elapsed {elapsed}h · ~\${spend} |")
print(f"| Progress | rollouts={metas} · unique_keeper_tasks={keepers} · sft={sft} |")
print(f"| Fail mix % | assertion={mix.get('assertion')} | no_edit={mix.get('no_edit')} | timeout={mix.get('timeout')} | apply={mix.get('apply')} | harness={mix.get('harness')} |")
print(f"| Status | {status} · task_yield_pct={d.get('task_yield_pct')} · stop_tasks={d.get('stop_tasks')} |")

# Circuit breaker: need enough rollouts AND >=3 distinct tasks before yield0 kill.
# (N=16 means the first 16 metas are one task — do not false-trip mid-task.)
distinct_tasks = len(d.get("task_metas") or {})
if metas >= 20 and distinct_tasks >= 3:
    yield0 = keepers == 0
    timeout = float(mix.get("timeout") or 0)
    harness = float(mix.get("harness") or 0)
    if yield0 and (timeout > 25.0 or harness > 5.0):
        Path("/tmp/p51r8_circuit_breaker.json").write_text(json.dumps({
            "reason": "yield0_timeout_or_harness",
            "mix": mix,
            "metas": metas,
            "distinct_task_ids": distinct_tasks,
        }))
        print("CIRCUIT_BREAKER_TRIP")
        raise SystemExit(42)

# Advise stop-sampling tasks (rejection_sample has no live skip API yet)
if d.get("stop_tasks"):
    print("STOP_SAMPLING_HINT", ",".join(d["stop_tasks"]))

if status in ("S3_PERSIST_DONE", "BON_COMPLETE", "BON_FAIL", "S3_PERSIST_FAIL"):
    print("TERMINAL", status)
    raise SystemExit(0)
PY
  RC=${PIPESTATUS[0]}

  if [ "$RC" = "42" ]; then
    echo "$TS | CIRCUIT_BREAKER — terminate pod=$POD" | tee -a "$LOG"
    terminate_pod | tee -a "$LOG"
    echo "$TS | done | P5.1a-r8_p314a | agent=cursor | hash=none | metrics=fail=circuit_breaker,pod=${POD}_terminated | next=diagnose_config_mismatch | blocker=circuit_breaker" >> "$ROOT/docs/experiment_log.md"
    exit 42
  fi

  if echo "$REMOTE" | grep -q '"status": "S3_PERSIST_DONE"\|"status": "BON_COMPLETE"\|"status": "BON_FAIL"\|"status": "S3_PERSIST_FAIL"'; then
    echo "$TS | terminal status — exit telemetry (watchdog owns terminate/sync)" | tee -a "$LOG"
    exit 0
  fi

  sleep "$POLL_S"
done
