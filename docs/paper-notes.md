# Paper track notes — Gemma 4 Developer Agent

**Status:** raw notes only (not a draft paper). Review before opting into paper track (~Oct 20 decision; submit by **Nov 12, 2026 23:59 UTC**).

**Competition:** [gemma-4-developer-agent](https://www.kaggle.com/competitions/gemma-4-developer-agent) · Paper: [gemma-4-developer-agent-paper](https://www.kaggle.com/competitions/gemma-4-developer-agent-paper)

**Last updated:** 2026-10-07 (post GATE_P3 FAIL, P3.7 keep temp 0.0, P3.2b on Kaggle v19)

**Stand:** GATE_P2 PASS · GATE_P3 FAIL · kept smoke Pass@1 **0.20** · official P2.7 score still **PENDING** · P4/P5 blocked.

**Sources of truth:** [`experiment_log.md`](./experiment_log.md) · `gemma4-agent/results/*` · [`agent-dev-spec.md`](./agent-dev-spec.md)

---

## 1. Setting (fixed host)

| Item | Value |
| --- | --- |
| Model | `gemma-4-31b-it-qat-w4a16-ct` (fixed; QAT W4A16) |
| Context | 32k; scored on Kaggle ADK + 4×L4 |
| Wall clock | **12h** for ~120 hidden tasks |
| Tools | 9 host tools only (workspace + graph) |
| Metric | resolution rate; near-miss = 0 (all-or-nothing pytest) |
| Submit | ADK zip (`agent/` only); ≤1/day; ≤2 finals |
| Our smoke split | 10 tasks (`configs/splits.json:smoke`) |
| Our dev split | 35 tasks (not yet used for Pass@1 ablations) |

---

## 2. Candidate theses (pick later)

1. **Aggressive tool-call caps destroy resolve under fixed Gemma-4 ADK** — community “budget-fit” (28 calls / 8 min) → 0.0 Pass@1 on smoke while baseline 60 calls recovers non-zero.
2. **Edit hygiene beats budget gymnastics** — `/tmp` repro + exact `old_str` (P3.2) recovered 0.10 → 0.20; budget discipline alone (P3.3) was flat.
3. **Localization proxies ≠ agent Pass@1** — graph file Hit@5 &lt;60% suggested grep-first; shipping grep-first **hurt** Pass@1 (0.20 → 0.10).
4. **32k context cliff is real and fixable without training** — ContextWindowExceed → compaction + lower `max_out` + read caps got first non-zero smoke (0.20).
5. **Scaffold KPIs can fail while Pass@1 looks OK** — GATE_P3 FAIL on failed_edit/loops despite kept Pass@1=0.20.

*Do not claim causality beyond smoke n=10 until `dev` + public LB replicate.*

---

## 3. Ablation ledger (one variable each; smoke n=10)

| ID | Change | Provider | Pass@1 | Resolved | Keep? | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| P2.6 v12 | Scaffold + compaction (`token_threshold=14336`, retention 5), `max_out=2048`, thoughts off, read cap 40 | Kaggle L4 | **0.20** | 2/10 | baseline | passes: `rich_3882`, `rich_3905` |
| P3.1 community | budgets 8 min / 28 calls / timeout 240 / turns 80 | RunPod A6000 | **0.00** | 0/10 | **no** | mean 175s; extrap 5.83h; hit call/turn/time caps; patches often non-empty but tests fail |
| P3.1 baseline | budgets 5 min / 60 calls / timeout 300 / turns 80 | Kaggle v13 | **0.10** | 1/10 | yes vs community | positive control (same harness class as P2.6) |
| P3.3 | + anti-loop / edit-by-12 / get_status~8 / &lt;25% cutoff | Kaggle | **0.10** | 1/10 | flat keep | same pass as baseline control |
| P3.2 | + `/tmp/repro.py` + exact `old_str` hygiene | Kaggle | **0.20** | 2/10 | **yes** | +0.10 vs P3.3; passes: `rich_3882`, `rich_3905` |
| P3.5 | nav → grep-first (after Hit@5) | Kaggle v17 | **0.10** | 1/10 | **no** | −0.10 vs P3.2; reverted to graph-first |
| P3.4 / P3.6 | `max_out`↓, `include_thoughts:false` | (folded into P2.6) | — | — | kept | done early for context |
| P3.7 | `temperature: 0.0` (was 0.2) | Kaggle v18 | **0.20** | 2/10 | **yes** | flat vs 0.2; skip 0.7; keep deterministic 0.0 |
| P3.2b | host `old_string`/`new_string` + max 5 edits/task | Kaggle v19 | TBD | TBD | **running** | P3.7 traces: failed_edit≈0.65; 34/40 bad args |

### Per-task highlights (kept vs rejected)

**P3.2 kept (0.20):** `rich_3882`✓, `rich_3905`✓; several non-empty patches still unresolved; one 5‑min timeout.

**P3.1 community (0.00):** 5/10 non-empty patches; failures mix: turn cap, 28-call cap, 8‑min timeout, “patched but tests failed” — not a harness `ModuleNotFound` (that was the discarded authlib run).

**P3.5 grep-first (0.10):** lost `rich_3882` vs P3.2; still solved `rich_3905`.

---

## 4. Budgets & 12h host clock

| Config | timeout_s | max_tool_calls | max_time_min | max_turns | Smoke Pass@1 | Extrap h/120 |
| --- | --- | --- | --- | --- | --- | --- |
| community_budget_fit | 240 | 28 | 8 | 80 | 0.00 | **5.83** (mean wall) |
| baseline (shipped) | 300 | 60 | 5 | 80 | 0.10–0.20 | not remeasured on Kaggle wall |

**Host implication:** `5 min × 120 ≈ 10h` agent time alone + load/overhead → easy to approach/exceed **12h** official scoring. First public submit (P2.7, 2026-10-06) still `PENDING` after ~12h — watch for partial score / hard stop.

**Selection rule (P3.1):** max resolve subject to `extrapolated_hours_120 ≤ 10` → community rejected despite OK extrap.

Artifact: `gemma4-agent/results/budget_pareto.json`

---

## 5. Localization diagnostic (P3.5)

Script: `gemma4-agent/scripts/measure_loc_hitk.py`  
Artifact: `gemma4-agent/results/loc_hitk.json` (+ `_smoke` / `_dev`)

| Split | n | Lexical Hit@5 | Graph→file Hit@5 | Threshold | Auto-recommend |
| --- | --- | --- | --- | --- | --- |
| smoke | 10 | **0.50** | **0.40** | 0.60 | grep_first |
| dev | 35 | **0.43** | **0.29** | 0.60 | grep_first |

**Outcome:** shipping grep-first **regressed** Pass@1 → **keep graph-first** in agent; diagnostic still valuable for paper (“proxy ≠ policy”).

Method note: TF-IDF over graph node text; file paths inferred from symbol names (`rich.progress.*` → `rich/progress.py`). Not identical to host `search_similar_code`.

---

## 6. GATE_P3 review (2026-10-07)

Artifact: `gemma4-agent/results/gate_p3_review.json`  
**Decision: FAIL** → do not unlock Phase 4/5 on these KPIs.

| KPI | Threshold | P2.6 (v12 traces) | P3 proxy (P3.5 traces*) | OK? |
| --- | --- | --- | --- | --- |
| Failed edit rate | &lt;10% and ≤½ P2 | **0.375** (3/8) | **0.971** (100/103) | no |
| Loop rate (identical consecutive tool+args) | &lt;5% | **0.387** | **0.454** | no |
| No-edit-by-12 rate | ↓ vs P2 | **0.90** | **0.80** | slight ↓ |
| Peak tokens | ≤28k | 16481 | 15932 | yes |
| vLLM refuse | 0 | 0 | 0 | yes |
| Extrap h | ≤10 | — | 5.83 (community) | yes |
| Graph loc hit | ≥60% or kill | — | 0.40 / 0.29 | kill attempted, reverted |

\*P3.5 traces include rejected nav + P3.2/P3.3 skills — pessimistic proxy for edit quality; **re-measure on pure P3.2 traces** before paper claims.

Also notable: `rich_3882` alone had **69** `edit_file` calls in P3.5 (edit thrash / loops).

---

## 7. Context / sampling evolution

| Knob | Early pain | Landed value (kept) |
| --- | --- | --- |
| `max_output_tokens` | 16384 / 8192 overflowed 32k | **2048** |
| `include_thoughts` | starter on | **false** |
| `thinking_budget` | 4096 → 2048 | **1024** |
| EventsCompaction | ContextWindowExceeded | `token_threshold=14336`, `event_retention_size=5` |
| `read_file` skill cap | 200 → | **40 lines** |
| temperature | 0.2 | **0.0** kept (P3.7 flat Pass@1; 0.7 skipped) |

Serve parity (P0.6): A6000 48GB, vLLM 0.31.0, load ~65–100s, FlashInfer sampler off (`VLLM_USE_FLASHINFER_SAMPLER=0`).

---

## 8. Official submission status

| Field | Value |
| --- | --- |
| First submit | 2026-10-06 22:21 UTC |
| Zip SHA256 | `b92db00d1f7cb64d8ddeb0a045634e632e0582790165a803b89e0cf7ef0c363e` |
| Message | GATE_P2 smoke 0.20 v12 |
| Status (as of notes) | **PENDING** (past ~12h wall — expect score or hard-stop partial) |
| Daily limit | 1/day |

**Next-submit engineering notes:** lower mean wall (3–4 min caps or fewer calls) so 120 tasks finish under 12h; ship kept stack (P3.2+P3.3+graph-first+baseline), not community budgets.

---

## 9. Kept agent stack (working tree)

Paths under `gemma4-agent/agent/`:

- `eval_config.yaml` — baseline 300 / 60 / 5 / 80
- `configs/sampling.yaml` — temp **0.0**, max_out 2048, thoughts false
- `skills/patch-rules` — `/tmp` repro + host `old_string`/`new_string` + max 3 edits/file, 5/task (P3.2+P3.2b)
- `skills/debug-workflow` — repro before edit (P3.2)
- `skills/anti-loop` — edit-by-12 + budget discipline + edit thrash stop (P3.3+P3.2b)
- `skills/nav-rules` — **graph-first** (P3.5 grep-first reverted)
- `prompts/system.md` — G11 + edit-by-12 + host edit args + max 5 edits/task

Zip boundary: **`agent/` only** (G5).

---

## 10. Negative results worth keeping

1. Community budget-fit (8/28/240) is **not** a free lunch on this host/model — 0/10 smoke.
2. Graph Hit@5 &lt;60% **does not** justify grep-first without a Pass@1 A/B.
3. Authlib-missing RunPod run → fake 0.0 in ~5s/task — always check wall time / error diversity before citing.
4. Laptop→SSH model copy wastes GPU $; direct S3 (presign/sync) required.
5. Smoke n=10 is **noisy** (P2.6 0.20 vs baseline re-run 0.10 same budgets).

---

## 11. Gaps before a real paper

- [ ] Public LB score for ≥1 zip
- [ ] Pass@1 on **`dev` (35)** for kept stack + key ablations
- [ ] Failed_edit / loop rates on **P3.2-only** traces (not P3.5 proxy)
- [ ] Replicate community vs baseline on same provider
- [ ] Peak-token / refuse rates logged per run
- [ ] Optional: training chapter (DoRA/GRPO) — only after GATE_TRAIN path
- [ ] Error bars / multiple seeds (or at least two smoke reshuffles)

---

## 12. Artifact index

| Artifact | Path |
| --- | --- |
| Experiment log | `docs/experiment_log.md` |
| GATE_P3 | `gemma4-agent/results/gate_p3_review.json` |
| Budget Pareto | `gemma4-agent/results/budget_pareto.json` |
| Loc Hit@k | `gemma4-agent/results/loc_hitk.json` |
| Community smoke | `gemma4-agent/results/p31_community_budget_fit_smoke.json` |
| Baseline Kaggle | `gemma4-agent/results/p31_baseline_kaggle_smoke.json` |
| P3.3 / P3.2 / P3.5 / P3.7 | `results/p33_smoke.json`, `p32_smoke.json`, `p35_smoke.json`, `p37_smoke.json` |
| P2.6 / P3.5 / P3.7 traces | `results/smoke_traces/`, `p35_traces/`, `p37_traces/` |
| Loc Hit@k script | `gemma4-agent/scripts/measure_loc_hitk.py` |
| Smoke kernel | `moranzavdi/gemma4-p26-smoke-swegemma` (latest ablation tag in notebook) |
| Agent dataset | `moranzavdi/gemma4-p26-agent-smoke` |

---

## 13. Changelog for this file

| Date | Note |
| --- | --- |
| 2026-10-07 | Initial dump through GATE_P3 FAIL + P3.7 kickoff |
| 2026-10-07 | P3.7 keep temp 0.0; skip 0.7; P3.2b edit-arg/thrash caps on Kaggle v19; stand board sync |

*Append new measured rows here when ablations finish; do not invent numbers.*
