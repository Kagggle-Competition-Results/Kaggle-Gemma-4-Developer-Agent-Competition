# Handoff / Audit Report — P5.1a-r8_p314a (2026-10-18)

**Audience:** new agent with no chat context.  
**Purpose:** audit progress, verify claims, choose next ticket.  
**Do not trust working-tree `gemma4-agent/agent/` as the harvest agent** — harvest uses freeze/S3 package `results/harvest_agent_p314a` (exact P3.14a: 8min / 28 calls / Verra prompt).

---

## 0) How to orient (5 minutes)

1. Read [`AGENTS.md`](../AGENTS.md) (G1–G16).
2. Live log (append-only truth): [`docs/experiment_log.md`](experiment_log.md).
3. This ticket stream: **P5.1a-r8_p314a** (clean Wave 1 BoN @ temp 0.75, N=16).
4. Working tree: `gemma4-agent/`. Zip boundary = `gemma4-agent/agent/` only (G5).

**CLOUD_APPROVAL (check log before any new pod):** last relevant entries allow RunPod up to **$0.70/hr**, purpose P5.1a-r8, expires **2026-10-19** (re-read log — do not invent spend).

---

## 1) Mission stand (competition)

| Item | State |
| --- | --- |
| Public baseline | **0.18** (Verra) |
| Path | scaffold freeze → BoN+STaR → DoRA SFT → GRPO → TIES (Plan A) |
| Gates | `GATE_P2=PASS`, `GATE_P3=FAIL+PIVOT`, `GATE_P4=PIVOT` (scaffold frozen) |
| Ship model | `gemma-4-31b-it-qat-w4a16-ct` (G6) |
| Train readiness | **blocked on keeper/task coverage** — DoRA not started |

---

## 2) What just finished (P5.1a-r8 recovery + Wave 1)

### Root cause (critical — audit this first)

Subprocess harvest/oracle was **poisoning** src-layout repos and FastAPI:

1. **`SubprocessManager` `PYTHONPATH`** was `{workspace}` only → `requests`/`httpx` imported **host site-packages**, so gold/agent patches never ran.
2. **`starlette` 1.x** (from vLLM/extras) broke FastAPI gold/oracle.
3. `install_test_dependencies` **no-ops** under `SubprocessManager` (host inherits bootstrap venv).

**Fix (idempotent scripts):**

| Script | Role |
| --- | --- |
| `scripts/patch_swegemma_subprocess.py` | `PYTHONPATH={ws}/src:{ws}`; pytest without `PYTHONSAFEPATH`/`-s`; DeprecationWarning filters; junit env-skip OK for `@needs_pydanticv1` |
| `scripts/ensure_subprocess_host_deps.py` | hard-pin **`starlette==0.46.2`** from wheelhouse |
| `scripts/gold_gate_wave1.py` | 12/12 gold hard gate before BoN |
| `scripts/reverify_rollouts_srcpath.py` | true-oracle re-verify saved rollouts |
| `scripts/quarantine_gold_unlock.py` | quarantine recheck helper |
| `scripts/p51r8_p314a_bootstrap.sh` | full pod bootstrap + gold gate + BoN remaining-11 |

### Pod timeline (do not resurrect dead pods)

| Pod | SKU | Outcome |
| --- | --- | --- |
| `s2zg73zeeolr17` | A40 $0.59 | Original Wave1; terminated mid-run |
| `6lrtqcis1wvffu` | RTX 5090 $0.69 | Gold/oracle OK; **vLLM CUDA Error 804** (unusable for harvest) |
| `zcy33a0djczyyh` | A40 $0.59 | Gold 12/12 then **BON_FAIL** (`host_drift` missing); watchdog killed pod |
| **`yf44e1spgzegbv`** | **A40 $0.59** | **Successful remaining-11 BoN** → `S3_PERSIST_DONE` |

**Live SSH meta:** `/tmp/p51r8_p314a_ssh.json` → `yf44e1spgzegbv` / `69.30.85.114:22107`.  
Ignore stale `results/p51a_r8_p314a/a40_ssh.json` if it still points at `zcy33a0djczyyh`.

### Wave 1 BoN result (audit KPIs)

