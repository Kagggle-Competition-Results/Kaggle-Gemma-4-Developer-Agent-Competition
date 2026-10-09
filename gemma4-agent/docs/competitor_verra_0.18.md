# Competitor audit: verracodeguacas 0.18 LB

**Kernel:** https://www.kaggle.com/code/verracodeguacas/gemma-4-agent-budget-fit-single-agent  
**Public score:** 0.18 (V5) · Runtime 25s (zip builder only)  
**Local copy:** `gemma4-agent/results/competitor_verra_0.18/`

## What the notebook is

Not a train/eval run. It **embeds a full `submission.zip`** (4 files) and writes it on Run All. Scoring is the competition host running that zip.

## Config vs our freeze (LB 0.06)

| Lever | Verra 0.18 | Ours (freeze / scored 0.06) |
| --- | --- | --- |
| `max_time_minutes` | **8** | **4** |
| `max_tool_calls` | **28** | **60** |
| `timeout_seconds` | **240** | **300** |
| `temperature` | **0.2** | **0.0** |
| `max_output_tokens` | **8192** | **2048** |
| `thinking_budget` / thoughts | **4096 / on** | **1024 / off** |
| Skills | **none** | 4 skills (debug/patch/anti-loop/nav) |
| Graph tools | listed but **prompt says do not use** | graph-first nav skill |
| Prompt | long official-starter + budget narrative | short G11 + soft edit-by-12 |
| Edit-by-12 | soft in prompt | soft in prompt + skill |

Their stated thesis: many fixes need **>4 min** of scorer-speed time; **28-call hard cap** stops hopeless explore and “pays for” 8 min within the 12h / ~120-task envelope. They also claim **`timeout_seconds` is the verification pytest timeout** (requests suites ~100s) — worth verifying against host docs.

## Conflict with our measurements

We already ran essentially this **budget package** as P3.1 community (`8 min / 28 calls / timeout 240`) → **smoke Pass@1 = 0.00** (paper-notes). So:

- LB 0.18 ≠ proof that 28/8 is free on our stack.
- Differences that may reconcile: **max_out 8192 + thinking on + temp 0.2 + their prompt (no skills, anti-graph)** vs our P3.1 setup (likely tighter sampling / different prompt).
- Public LB ~58 tasks with ±~2-task noise (their note).

## Likely reasons they beat 0.06 (ranked)

1. **Throughput package (8 min × 28 calls)** finishes more of the 12h queue with enough wall on hard tasks — our wall=4 may leave solveable tasks unfinished; our 60 calls may thrash.
2. **Much larger decode budget (8192 + thoughts)** — our `max_out=2048` may truncate tool plans on hard tasks (high confidence this is under-tested on LB).
3. **Prompt: discourage graph + explicit 28/8 budget + ignore broken tests** — different policy than our skills stack.
4. **Not** adapters / training (zip has no adapters).

## Recommended A/Bs (one G3 each; after pending submit clears)

| Priority | Ablation | Why |
| --- | --- | --- |
| **1** | `max_output_tokens` 2048→8192 (+ optional thinking on) keep wall4/60 | Isolates decode-cap hypothesis; low risk vs full 28/8 |
| 2 | Full Verra budget package **only if** (1) helps or smoke re-check | Re-test 8/28/240 with *their* sampling+prompt on smoke first |
| 3 | Do **not** drop skills blindly without smoke | Our skills are freeze; change one block at a time |

## Do not

- Blind-submit their zip as “ours” without smoke (P3.1 already failed community budgets on smoke).
- Combine wall + calls + max_out + prompt in one zip (G3).

## Execution (2026-10-09)

- **P3.8d** zip built: `results/submission_p38d_maxout8192.zip` (max_out **8192**, wall4/60/temp0 kept). Submit/watch is the current Kaggle follow-up; verify final ref/status in UI before another zip.
- **Do not** submit full Verra 28/8 until smoke re-check with their sampling.
- GPU harvest no longer targets r5b. Current DS blocker is subprocess env parity: `inline_snapshot` and `dirty_equals` are missing from the harvest wheel path, which polluted `P5.1b-star-r1` misses.
