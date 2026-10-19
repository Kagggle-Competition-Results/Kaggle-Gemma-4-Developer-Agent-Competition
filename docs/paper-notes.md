# Paper track notes — Gemma 4 Developer Agent

**Status:** raw notes only (not a draft paper). Review before opting into paper track (~Oct 20 decision; submit by **Nov 12, 2026 23:59 UTC**).

**Competition:** [gemma-4-developer-agent](https://www.kaggle.com/competitions/gemma-4-developer-agent) · Paper: [gemma-4-developer-agent-paper](https://www.kaggle.com/competitions/gemma-4-developer-agent-paper)

**Last updated:** 2026-10-12 (P5.1a-r7d done: oracle PASS; 0 keepers / 80 metas; GPU off; SFT **67/11**)

**Stand:** GATE_P2 PASS · GATE_P3 FAIL+**PIVOT** · GATE_P4 **PIVOT** single · public baseline **0.18** · wall **4** · SFT **67/11** · P3.15a 12B FAIL · **RunPod off**.

**Plan doc:** [`scaffold-freeze-train-plan.md`](./scaffold-freeze-train-plan.md)

**Single source of truth for live status:** [`experiment_log.md`](./experiment_log.md)
**Supporting references:** `gemma4-agent/results/*` · [`agent-dev-spec.md`](./agent-dev-spec.md)

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
| Our dev split | 35 tasks (`configs/splits.json:dev`; Pass@1 **0.1714** on P3.9) |

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
| P3.2b | host `old_string`/`new_string` + max 5 edits/task | Kaggle v19 | **0.10** | 1/10 | **no** | failed_edit=0.9114; loop=0.5507; revert_P3.2b |
| P3.8 | `max_time_minutes` **5 → 4** (wall throughput) | Kaggle v20 + LB submit | **0.20** | 2/10 | **yes** | flat vs kept; failed_edit=0.50; 1× session timeout; zip `c3136b1b…` |
| P3.9 | same stack on **`dev`** (n=35) | Kaggle v21 | **0.1714** | 6/35 | **baseline** | failed_edit=0.75; ~3 session timeouts; validates smoke≈dev |
| P3.10 | soft edit: tool-call shape + short `old_string` values (no hard caps) | Kaggle v22 | **0.10** | 1/10 | **no** | −0.10 vs P3.8; failed_edit=0.99; bad_arg=0.95; **revert** |
| P3.8b | `max_time_minutes` **4 → 3.5** | Kaggle v23 | **0.10** | 1/10 | **no** | −0.10; 3× session timeout; **revert wall to 4** |
| P3.11 | smoke **reshuffle** seed=42 (8/10 new IDs) | Kaggle v24 | **0.40** | 4/10 | **yes (stability)** | +0.20 vs orig smoke; **all 4 passes on new IDs**; overlap `rich_3480`/`rich_3930` fail; failed_edit=0.45; bad_arg=0 |
| P3.12 | **force** edit-by-12 (prompt/skill; no schema) | RunPod A6000 | **0.00** | 0/10 | **no** | −0.20 vs P3.8; failed_edit=0.83; bad_arg=0.62; no_edit 4/10 still rg-past-12; **revert** |

### Per-task highlights (kept vs rejected)

**P3.2 kept (0.20):** `rich_3882`✓, `rich_3905`✓; several non-empty patches still unresolved; one 5‑min timeout.

**P3.1 community (0.00):** 5/10 non-empty patches; failures mix: turn cap, 28-call cap, 8‑min timeout, “patched but tests failed” — not a harness `ModuleNotFound` (that was the discarded authlib run).

**P3.5 grep-first (0.10):** lost `rich_3882` vs P3.2; still solved `rich_3905`.

**P3.11 reshuffle (0.40):** passes `fastapi_14794`, `fastapi_14786`, `fastapi_14372`, `rich_3006` (all new vs orig smoke); overlap fails `rich_3480`, `rich_3930`.

**P3.12 force edit-by-12 (0.00):** 0/10; lost prior solves `rich_3882`/`rich_3905` (non-empty patches, unresolved); no_edit 4/10 still rg-past-12; failed_edit=0.83; bad_arg=0.62 → **revert**.

---

## 4. Budgets & 12h host clock

| Config | timeout_s | max_tool_calls | max_time_min | max_turns | Smoke Pass@1 | Extrap h/120 |
| --- | --- | --- | --- | --- | --- | --- |
| community_budget_fit | 240 | 28 | 8 | 80 | 0.00 | **5.83** (mean wall) |
| baseline (pre-P3.8) | 300 | 60 | 5 | 80 | 0.10–0.20 | not remeasured on Kaggle wall |
| **P3.8 wall-4min (shipped)** | 300 | 60 | **4** | 80 | **0.20** | keep; LB still PENDING; extrap TBD from mean wall |

**Host implication:** `5 min × 120 ≈ 10h` agent time alone + load/overhead → easy to approach/exceed **12h** official scoring. First public submit (P2.7, 2026-10-06) still `PENDING` after ~12–15h — likely wall overrun and/or scorer backlog; treat long PENDING as a **throughput** signal, not only model quality.

**Selection rule (P3.1):** max resolve subject to `extrapolated_hours_120 ≤ 10` → community rejected despite OK extrap.

Artifact: `gemma4-agent/results/budget_pareto.json`

### Remember next daily submit — 12h wall checklist (P3.8+)

**Do not assume `max_time_minutes: 4` alone prevents another 12–15h scoring run.**

Rough extrap for **120** official tasks (agent budget + per-task overhead):

| `max_time_minutes` | Agent alone | +~60s overhead/task | +~90s overhead/task | Margin vs 12h |
| --- | --- | --- | --- | --- |
| **5** (P2.7) | 10.0 h | ~12.0 h | ~13.0 h | **none / over** |
| **4** (P3.8 shipped) | 8.0 h | ~10.0 h | ~11.0 h | ~1–2 h — **helps, not guaranteed** |
| **3.5** | 7.0 h | ~9.0 h | ~10.0 h | safer |
| **3** | 6.0 h | ~8.0 h | ~9.0 h | comfortable |

**What 4 min does *not* change (still in shipped zip unless ablated):**
- `timeout_seconds: 300` (cmd timeout still 5 min; clipped by remaining session time)
- `max_tool_calls: 60` (can still burn the full wall exploring)
- Sandbox / pytest / host overhead outside agent clock
- Scorer queue / hang (PENDING ≠ always “still generating patches”)

**If P3.8 LB is 0 / partial / still PENDING past ~12h → next submit (one G3 variable):**
1. Prefer cut wall further: **`max_time_minutes: 3` or `3.5`** (keep calls=60 first), **or**
2. Mild call cut: **`max_tool_calls: 40–45`** (keep wall=4) — **not** community 28/8 (killed Pass@1).
3. Before shipping: smoke mean wall × 120 → must land **`extrapolated_hours_120 ≤ 10`**.
4. Log ticket as `P3.8b` (or next wall A/B); do not combine wall + calls + prompts in one zip.

**Decision rule:** if smoke Pass@1 holds at 4 min but LB times out → **tighten wall again**, do not revert to 5.

---

## 5. Localization diagnostic (P3.5)

Script: `gemma4-agent/scripts/measure_loc_hitk.py`  
Artifacts: `results/p3_metrics_batch/loc_hitk_{smoke,dev}.json` (local + S3; Spot re-confirmed 2026-10-07)

| Split | n | Lexical Hit@5 | Graph→file Hit@5 | Threshold | Auto-recommend |
| --- | --- | --- | --- | --- | --- |
| smoke | 10 | **0.50** | **0.40** | 0.60 | grep_first |
| dev | 35 | **0.4286** | **0.2857** | 0.60 | grep_first |

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

\*P3.5 traces include rejected nav + P3.2/P3.3 skills — pessimistic proxy for edit quality.

### Re-measure after Spot CPU metrics (2026-10-07)

Artifacts: `gemma4-agent/results/p3_metrics_batch/trace_kpis_all.json` (+ S3 same prefix).

| Trace set | Stack | failed_edit | loop_rate | bad_arg_rate | edits |
| --- | --- | --- | --- | --- | --- |
| **p37** (kept) | P3.2+P3.3+graph-first+temp 0.0 | **0.6452** | 0.5161 | 0.50 | 62 |
| p35 (rejected) | + grep-first | 0.9709 | 0.4542 | 0.9709 | 103 |
| p32b (rejected) | + host-arg / max-5 caps | 0.9114 | 0.5507 | 0.9114 | 79 |

Kept stack still fails GATE_P3 edit thresholds (&lt;10% / ≤½ P2). Prefer **p38/p39** over P3.5 proxy for paper claims.

**Autopsy (2026-10-08):** `results/p39_autopsy.json` — on `dev`, **20/35 never call `edit_file`**; among edits, **bad_arg≈0.71** often = source text used as JSON **keys** instead of `old_string` values. Soft fix = P3.10 (no hard thrash caps). GATE refresh: `results/gate_p3_refresh.json` still **FAIL**.

**Loc Hit@5 (offline, Spot+local):** smoke lex **0.50** / graph **0.40** (n=10); **dev** lex **0.4286** / graph **0.2857** (n=35). Both &lt;60% → diagnostic still says grep-first, but Pass@1 A/B already rejected that (keep graph-first).

**Inventory:** 129 snapshots / 127 graphs / 127 embeds / zero_byte=0 / `integrity_ok`.

**Zip dry-run:** 8 files, unpacked **5594** B, sha256 `8829a00049986f728588b0dea66956005df7934b06d0f8f16d4c19d89efb573b`.

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

| Field | P2.7 | P3.8 | Probe (kept wall4) |
| --- | --- | --- | --- |
| Submitted | 2026-10-06 22:21 UTC | 2026-10-07 11:27 UTC | 2026-10-08 05:01 UTC |
| Ref | `56892002` | `56909388` | `56935643` |
| Zip SHA256 | `b92db00d…` | `c3136b1b…` | `9a6f2283…` |
| Message | GATE_P2 smoke 0.20 v12 | wall-4min kept stack | infra probe kept wall4 |
| Status | **ERROR** | **ERROR** | **submitted via watchdog; verify final Kaggle ref/status in UI** |
| Daily limit | 1/day — do not resubmit until probe resolves | | |

**Local / cloud eval (not LB):** smoke kept **0.20**; reshuffle **0.40** (P3.11); `dev` **0.1714** (P3.9). Edit/loop GATE_P3 still FAIL.

**Next:** fix subprocess harvest deps, then re-run broadened harvest/STaR under a clean oracle path. No DoRA until keepers and task coverage grow materially.

### Scaffold ceiling → train thesis

Prompt/skill levers for edit hygiene and force-edit-by-12 repeatedly **lowered** Pass@1 while GATE_P3 edit/loop thresholds stayed unreachable. LB only scores resolve rate. Therefore freeze the best single-agent scaffold and attack no_edit / bad_arg modes via **self** BoN+STaR → DoRA (r≤64), not more system.md lectures.

### BoN harvest (negative)

| Run | Pool | Kept | Patched | Wall |
| --- | --- | --- | --- | --- |
| P5.1a | smoke×N=4 @0.2 | 0/40 | 20/40 | ~1.85 h A6000 |
| P5.1a-r2 | 8 known-solvable×N=4 @0.2 | **0/32** | **32/32** | ~1.1 h A40 CUDA13.2 |

Even tasks that pass at temp 0.0 did not reliably resolve under BoN temp 0.2 before host fixes. **P5.1b STaR** (8 tasks×1 retry, error-tail 20): **0 recoveries**. After fixing retry injection, persist ordering, and patch metadata, **P5.1b-star-r1** (9 r6 near-misses) produced **1/9 recoveries** and **1 clean non-duplicate SFT row**. However, `5/8` unresolved finals were blocked by missing subprocess deps (`inline_snapshot`, `dirty_equals`) plus `1` follow-on import error, so the current harvest miss rate is still not clean enough to scale.

---

## 9. Kept agent stack (working tree)

Paths under `gemma4-agent/agent/`:

- `eval_config.yaml` — 300 / 60 / **4** / 80 (P3.8 wall A/B)
- `configs/sampling.yaml` — temp **0.0**, max_out 2048, thoughts false
- `skills/patch-rules` — `/tmp` repro + exact `old_str` (P3.2; P3.2b thrash/host-arg caps reverted)
- `skills/debug-workflow` — repro before edit (P3.2)
- `skills/anti-loop` — edit-by-12 + budget discipline (P3.3; P3.2b thrash + **P3.12 force-edit** reverted)
- `skills/nav-rules` — **graph-first** (P3.5 grep-first reverted)
- `prompts/system.md` — G11 + soft edit-by-12 (no max-5 / host-arg / force-stop-rg lectures)

Zip boundary: **`agent/` only** (G5).

---

## 10. Negative results worth keeping

1. Community budget-fit (8/28/240) is **not** a free lunch on this host/model — 0/10 smoke.
2. Graph Hit@5 &lt;60% **does not** justify grep-first without a Pass@1 A/B.
3. Authlib-missing RunPod run → fake 0.0 in ~5s/task — always check wall time / error diversity before citing.
4. Laptop→SSH model copy wastes GPU $; direct S3 (presign/sync) required.
5. Smoke n=10 is **noisy** (P2.6 0.20 vs baseline re-run 0.10 same budgets).
6. Hardcoding host `old_string`/`new_string` + max-5 edit caps (P3.2b) **worsened** tool-arg hygiene — model often omitted `old_string` or put span text as a key; failed_edit 0.65→0.91; Pass@1 0.20→0.10.
7. Soft “tool-call shape / short old_string” lectures (P3.10) also **hurt** — Pass@1 0.20→0.10; bad_arg 0.25→0.95. Do not keep expanding edit-arg instructions; try other levers (wall, earlier submit, training after GATE).
8. Harder “force edit-by-12 / stop rg after #12” wording (P3.12) **hurt** Pass@1 0.20→0.00 on RunPod smoke; no_edit fell  but failed_edit/bad_arg rose (0.83 / 0.62). Premature edits ≠ solving.
9. Self BoN @ temp 0.2 → **yield 0** on smoke (P5.1a) and on known-solvable IDs (P5.1a-r2, 32/32 patched). Do not scale N; use STaR / revisit harvest temp.

---

## 11. Methods snapshot (draft bullets)

**Setup:** Fixed `gemma-4-31b-it-qat-w4a16-ct` on Kaggle ADK (4×L4, 32k). Single-agent zip (`agent/` only). Budgets measured via `eval_config.yaml`. Splits: smoke n=10, `dev` n=35, holdout untouched (G4).

**Procedure:** One-variable ablations (G3). Keep if smoke Pass@1 ≥ baseline (noisy n=10) and not worse on edit KPIs. Confirm stability on `dev` when possible.

**Kept configuration:** compaction `token_threshold=14336`, `max_out=2048`, thoughts off, temp **0.0**, graph-first nav, `/tmp` repro + exact edit hygiene, edit-by-12 + budget discipline, **`max_time_minutes=4`**.

**Primary metrics:** Pass@1; failed_edit / bad_arg / loop rates from traces; offline loc Hit@5; wall extrap for 120 tasks.

## 12. `no_edit` deep dive (P3.9 `dev`, Spot+local 2026-10-08)

Artifact: `results/p39_no_edit_dive.json` (also S3 `results/p3_metrics_batch/`).

| Finding | Value |
| --- | --- |
| Unresolved with **zero** `edit_file` | **21/35 (60%)** |
| Of those, still exploring past tool-call **#12** | **21/21 (100%)** |
| Ranked cause | **`explore_past_12_rg_heavy` 18**; read-heavy 2; mixed 1 |
| Mean calls (no_edit) | **56.8** |
| Mean `rg`/search shell calls | **20.9** |
| Mean **graph** tool calls | **0.0** |

**Implication:** Dominant miss is **localization thrash in shell search past edit-by-12**, not graph-tool addiction and not (only) edit-arg schema. **P3.12 tested force edit-by-12 → Pass@1 0.0** (reverted). Forcing earlier edits without better localization quality is not free.

## 13. Peak tokens / refuse (logged)

Artifacts: `results/p39_peak_tokens.json`, `p38_peak_tokens.json`.

| Split | refuse_rate | note |
| --- | --- | --- |
| p39 `dev` | **0.0** | no ContextWindow/refuse strings in traces |
| p38 smoke | **0.0** | same |

`final_metrics.total_prompt_tokens` is **cumulative** (hundreds of k) — not a single-step peak; do not treat as &gt;32k cliff. Compaction path appears to hold (refuse=0).

## 14. Gaps before a real paper

- [ ] Public LB score for ≥1 kept zip after submit infra stabilized (prior two **ERROR**; max-out submit needs final Kaggle status check)
- [x] Pass@1 on **`dev` (35)** for kept+wall-4 — **0.1714** (P3.9)
- [x] Failed_edit / loop rates on kept traces — see §6 + autopsy
- [ ] Replicate community vs baseline on same provider
- [x] Peak-token / refuse rates logged (refuse=0; cumulative totals only)
- [ ] Optional: training chapter (DoRA/GRPO) — only after GATE_TRAIN path
- [x] Error bars / second smoke — **P3.11 reshuffle Pass@1=0.40** (4/10; all new-ID passes)

---

## 15. Artifact index

| Artifact | Path |
| --- | --- |
| Experiment log | `docs/experiment_log.md` |
| GATE_P3 | `gemma4-agent/results/gate_p3_review.json` |
| Budget Pareto | `gemma4-agent/results/budget_pareto.json` |
| Loc Hit@k (earlier) | `gemma4-agent/results/loc_hitk.json` |
| Loc Hit@k smoke/dev | `results/p3_metrics_batch/loc_hitk_{smoke,dev}.json` (+ S3) |
| Trace KPIs (all) | `results/p3_metrics_batch/trace_kpis_all.json` |
| Classified traces | `results/p3_metrics_batch/p{32b,35,37}_traces_classified.json` |
| Inventory / zip dry-run | `results/p3_metrics_batch/local_inventory.json`, `submission_dryrun.zip` |
| Community smoke | `gemma4-agent/results/p31_community_budget_fit_smoke.json` |
| Baseline Kaggle | `gemma4-agent/results/p31_baseline_kaggle_smoke.json` |
| P3.3 / P3.2 / P3.5 / P3.7 / P3.2b | `results/p33_smoke.json`, `p32_smoke.json`, `p35_smoke.json`, `p37_smoke.json`, `p32b_smoke.json` |
| Traces | `results/smoke_traces/`, `p35_traces/`, `p37_traces/`, `p32b_traces/` (+ S3 `results/trace_exports/`) |
| Loc Hit@k script | `gemma4-agent/scripts/measure_loc_hitk.py` |
| Spot batch scripts | `scripts/spot_p3_metrics_batch.sh`, `spot_p3_optional_batch.sh`, `compute_trace_kpis.py` |
| Smoke kernel | `moranzavdi/gemma4-p26-smoke-swegemma` (latest ablation tag in notebook) |
| Agent dataset | `moranzavdi/gemma4-p26-agent-smoke` |
| S3 metrics prefix | `s3://gemma4-dev-agent-data-650825122703/results/p3_metrics_batch/` |
| no_edit dive (p39) | `gemma4-agent/results/p39_no_edit_dive.json` |
| Peak/refuse (p38/p39) | `gemma4-agent/results/p{38,39}_peak_tokens.json` |
| Autopsy / GATE refresh | `gemma4-agent/results/p39_autopsy.json`, `gate_p3_refresh.json` |
| Smoke reshuffle split | `gemma4-agent/configs/splits_smoke_reshuffle.json` |
| P3.11 smoke | `gemma4-agent/results/p311_smoke.json`, `p311_traces/`, `p311_trace_kpis.json` |
| P3.12 smoke (reverted) | `gemma4-agent/results/p312_smoke.json`, `p312_traces/`, `p312_trace_kpis.json`, `p312_no_edit_dive.json` |

---

## 16. Changelog for this file

| Date | Note |
| --- | --- |
| 2026-10-07 | Initial dump through GATE_P3 FAIL + P3.7 kickoff |
| 2026-10-07 | P3.7 keep temp 0.0; skip 0.7; P3.2b edit-arg/thrash caps on Kaggle v19; stand board sync |
| 2026-10-07 | P3.2b COMPLETE: Pass@1=0.10, failed_edit=0.91 → **revert**; restore P3.2/P3.3/temp0.0/graph-first |
| 2026-10-07 | Spot CPU P3 metrics + optional batch; kept failed_edit=0.65 (p37); loc Hit@5 smoke/dev logged; Spot terminated; local+S3 synced |
| 2026-10-07 | **P3.8** wall 5→4 min: smoke kernel v20 RUNNING; daily LB submit `56909388` PENDING |
| 2026-10-07 | Documented §4 wall checklist: 4 min helps but not guaranteed; next timeout → 3–3.5 min A/B |
| 2026-10-07 | P3.8 smoke COMPLETE: Pass@1=0.20 **keep**; failed_edit=0.50; await LB |
| 2026-10-07 | P3.9 **dev** COMPLETE: Pass@1=0.1714 (6/35); P2.7 LB → **ERROR**; P3.8 still PENDING |
| 2026-10-08 | Autopsy p39/p38 + GATE_P3 refresh FAIL; P3.10 soft-edit smoke v22 QUEUED (no RunPod key; no gemma4 Spot) |
| 2026-10-08 | P3.10 COMPLETE: Pass@1=0.10, bad_arg=0.95 → **revert** to P3.8 prompts/skills |
| 2026-10-08 | Submit probe `56935643` PENDING; no_edit dive (60% rg-past-12); refuse=0; P3.11 reshuffle v24 QUEUED |
| 2026-10-08 | P3.11 COMPLETE: Pass@1=**0.40** (4/10 new-ID solves); failed_edit=0.45; LB probe still PENDING |
| 2026-10-08 | P3.12 COMPLETE: force edit-by-12 → Pass@1=**0.00** on RunPod A6000 → **revert**; pod terminated |
| 2026-10-08 | Scaffold FREEZE + GATE_P3 FAIL+PIVOT + GATE_P4 single; P5.0 done; plan `docs/scaffold-freeze-train-plan.md` |
| 2026-10-08 | **P5.1a** BoN smoke×N=4 @0.2 → yield **0.0**; pod terminated; next P5.1a-r2 known-solvable + S3 persist → STaR |
| 2026-10-08 | **P5.1a-r2** known-solvable×N=4 → yield **0.0** but 32/32 patched; traces S3+local; next **P5.1b STaR** |
| 2026-10-08 | **P5.1b** STaR recoveries=**0**/8; pivot harvest (no DoRA yet) |
| 2026-10-09 | **P5.1b-star-r1** fixed STaR retry injection/persist path → recoveries=**1/9**; 1 clean non-duplicate row; 5 misses blocked by missing subprocess deps |
| 2026-10-11 | **P3.15a** 12B Pass@1=**0.10** FAIL → keep 31B; **P5.1a-r7b** → **6 keepers** (`rich_2943`) → SFT **67/11**; **P5.1a-r7c** 0 keepers / 27 metas → GPU off |

*Append new measured rows here when ablations finish; do not invent numbers.*

## P5 harvest / LB (2026-10-09)

- LB public **0.06** (infra recovered); leaders ~0.17–0.24. Gap vs smoke still leaves a wall/timeout hypothesis live.
- BoN resolve false-negatives mostly fixed: RunPod must run `ensure_subprocess_host_deps` + gold oracle before harvest (`gemma4-agent/docs/harvest_runpod.md`).
- **P5.1a-r5c** yield=0.9375 (60/64). **P5.1a-r6** broadened yield≈0.13 and lifted `data_train/sft.jsonl` to **65 rows / 9 tasks**.
- **P5.1b-star-r1** finished in `3287.96s`: `recoveries=1/9`, `new_rows=1`, `exact_dup_vs_sft=0`.
- Host extras pin/preflight hardened 2026-10-11; SFT **66/10**.
- **P3.15a** 12B exact-0.18 smoke → Pass@1=**0.10** FAIL vs gate 0.14 → keep default **31B**.
- **P5.1a-r7** contaminated working-tree override → 0 keepers (grep loop on `fastapi_11194`) → aborted.
- **P5.1a-r7b** freeze-agent BoN (partial, operator stop): **6 keepers** on `rich_2943` (6/8 resolved; **2 unique patches**); `rich_3061` 0/7; 14 tasks remaining. Artifacts: `gemma4-agent/results/p51a_r7b/` + S3 same prefix. Lesson: **never** set `AGENT_OVERRIDE_DIR` to working-tree `agent/`.

### Next steps (ordered)

1. ~~CPU filter/render/merge r7b~~ **done:** SFT **67/11** (+`rich_2943`).
2. ~~P5.1a-r7c~~ **stopped partial:** keepers=**0**, metas=27 (`rich_3105/3468/3518` 0/8; `rich_3772` 0/3). Artifacts: `gemma4-agent/results/p51a_r7c/`.
3. **Done:** **P5.1a-r7d** freeze BoN on 10 tasks (non-rich first) finished with oracle PASS, `0/80` keepers, and mixed failure classes (`test_fail`, `timeout`, `test_patch_apply_fail`, `no_submit_patch`). Artifacts: `gemma4-agent/results/p51a_r7d/` and `gemma4-agent/results/p51a_r7d_filtered/`.
4. Next: CPU autopsy on `r7d` fail classes, then decide targeted rerun vs pool refresh. Freeze agent only; no edit-prompt A/Bs.
5. If a hypothetical `P3.15b` public probe is still taken, treat the score read as a strict 12B keep/kill gate only. The serious path toward **0.25** remains: `r7d` autopsy -> repaired **31B** BoN/STaR -> larger SFT set -> DoRA A/B -> next scoring submit.