| KPI | Value | Evidence |
| --- | --- | --- |
| Status | **`S3_PERSIST_DONE`** | pod `/workspace/p51r8_p314a.status` |
| Gold gate | **12/12 PASS** | `results/p51a_r8_p314a/gold_gate_wave1_a40.json` |
| Tasks run | **11** (7205 removed; keepers already secured) | `configs/p51a_r8_p314a_wave1_remaining.txt` |
| Config | N=16, temp=0.75, **concurrency=2**, **early_stop@2**, command_timeout=75 | bon log |
| nXX metas | **122** | `traces/bon/**/n*/meta.json` on pod |
| Keepers | **13** dirs | `traces/bon/keep/` on pod (+ S3 sync) |
| Wall | ~7.5h (`19:44Z` → `~03:14Z`) | bon log + status mtime |

**Early-stop lines (must see in bon log):**

- `requests_7505 EARLY_STOP keepers>=2 (skip 12 remaining)` → keep `n02`, `n04`
- `requests_6644 EARLY_STOP keepers>=2 (skip 12 remaining)`
- also: `httpx_3672`, `fastapi_14301`, `fastapi_14419`

**Keeper inventory (pod `traces/bon/keep`):**

```
requests_7505_n02, requests_7505_n04
requests_6644_n01, n03, n04
httpx_3672_n01, n02
fastapi_14301_n01, n02
fastapi_14419_n02, n14
rich_4075_n04, n16
```

**Zero-yield (full N=16, 0 keepers):** `fastapi_14186`, `fastapi_14262`, `fastapi_15280`, `rich_3934`, `rich_4077`.

### Pre-BoN oracle re-verify (7205 / 7505)

| Task | Pass / metas | Keepers into SFT |
| --- | --- | --- |
| `requests_7205` | 9 / 16 | **n02, n03** (206 passed each) |
| `requests_7505` | 8 / 14 | **n05, n10** |

Artifact: `results/p51a_r8_p314a/reverify_srcpath_manifest.json`  
Backups: `results/p51a_r8_p314a/backup_pre_restart/{requests_7205,requests_7505}/`

---

## 3) Training data stand

| File | Rows / tasks | Notes |
| --- | --- | --- |
| `gemma4-agent/data_train/sft.jsonl` | **71 / 13** | includes oracle `7205×2` + `7505×2` |
| Cap | **max 2 / task** | via early-stop + reverify `--max-keepers 2` (+ `filter_bon_keepers.py`). **No `curate_sft.py` exists.** |

**Gap:** Wave1 keepers for `requests_7505` on disk are **n02/n04**, but SFT still has prior oracle **n05/n10**. `requests_6644` / `httpx_3672` / fastapi / rich keepers are **not yet merged** into local `sft.jsonl`.

**Next merge ticket (suggested):** pull S3/pod `traces/bon/keep` → `filter_bon_keepers.py` → render → merge with **hard cap 2/task** (prefer newest true-oracle Wave1 keepers; do not exceed 2 for `7505`).

---

## 4) Quarantine unlock (audit carefully)

Hypothesis: some `soundness.csv` quarantine was false-negative from the same PYTHONPATH/starlette bugs.

**Recheck method:** target FAIL_TO_PASS (not full `verify_task` suite) under `PYTHONPATH=src` + `starlette==0.46.2`.  
Full-suite `verify_task` still fails many `requests_*` due to unrelated env flake: `test_redirecting_to_bad_url[http://localhost:-1-InvalidURL]`.

| Unlocked (5) → train_pool + Wave2 shards | Stayed quarantined (4 of the 9 checked) |
| --- | --- |
| `requests_6589`, `6592`, `6629` | `requests_6757` |
| `fastapi_14356`, `15763` | `fastapi_14077`, `15745`, `15785` |

**Also still quarantined (rich + others):** see `configs/splits.json` → `quarantine` (**13** IDs total after unlock). Rich quarantine is **not** explained by srcpath/starlette.

Artifacts:

- `results/p51a_r8_p314a/quarantine_gold_unlock.json` (full verify_task — 0 unlocks)
- `results/p51a_r8_p314a/quarantine_gold_unlock_targeted.json` (FAIL_TO_PASS — 5 unlocks)
- `results/p51a_r8_p314a/quarantine_unlock_applied.json`

**Wave2 shards:** `configs/wave2_shards/` now **59** selected (was 54); shortfall vs 160 still large. Meta: `wave2_shards_meta.json`.

**Review risk:** unlock used target-test logic, not Docker P1.1 soundness. Auditor should decide whether to require Docker re-soundness before training on unlocked IDs (G10/distill policy + harness_verify).

---

## 5) What an auditor should re-verify (checklist)

### A. Pod / harvest integrity

