# P3.13 Regression vs Public 0.18

## Decision

- Standing public baseline is the exact public `0.18` notebook:
  - `gemma4-agent/results/competitor_verra_0.18/gemma-4-agent-budget-fit-single-agent.ipynb`
  - `gemma4-agent/results/competitor_verra_0.18/submission.zip`
- `P3.13` is a failed hybrid ablation:
  - public score `0.13`
  - Kaggle ref `57017054`
- Any next public submit should start from exact `0.18`, with one boring single-variable change only.

## Score Read

| Branch | Public LB | Read |
| --- | --- | --- |
| frozen scaffold | `0.06` | obsolete |
| public notebook `0.18` | `0.18` | standing baseline |
| `P3.13` hybrid | `0.13` | regression vs baseline |

`P3.13` improved over the old frozen `0.06` line, but that is no longer the relevant comparison. The relevant comparison is against the already-public `0.18` winner, and `P3.13` lost by `-0.05`.

## Exact Artifact Comparison

Compared shipped zip contents:

- `gemma4-agent/results/competitor_verra_0.18/submission.zip`
- `gemma4-agent/results/p313_budgetfit_plus_proven/submission.zip`

### Same across both zips

- `agent.yaml`
- `configs/sampling.yaml`
- `eval_config.yaml`

### Different across both zips

- `prompts/system.md`

So the public regression is not coming from sampling YAML or top-level agent wiring. The shipped delta is the prompt package.

## Prompt Delta Summary

Relative to the public `0.18` prompt, `P3.13` added or changed:

- hard context rules and read caps
- `/tmp/repro.py` reproduction requirement
- extra edit hygiene instructions
- cleanup requirement for scratch files
- softer wording in some speed/budget rules
- broader navigation constraints around graph tools

The hybrid prompt was more "careful", but the public score says those additions hurt this branch in aggregate.

## Campaign Rule Going Forward

1. Freeze exact `0.18` as the standing submit baseline.
2. Treat `P3.13` as a failed prompt-only ablation.
3. Do not compare future scaffold submits to `0.06`; compare to `0.18`.
4. Next public submit must be:
   - exact `0.18` repro, or
   - one single-variable ablation from exact `0.18`.

## Practical Implication

The next attribution task is not "invent a better hybrid". It is "identify which single prompt delta from `P3.13` cost the `-0.05` and avoid repeating it."
