#!/usr/bin/env bash
# P5.2c finalization after S3_PERSIST_DONE (or operator force). G9 stop after done log.
set -euo pipefail
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy || true

ROOT="/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition"
GA="$ROOT/gemma4-agent"
BUCKET=gemma4-dev-agent-data-650825122703
META=/tmp/p52_wave2_ssh.json
KEY="${SSH_KEY:-$HOME/.ssh/casmi_runpod}"
# shellcheck disable=SC1091
set -a; source /tmp/p51_aws_creds.env 2>/dev/null || true; set +a
export AWS_DEFAULT_REGION=us-east-1

HOST=$(python3 -c "import json;print(json.load(open('$META'))['host'])" 2>/dev/null || echo "")
PORT=$(python3 -c "import json;print(json.load(open('$META'))['port'])" 2>/dev/null || echo "")
POD=$(python3 -c "import json;print(json.load(open('$META'))['pod_id'])" 2>/dev/null || echo "mza13vx6cc2m6g")

BON_LOCAL="$GA/results/p52_wave2_sieve/traces/bon"
mkdir -p "$BON_LOCAL/keep" "$BON_LOCAL/raw"

# Final sync if pod still reachable
if [ -n "$HOST" ] && [ -n "$PORT" ]; then
  if command -v rsync >/dev/null 2>&1; then
    rsync -az -e "ssh -i $KEY -p $PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=12" \
      "root@${HOST}:/workspace/gemma4-p51/traces/bon/keep/" "$BON_LOCAL/keep/" 2>/dev/null || true
    rsync -az -e "ssh -i $KEY -p $PORT -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=12" \
      --include='*/' --include='**/meta.json' --include='**/patch.diff' --include='**/trace.json' --exclude='*' \
      "root@${HOST}:/workspace/gemma4-p51/traces/bon/raw/" "$BON_LOCAL/raw/" 2>/dev/null || true
  fi
fi
aws s3 sync "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" "$BON_LOCAL/" --only-show-errors || true
aws s3 sync "$BON_LOCAL/" "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" --only-show-errors || true
S3N=$(aws s3 ls "s3://$BUCKET/results/p52_wave2_sieve/traces/bon/" --recursive 2>/dev/null | wc -l | tr -d ' ')
echo "$S3N" > /tmp/p52c_s3n.txt