```bash
# SSH meta
python3 -c "import json;print(json.load(open('/tmp/p51r8_p314a_ssh.json')))"
# On pod (read-only)
cat /workspace/p51r8_p314a.status   # expect S3_PERSIST_DONE
ls /workspace/gemma4-p51/traces/bon/keep | wc -l   # expect 13
grep EARLY_STOP /workspace/p51r8_p314a_bon.log
# starlette + PYTHONPATH patch still present
.venv_bootstrap/bin/python -c "import starlette; print(starlette.__version__)"  # 0.46.2
grep GEMMA4_SRCPATH .venv_bootstrap/lib/python3.12/site-packages/swegemma/sandbox/subprocess.py
```

### B. Local artifacts

```bash
python3 -c "import json;from collections import Counter;from pathlib import Path
rows=[json.loads(l) for l in Path('gemma4-agent/data_train/sft.jsonl').read_text().splitlines() if l.strip()]
print(len(rows), len(Counter(r['task_id'] for r in rows)))"
python3 -c "import json;print(json.load(open('gemma4-agent/results/p51a_r8_p314a/gold_gate_wave1_a40.json'))['all_pass'])"
python3 -c "import json;print(json.load(open('gemma4-agent/configs/splits.json'))['quarantine_unlock_ids'])"
```

### C. S3 (if creds present)

```text
s3://gemma4-dev-agent-data-650825122703/results/p51a_r8_p314a/
  traces/bon/…, gold_gate_wave1.json, p51r8_p314a_bon.log, …
```

### D. Policy / process traps

- Do **not** start Wave2 until Wave1 keepers are filtered/merged and gold-gate policy is still enforced.
- Do **not** use RTX 5090 image/torch stack until CUDA 804 is fixed (Blackwell).
- Hard watchdog **terminates pod on `BON_FAIL`** — ensure `host_drift.py` is on `/workspace/` before bootstrap (bootstrap now hard-fails `MISSING_HOST_DRIFT`).
- Never train/peek **holdout** (G4).

---

## 6) Recommended next tickets (pick one)

| Priority | Ticket | Why |
| --- | --- | --- |
| P0 | **Merge Wave1 keepers → SFT** | 13 keepers on pod/S3 not in local `sft.jsonl`; cap 2/task; drop stale 7505 n05/n10 if replacing with n02/n04 after filter |
| P1 | **Autopsy zero-yield tasks** | 5 tasks × 16 = 80 wasted rollouts; decide prompt vs task hardness vs residual host-deps |
| P2 | **Docker soundness recheck** on 5 unlocked quarantine IDs | confirm unlock is not subprocess-only false positive |
| P3 | **Wave2 Stage-1** on updated shards (59) | only after P0; still needs CLOUD_APPROVAL + multi-pod plan |
| P4 | Terminate idle A40 if no pull needed | `yf44e1spgzegbv` @$0.59/hr burns while `S3_PERSIST_DONE` |

---

## 7) Key paths index

```
AGENTS.md
docs/experiment_log.md
docs/2026-10-18-handoff-audit.md          ← this file
gemma4-agent/configs/p51a_r8_p314a_wave1.txt
gemma4-agent/configs/p51a_r8_p314a_wave1_remaining.txt
gemma4-agent/configs/splits.json
gemma4-agent/configs/wave2_shards/
gemma4-agent/data_train/sft.jsonl
gemma4-agent/results/harvest_agent_p314a/   ← harvest agent package
gemma4-agent/results/p51a_r8_p314a/         ← gold/reverify/quarantine reports
gemma4-agent/scripts/p51r8_p314a_bootstrap.sh
gemma4-agent/scripts/rejection_sample.py
gemma4-agent/scripts/patch_swegemma_subprocess.py
gemma4-agent/scripts/filter_bon_keepers.py
gemma4-agent/docs/harness_verify.md
```

---

## 8) One-paragraph stand

Scaffold is frozen; public baseline 0.18. Subprocess oracle was fixed (`PYTHONPATH=src` + `starlette==0.46.2`); Wave1 gold is 12/12. Clean remaining-11 BoN on A40 `yf44e1spgzegbv` completed with 13 keepers and early-stop working; SFT is 71/13 but **missing the new Wave1 keepers merge**. Five quarantine IDs were unlocked via targeted FAIL_TO_PASS and added to Wave2 shards (59). Next value is **filter+merge keepers**, then autopsy zeros / optional Docker re-soundness — not another blind BoN restart.
