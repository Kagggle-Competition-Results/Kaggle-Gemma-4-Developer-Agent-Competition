#!/usr/bin/env bash
# P5.2c one-shot poll tick: sync pod→local→S3, filter/merge SFT, dry-runs, safe disk prune.
# Never deletes snapshots / model / venv / traces. Exit 0=rolling, 42=S3_PERSIST_DONE.
set -euo pipefail
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy || true

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
GA="$ROOT/gemma4-agent"
BUCKET=gemma4-dev-agent-data-650825122703
META=/tmp/p52_wave2_ssh.json
KEY="${SSH_KEY:-$HOME/.ssh/casmi_runpod}"
TICK="${TICK:-unknown}"
# shellcheck disable=SC1091
set -a; source /tmp/p51_aws_creds.env 2>/dev/null || true; set +a
export AWS_DEFAULT_REGION=us-east-1

HOST=$(python3 -c "import json;print(json.load(open('$META'))['host'])")
PORT=$(python3 -c "import json;print(json.load(open('$META'))['port'])")
POD=$(python3 -c "import json;print(json.load(open('$META'))['pod_id'])")
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o BatchMode=yes -p "$PORT" root@"$HOST")

POD_OUT=$("${SSH[@]}" 'echo STATUS=$(cat /workspace/p52_wave2.status 2>/dev/null||echo none)
curl -sf --max-time 3 http://127.0.0.1:8000/v1/models >/dev/null && echo vllm=200 || echo vllm=DOWN
echo KEEP=$(ls /workspace/gemma4-p51/traces/bon/keep 2>/dev/null|wc -l|tr -d " ")
echo META=$(find /workspace/gemma4-p51/traces/bon -name meta.json 2>/dev/null|wc -l|tr -d " ")
df -h / | awk "NR==2{print \"disk=\"\$5\" avail=\"\$4}"
pgrep -af "scripts/rejection_sample.py" | head -1 || echo NO_RS
find /workspace/gemma4-p51/traces/bon -name meta.json -printf "%T@ %p\n" 2>/dev/null | sort -n | tail -3
date -u +now=%Y-%m-%dT%H:%M:%SZ
' 2>/dev/null | grep -v Warning || true)

STATUS=$(echo "$POD_OUT" | sed -n 's/^STATUS=//p' | head -1)
KEEP=$(echo "$POD_OUT" | sed -n 's/^KEEP=//p' | head -1)
PMETA=$(echo "$POD_OUT" | sed -n 's/^META=//p' | head -1)
DISK=$(echo "$POD_OUT" | sed -n 's/^disk=//p' | head -1)
VLLM=$(echo "$POD_OUT" | sed -n 's/^vllm=//p' | head -1)
echo "POD=$POD ssh=$HOST:$PORT STATUS=$STATUS KEEP=$KEEP META=$PMETA disk=$DISK vllm=$VLLM"
echo "$POD_OUT" | tail -n 8

# Safe disk prune if >=80% (never snapshots/venv/traces/models)
PCT=$(echo "$DISK" | sed -n 's/^\([0-9]*\)%.*/\1/p')
if [ "${PCT:-0}" -ge 80 ] 2>/dev/null; then
  echo "DISK_PRUNE pct=$PCT"
  DISK=$("${SSH[@]}" 'rm -rf /tmp/pytest-* /tmp/pip-* /tmp/torch_* /tmp/cuda_probe \
    /tmp/vllm_local_wheels /tmp/vllm_dist_* /tmp/torchinductor_* \
    /root/.cache/pip /root/.cache/uv /root/.cache/huggingface/download/*.incomplete 2>/dev/null || true
  find /tmp -maxdepth 1 -type d \( -name "tmp*" -o -name "pytest-*" -o -name "swegemma*" \) \
    -mmin +60 -print0 2>/dev/null | xargs -0 rm -rf 2>/dev/null || true
  find /tmp -maxdepth 1 -type d -regextype posix-extended \
    -regex ".*/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}" \
    -mmin +60 -print0 2>/dev/null | xargs -0 rm -rf 2>/dev/null || true
  rm -rf /workspace/gemma4-p51/tmp/* 2>/dev/null || true
  df -h / | awk "NR==2{print \$5\" avail \"\$4}"
  ' 2>/dev/null | grep -v Warning | tail -1)
  echo "disk_after=$DISK"
fi

BON_LOCAL="$GA/results/p52_wave2_sieve/traces/bon"
mkdir -p "$BON_LOCAL/keep" "$BON_LOCAL/raw"
if command -v rsync >/dev/null 2>&1; then
  rsync -az -e "ssh -i $KEY -p $PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15" \
    "root@${HOST}:/workspace/gemma4-p51/traces/bon/keep/" "$BON_LOCAL/keep/" || true
  rsync -az -e "ssh -i $KEY -p $PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15" \
    --include='*/' --include='**/meta.json' --include='**/patch.diff' --include='**/trace.json' --exclude='*' \
    "root@${HOST}:/workspace/gemma4-p51/traces/bon/raw/" "$BON_LOCAL/raw/" || true
fi
aws s3 sync "$BON_LOCAL/" "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" --only-show-errors || true
aws s3 sync "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" "$BON_LOCAL/" --only-show-errors || true
S3N=$(aws s3 ls "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" --recursive 2>/dev/null | wc -l | tr -d ' ')

python3 - <<'PY' > /tmp/p52c_tick_stats.json
import json
from pathlib import Path
root = Path("/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition/gemma4-agent")
bon = root/"results/p52_wave2_sieve/traces/bon"
live = [ln.strip() for ln in (root/"configs/p52_wave2_bon_live.txt").read_text().splitlines() if ln.strip()]
keep = list((bon/"keep").iterdir()) if (bon/"keep").is_dir() else []
raw = bon/"raw"
done = 0
for t in live:
    n03 = raw/t/"n03"/"meta.json"
    k = [p for p in keep if p.name.startswith(t+"_n")]
    if n03.exists() or len(k) >= 2:
        done += 1
metas = len(list(bon.rglob("meta.json")))
sft = [json.loads(l) for l in (root/"data_train/sft.jsonl").read_text().splitlines() if l.strip()]
print(json.dumps({"completed": done, "keepers": len(keep), "metas": metas,
                  "sft_rows": len(sft), "sft_tasks": len({r.get("task_id") for r in sft})}))
PY
COMPLETED=$(python3 -c "import json;print(json.load(open('/tmp/p52c_tick_stats.json'))['completed'])")
KEEPERS=$(python3 -c "import json;print(json.load(open('/tmp/p52c_tick_stats.json'))['keepers'])")
METAS=$(python3 -c "import json;print(json.load(open('/tmp/p52c_tick_stats.json'))['metas'])")

rm -rf "$GA/results/p52_wave2_sieve/bon_clean_rolling"
python3 "$GA/scripts/filter_bon_keepers.py" \
  --in "$BON_LOCAL" \
  --out "$GA/results/p52_wave2_sieve/bon_clean_rolling" \
  --splits "$GA/configs/splits.json" \
  --max-loop-pairs 2 --max-keepers 2 \
  --summary "$GA/results/p52_wave2_sieve/bon_clean_rolling_summary.json" >/tmp/p52c_filter_out.txt

python3 - <<'PY' | tee /tmp/p52c_merge.json
import json, hashlib, subprocess, sys
from pathlib import Path
from collections import Counter
root = Path("/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition/gemma4-agent")
keep_dir = root/"results/p52_wave2_sieve/bon_clean_rolling/keep"
out_jsonl = root/"results/p52_wave2_sieve/filtered_sft.jsonl"
sft_path = root/"data_train/sft.jsonl"
r = subprocess.run(
    [sys.executable, str(root/"scripts/render_trajectories.py"),
     "--in", str(keep_dir), "--out", str(out_jsonl), "--mask-tool-results"],
    capture_output=True, text=True,
)
ok = r.returncode == 0
rows = [json.loads(l) for l in sft_path.read_text().splitlines() if l.strip()]
existing = {hashlib.sha1(json.dumps(x, sort_keys=True).encode()).hexdigest() for x in rows}
tc = Counter(x.get("task_id") for x in rows)
added = []
if ok and out_jsonl.exists():
    for row in (json.loads(l) for l in out_jsonl.read_text().splitlines() if l.strip()):
        tid = row.get("task_id")
        h = hashlib.sha1(json.dumps(row, sort_keys=True).encode()).hexdigest()
        if h in existing or tc[tid] >= 2:
            continue
        rows.append(row); existing.add(h); tc[tid] += 1; added.append(tid)
    if added:
        bak = sft_path.with_suffix(".jsonl.bak_pre_p52c_poll")
        if not bak.exists():
            bak.write_text(sft_path.read_text())
        sft_path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
print(json.dumps({
    "render_ok": ok, "added": len(added), "added_tasks": added[:20],
    "sft_rows": len(rows), "sft_tasks": len(tc),
    "render_err": None if ok else (r.stderr or r.stdout)[-500:],
}))
PY

python3 "$GA/scripts/train_qlora.py" \
  --config "$GA/configs/train_qlora.yaml" \
  --data "$GA/data_train/sft.jsonl" \
  --out /tmp/p52c_qlora_unused \
  --dry-run > /tmp/p52c_qlora_dry.out 2> /tmp/p52c_qlora_dry.log || true
python3 "$GA/scripts/train_grpo.py" \
  --config "$GA/configs/train_grpo.yaml" \
  --out /tmp/p52c_grpo_unused \
  --dry-run > /tmp/p52c_grpo_dry.out 2> /tmp/p52c_grpo_dry.log || true

python3 "$GA/scripts/p52c_parse_dry_kpis.py"

SFT_NOW=$(python3 -c "import json;d=json.load(open('/tmp/p52c_merge.json'));print(f\"{d['sft_rows']}/{d['sft_tasks']}\")")
ADDED=$(python3 -c "import json;print(json.load(open('/tmp/p52c_merge.json'))['added'])")
KPI=$(python3 -c "import json;d=json.load(open('/tmp/p52c_dry_kpis.json'));print(f\"trunc={d.get('n_truncated')},leak={d.get('tool_response_leak')},grpo_active={d.get('grpo_active')},grpo_mixed={d.get('grpo_mixed')},pos_loop_gt2={d.get('pos_loop_gt2')},active_eq_mixed={d.get('n_active_eq_n_mixed')}\")")
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HASH=$(cd "$ROOT" && git rev-parse --short HEAD 2>/dev/null || echo none)

python3 - <<PY
import json
from pathlib import Path
p = Path("$GA/results/p52_wave2_sieve/p52c_final_merge_report.json")
rep = {
  "status": "DONE" if "$STATUS" == "S3_PERSIST_DONE" else "ROLLING",
  "tick": "$TICK",
  "pod": "$POD",
  "ssh": "$HOST:$PORT",
  "wave2_status": "$STATUS",
  "completed": int("$COMPLETED"),
  "keepers": int("$KEEPERS"),
  "metas": int("$METAS"),
  "s3_bon_objects": int("$S3N" or 0),
  "sft": "$SFT_NOW",
  "added": int("$ADDED"),
  "disk": "$DISK",
  "dry": json.loads(Path("/tmp/p52c_dry_kpis.json").read_text()),
  "ts": "$TS",
}
p.write_text(json.dumps(rep, indent=2) + "\n")
print(json.dumps(rep, indent=2))
PY

if [ "$STATUS" = "S3_PERSIST_DONE" ]; then
  cat >> "$ROOT/docs/experiment_log.md" <<EOF
$TS | done | P5.2c-merge-wave2-bon | agent=cursor | hash=$HASH | metrics=poll_tick=$TICK,pod=$POD,status=S3_PERSIST_DONE,completed=${COMPLETED}/49,keepers=$KEEPERS,metas=$METAS,s3=$S3N,sft=$SFT_NOW,added=$ADDED,$KPI,report=DONE | next=P5.6-dora-sft | blocker=none
EOF
  pkill -f 'p52_wave2_bon_incremental_pull' 2>/dev/null || true
  pkill -f 'watchdog_p52_wave2' 2>/dev/null || true
  pkill -f 'p52c_poll_loop' 2>/dev/null || true
  echo "FINALIZED S3_PERSIST_DONE"
  exit 42
fi

cat >> "$ROOT/docs/experiment_log.md" <<EOF
$TS | note | P5.2c-merge-wave2-bon | agent=cursor | hash=$HASH | metrics=poll_tick=$TICK,pod=$POD,status=$STATUS,completed=${COMPLETED}/49,keepers=$KEEPERS,metas=$METAS,s3=$S3N,sft=$SFT_NOW,added=$ADDED,disk=$DISK,vllm=$VLLM,$KPI | next=P5.2c-merge-wave2-bon | blocker=await_S3_PERSIST_DONE_wave2_bon
EOF
echo "TICK_OK tick=$TICK completed=$COMPLETED/49 keepers=$KEEPERS sft=$SFT_NOW s3=$S3N $KPI"
exit 0
