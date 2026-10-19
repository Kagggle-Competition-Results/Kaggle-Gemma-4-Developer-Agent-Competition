# P3.15 Model-size ablation (exact 0.18 scaffold)

## Decision

Host now allows multiple Gemma-4 QAT variants, but **no mixing within one submission**.

| Variant | Role |
| --- | --- |
| `gemma-4-31b-it-qat-w4a16-ct` | **Default ship + harvest** (protects public **0.18**) |
| `gemma-4-12b-it-qat-w4a16-ct` | First measured pivot candidate (TTC / throughput) |
| `gemma-4-e4b-it-qat-w4a16-ct` | Deferred until 12B shows non-collapse |
| `gemma-4-e2b-it-qat-w4a16-ct` | Deferred (specialist / multi-adapter Plan B only) |

Adapters do **not** transfer across variants. 31B BoN/STaR/SFT stays on 31B until a model pivot is logged.

## G3 rule

One variable: **`model:` only**. Scaffold = exact public `0.18` package (`agent.yaml` tools/prompt/sampling/eval unchanged except model string).

## Artifact (ready)

- Dir: `gemma4-agent/results/p315a_018_model_12b_only/`
- Zip: `submission.zip`
- sha256: `819e84974cc4df9e542e493facb7ad8459c4bc6b08f39f63e984d915c1c03dbd`
- Diff vs `competitor_verra_0.18/submission.zip`: `agent.yaml` model `31b` → `12b` only

## Run order

1. **P3.15a** — smoke n=10 on RunPod A40: exact 0.18 + **12B** (**RUNNING** pod `mi9cfg2np2y856`; interim 0/2 resolved — timeout/28-call).
2. Compare to known 31B smoke on same scaffold class (Verra/0.18 line or freeze control).
3. **Keep 12B path only if** smoke Pass@1 ≥ ~0.7× of 31B control **and** mean wall clearly better.
4. **If 15a FAIL gate** (expected if Pass@1 ≈ 0 under 28/8): **keep 31B**, launch **P5.1a-r7** clean BoN with hardened pin/preflight (queued).
5. **P3.15b** (optional, only if 15a holds): same 12B + **more TTC** (higher wall/calls — not community 28/8).
6. Do **not** open E4B/E2B or ParallelAgent until 15a/15b decide.

## Post-KPI decision (locked)

| 15a Pass@1 | Next |
| --- | --- |
| &lt; 0.14 (≪ 0.7× of ~0.20 31B class) | **FAIL pivot** → 31B default; P5.1a-r7 clean harvest on freed GPU |
| ≥ 0.14 and mean wall clearly better | consider **P3.15b** TTC arm before harvest scale |
| ≥ 0.20 and faster | log measured model pivot; re-home harvest to 12B later |

## KPIs to log

| KPI | Field |
| --- | --- |
| Pass@1 | `pass_at_1` |
| Resolved | `resolved/n` |
| Mean wall | `mean_s` + `extrap_h_120` |
| vs 31B | `delta_pass`, `speedup_mean_wall` |

## Must not

- Mix variants in one zip
- Combine model swap with prompt/budget changes
- Replace 31B harvest default before measured pivot
- Spend daily LB slot on 12B before smoke KPIs

## After `P3.15b` (if forced anyway)

If an operator still spends a public slot on `P3.15b`, treat the resulting score as a terminal keep/kill decision for the 12B branch. Unless that read clearly improves on the **31B** line, stop model-size probing and return to the locked path for real leaderboard upside:

1. CPU autopsy `P5.1a-r7d`.
2. Repair one failure class only.
3. Re-run clean **31B** BoN/STaR harvest.
4. Grow SFT data and run DoRA (`r<=64`) plus 4-bit/dev A/B.
5. Use the next scoring slot on the best trained **31B** finalist, not another model-size public probe.