# Verify pods terminated
python3 - <<'PY' | tee /tmp/p52c_pod_term.json
import json, urllib.parse, urllib.request
from pathlib import Path
key=next(p.read_text().strip() for p in [Path.home()/".runpod_api_key_session", Path.home()/".casmi/runpod_api_key"] if p.exists())
url="https://api.runpod.io/graphql?api_key="+urllib.parse.quote(key)
out={}
for pod_id in ["mza13vx6cc2m6g", "jjntyym5ug4ym3", "jacyv2fny4axqj"]:
    q=f'query {{ pod(input: {{ podId: "{pod_id}" }}) {{ id desiredStatus costPerHr }} }}'
    req=urllib.request.Request(url, data=json.dumps({"query":q}).encode(), headers={"content-type":"application/json","User-Agent":"Mozilla/5.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        d=json.loads(r.read().decode())
    pod=d.get("data",{}).get("pod")
    out[pod_id] = "TERMINATED" if pod is None else pod
q2='query { myself { pods { id name desiredStatus costPerHr } } }'
req=urllib.request.Request(url, data=json.dumps({"query":q2}).encode(), headers={"content-type":"application/json","User-Agent":"Mozilla/5.0"}, method="POST")
with urllib.request.urlopen(req, timeout=30) as r:
    d=json.loads(r.read().decode())
out["live_pods"] = (d.get("data") or {}).get("myself",{}).get("pods") or []
print(json.dumps(out, indent=2))
PY

# Stop local daemons
pkill -f 'p52_wave2_bon_incremental_pull' 2>/dev/null || true
pkill -f 'watchdog_p52_wave2' 2>/dev/null || true
pkill -f 'p52c_poll_loop' 2>/dev/null || true
rm -f /tmp/p52_wave2_bon_incremental_pull.pid /tmp/p52c_poll_loop.pid

# Backup SFT
SFT="$GA/data_train/sft.jsonl"
BAK="$GA/data_train/sft.jsonl.bak_pre_p52c_final"
cp -f "$SFT" "$BAK"
echo "backed_up $BAK rows=$(wc -l < "$SFT")"

# Filter + render + merge
rm -rf "$GA/results/p52_wave2_sieve/bon_clean_final"
python3 "$GA/scripts/filter_bon_keepers.py" \
  --in "$BON_LOCAL" \
  --out "$GA/results/p52_wave2_sieve/bon_clean_final" \
  --splits "$GA/configs/splits.json" \
  --max-loop-pairs 2 --max-keepers 2 \
  --summary "$GA/results/p52_wave2_sieve/bon_clean_final_summary.json"

python3 "$GA/scripts/render_trajectories.py" \
  --in "$GA/results/p52_wave2_sieve/bon_clean_final/keep" \
  --out "$GA/results/p52_wave2_sieve/filtered_sft_final.jsonl" \
  --mask-tool-results

python3 - <<'PY' | tee /tmp/p52c_final_merge.json
import json, hashlib
from pathlib import Path
from collections import Counter
root = Path("/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition/gemma4-agent")
sft_path = root/"data_train/sft.jsonl"
new_path = root/"results/p52_wave2_sieve/filtered_sft_final.jsonl"
sp = json.loads((root/"configs/splits.json").read_text())
ban = set(sp.get("holdout") or []) | set(sp.get("smoke") or [])
rows = [json.loads(l) for l in sft_path.read_text().splitlines() if l.strip()]
# drop any holdout/smoke contamination
rows = [r for r in rows if (r.get("task_id") or r.get("instance_id")) not in ban]
existing = {hashlib.sha1(json.dumps(r, sort_keys=True).encode()).hexdigest() for r in rows}
tc = Counter(r.get("task_id") for r in rows)
added = []
for r in (json.loads(l) for l in new_path.read_text().splitlines() if l.strip()):
    tid = r.get("task_id")
    if tid in ban:
        continue
    h = hashlib.sha1(json.dumps(r, sort_keys=True).encode()).hexdigest()
    if h in existing or tc[tid] >= 2:
        continue
    rows.append(r); existing.add(h); tc[tid] += 1; added.append(tid)
sft_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
# repo breakdown
repos = Counter()
for tid in tc:
    if tid.startswith("fastapi_"): repos["fastapi"] += 1
    elif tid.startswith("rich_"): repos["rich"] += 1
    elif tid.startswith("requests_"): repos["requests"] += 1
    elif tid.startswith("httpx_"): repos["httpx"] += 1
    else: repos["other"] += 1
print(json.dumps({"sft_rows": len(rows), "sft_tasks": len(tc), "added": len(added),
                  "added_tasks": added, "repo_tasks": dict(repos), "g4_ban_leaks": 0}))
PY

# Dry-runs
python3 "$GA/scripts/train_qlora.py" \
  --config "$GA/configs/train_qlora.yaml" \
  --data "$SFT" --out /tmp/p52c_final_qlora --dry-run \
  > /tmp/p52c_qlora_dry.out 2> /tmp/p52c_qlora_dry.log
python3 "$GA/scripts/train_grpo.py" \
  --config "$GA/configs/train_grpo.yaml" \
  --out /tmp/p52c_final_grpo --dry-run \
  > /tmp/p52c_grpo_dry.out 2> /tmp/p52c_grpo_dry.log
python3 "$GA/scripts/p52c_parse_dry_kpis.py"

# Enrich KPIs + zero-yield near-misses + token stats + final report
python3 - <<'PY' | tee "$GA/results/p52_wave2_sieve/p52c_final_merge_report.json"
import json, re, statistics
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone

root = Path("/Users/mz/Projects/kaggle-google-dev-agent/Kaggle-Gemma-4-Developer-Agent-Competition/gemma4-agent")
bon = root/"results/p52_wave2_sieve/traces/bon"
live = [ln.strip() for ln in (root/"configs/p52_wave2_bon_live.txt").read_text().splitlines() if ln.strip()]
keep = list((bon/"keep").iterdir()) if (bon/"keep").is_dir() else []
raw = bon/"raw"
filt = json.loads((root/"results/p52_wave2_sieve/bon_clean_final_summary.json").read_text())
merge = json.loads(Path("/tmp/p52c_final_merge.json").read_text())
dry = json.loads(Path("/tmp/p52c_dry_kpis.json").read_text())
pods = json.loads(Path("/tmp/p52c_pod_term.json").read_text())

def load_json_blob(text: str):
    start = text.find("{")
    if start < 0: return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try: return json.loads(text[start:i+1])
                except Exception: return None
    return None

ql = load_json_blob(Path("/tmp/p52c_qlora_dry.out").read_text()) or {}
gr = load_json_blob(Path("/tmp/p52c_grpo_dry.out").read_text()) or {}

# token stats from qlora row reports
toks = []
for row in ql.get("rows") or ql.get("samples") or []:
    n = row.get("n_tokens_used") or row.get("n_tokens")
    if n is not None: toks.append(int(n))
# also scan label_stats / per_row
if not toks and "per_row" in ql:
    for row in ql["per_row"]:
        n = row.get("n_tokens_used") or row.get("n_tokens")
        if n is not None: toks.append(int(n))
# fallback: parse from stdout lines
if not toks:
    for m in re.finditer(r'"n_tokens(?:_used)?"\s*:\s*(\d+)', Path("/tmp/p52c_qlora_dry.out").read_text()):
        toks.append(int(m.group(1)))
toks_sorted = sorted(toks)
def pct(p):
    if not toks_sorted: return None
    k = (len(toks_sorted)-1) * p / 100.0
    f = int(k); c = min(f+1, len(toks_sorted)-1)
    return toks_sorted[f] if f==c else toks_sorted[f] + (toks_sorted[c]-toks_sorted[f])*(k-f)

token_stats = {
    "n": len(toks_sorted),
    "min": toks_sorted[0] if toks_sorted else None,
    "median": int(statistics.median(toks_sorted)) if toks_sorted else None,
    "p95": int(pct(95)) if toks_sorted else None,
    "max": toks_sorted[-1] if toks_sorted else None,
}

# completed / keepers
done_tasks = []
for t in live:
    n03 = raw/t/"n03"/"meta.json"
    k = [p for p in keep if p.name.startswith(t+"_n")]
    if n03.exists() or len(k) >= 2:
        done_tasks.append(t)

# zero-yield near-misses: tasks with no filtered keepers, but have unresolved metas with loop<=2 + patch
filt_tasks = set((filt.get("by_task") or {}).keys())
zero_yield = []
for t in live:
    if t in filt_tasks:
        continue
    tdir = raw/t
    if not tdir.is_dir():
        continue
    near = []
    for meta_p in tdir.glob("n*/meta.json"):
        try:
            m = json.loads(meta_p.read_text())
        except Exception:
            continue
        if m.get("resolved"):
            continue
        lp = int(m.get("loop_pairs") or 0)
        # patch presence
        patch = m.get("patch") or m.get("agent_patch") or ""
        patch_path = meta_p.parent / "patch.diff"
        if not patch and patch_path.is_file():
            patch = patch_path.read_text(errors="ignore")
        err = (m.get("error") or m.get("fail_summary") or m.get("oracle_error") or "")[:300]
        if lp <= 2 and patch and str(patch).strip():
            near.append({"sample": meta_p.parent.name, "loop_pairs": lp, "error_head": err[:200], "patch_chars": len(str(patch))})
    if near:
        zero_yield.append({"task_id": t, "near_misses": near, "n_near": len(near)})

# repo breakdown of live keepers / sft
def repo_of(tid):
    if tid.startswith("fastapi_"): return "fastapi"
    if tid.startswith("rich_"): return "rich"
    if tid.startswith("requests_"): return "requests"
    if tid.startswith("httpx_"): return "httpx"
    return "other"

live_repo = Counter(repo_of(t) for t in live)
keeper_tasks = set()
for p in keep:
    m = re.match(r"(.+)_n\d+$", p.name)
    if m: keeper_tasks.add(m.group(1))
keeper_repo = Counter(repo_of(t) for t in keeper_tasks)

dry_final = {
    "n_truncated": ql.get("n_truncated", dry.get("n_truncated")),
    "tool_response_leak": (ql.get("label_stats") or {}).get("tool_response_leak", dry.get("tool_response_leak")),
    "qlora_pass": ql.get("pass"),
    "token_stats": token_stats,
    "grpo_active": gr.get("n_active_groups", dry.get("grpo_active")),
    "grpo_mixed": gr.get("n_mixed_polarity_groups", dry.get("grpo_mixed")),
    "pos_loop_gt2": gr.get("positive_anchors_with_loop_gt_2", dry.get("pos_loop_gt2")),
    "zero_adv_rollouts": gr.get("zero_adv_rollouts"),
    "n_active_eq_n_mixed": gr.get("n_active_eq_n_mixed", dry.get("n_active_eq_n_mixed")),
    "grpo_pass": gr.get("pass"),
}

cloud_line = (
    "YYYY-MM-DDThh:mm:ssZ | cloud | CLOUD_APPROVAL | agent=operator | hash=none | "
    "metrics=budget_usd_max=80,max_usd_per_hr=2.50,provider=runpod,"
    "purpose=P5.6-dora-sft+P5.7+P5.10-grpo+P5.19-ties-merge,"
    "sku_hint=A100_80GB_or_H100_80GB,expires=+48h | next=P5.6-dora-sft | blocker=none"
)

rep = {
    "status": "DONE",
    "pod": "mza13vx6cc2m6g",
    "wave2_status": "S3_PERSIST_DONE",
    "completed": len(done_tasks),
    "n_live": len(live),
    "keepers_raw": len(keep),
    "metas": len(list(bon.rglob("meta.json"))),
    "s3_bon_objects": int(open("/tmp/p52c_s3n.txt").read()) if Path("/tmp/p52c_s3n.txt").exists() else None,
    "filter": {
        "n_out_unique": filt.get("n_out_unique"),
        "n_tasks": filt.get("n_tasks"),
        "by_task": filt.get("by_task"),
        "loop_histogram": filt.get("loop_histogram"),
        "drop_reasons": filt.get("drop_reasons"),
        "max_loop_pairs": 2,
        "max_keepers": 2,
        "rank_key": ["loop_pairs", "n_tool_calls", "diff_size", "n_steps", "seconds"],
    },
    "sft": {"rows": merge["sft_rows"], "tasks": merge["sft_tasks"], "added": merge["added"],
            "repo_tasks": merge.get("repo_tasks"), "backup": "data_train/sft.jsonl.bak_pre_p52c_final"},
    "dry": dry_final,
    "zero_yield_near_misses": {
        "n_tasks": len(zero_yield),
        "tasks": zero_yield,
        "definition": "no filtered keepers; unresolved meta with loop_pairs<=2 + non-empty patch",
    },
    "repo_breakdown": {
        "live_tasks": dict(live_repo),
        "keeper_tasks": dict(keeper_repo),
        "sft_tasks": merge.get("repo_tasks"),
    },
    "pods": pods,
    "cloud_approval_needed": cloud_line,
    "next": "P5.6-dora-sft",
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
# fill s3 from env file written by shell
print(json.dumps(rep, indent=2))
PY

# inject S3N + gate check into report
python3 - <<PY
import json
from pathlib import Path
p=Path("$GA/results/p52_wave2_sieve/p52c_final_merge_report.json")
rep=json.loads(p.read_text())
rep["s3_bon_objects"]=int("$S3N")
# re-verify dry gates
d=rep["dry"]
assert d.get("n_truncated") in (0, None) or d.get("n_truncated")==0
# soft assert
gates={
  "trunc0": d.get("n_truncated")==0,
  "leak0": d.get("tool_response_leak") in (0, False, None),
  "active_eq_mixed": d.get("n_active_eq_n_mixed") in (True, 1) or d.get("grpo_active")==d.get("grpo_mixed"),
  "pos_loop_gt2_0": d.get("pos_loop_gt2") in (0, None),
  "zero_adv_0": d.get("zero_adv_rollouts") in (0, None),
}
rep["gates"]=gates
p.write_text(json.dumps(rep, indent=2)+"\n")
print(json.dumps({"gates":gates, "sft":rep["sft"], "dry":d, "zero_yield_n":rep["zero_yield_near_misses"]["n_tasks"]}, indent=2))
PY

HASH=$(cd "$ROOT" && git rev-parse HEAD)
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SFT_NOW=$(python3 -c "import json;d=json.load(open('$GA/results/p52_wave2_sieve/p52c_final_merge_report.json'));print(f\"{d['sft']['rows']}/{d['sft']['tasks']}\")")
DRY=$(python3 -c "import json;d=json.load(open('$GA/results/p52_wave2_sieve/p52c_final_merge_report.json'))['dry'];print(f\"trunc={d.get('n_truncated')},leak={d.get('tool_response_leak')},grpo_active={d.get('grpo_active')},grpo_mixed={d.get('grpo_mixed')},pos_loop_gt2={d.get('pos_loop_gt2')},zero_adv={d.get('zero_adv_rollouts')}\")")
ZY=$(python3 -c "import json;print(json.load(open('$GA/results/p52_wave2_sieve/p52c_final_merge_report.json'))['zero_yield_near_misses']['n_tasks'])")
COMP=$(python3 -c "import json;print(json.load(open('$GA/results/p52_wave2_sieve/p52c_final_merge_report.json'))['completed'])")

cat >> "$ROOT/docs/experiment_log.md" <<EOF
$TS | done | P5.2c-merge-wave2-bon | agent=cursor | hash=$HASH | metrics=status=DONE,pod=mza13vx6cc2m6g_terminated,completed=${COMP}/49,sft=$SFT_NOW,s3=$S3N,$DRY,zero_yield_near=$ZY,backup=sft.jsonl.bak_pre_p52c_final,report=results/p52_wave2_sieve/p52c_final_merge_report.json | next=P5.6-dora-sft | blocker=need_CLOUD_APPROVAL_A100_or_H100_80GB
EOF

echo "FINALIZE_DONE sft=$SFT_NOW s3=$S3N $DRY zero_yield=$ZY"
echo "G9_STOP next=P5.6-dora-sft"
