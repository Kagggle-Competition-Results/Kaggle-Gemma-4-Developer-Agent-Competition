# P3.14 Prompt Ablation Plan From Exact 0.18

## Objective

Preserve the exact public `0.18` branch and spend future public slots only on single-variable prompt ablations against that exact baseline.

Baseline artifacts:

- `gemma4-agent/results/competitor_verra_0.18/submission.zip`
- `gemma4-agent/results/competitor_verra_0.18/gemma-4-agent-budget-fit-single-agent.ipynb`

Failed comparison point:

- `gemma4-agent/results/p313_budgetfit_plus_proven/submission.zip`
- public `0.13`

## What Actually Changed

Shipped zip diff between public `0.18` and `P3.13`:

- `agent.yaml`: no diff
- `configs/sampling.yaml`: no diff
- `eval_config.yaml`: no diff
- `prompts/system.md`: all meaningful shipped changes

So prompt changes alone explain the public regression until proven otherwise.

## Prompt Delta Buckets

### A. Hard context rules

Added in `P3.13`:

- shell output caps
- `read_file` cap guidance
- "do not ask for large blob again"
- extra "go to explicit filename/function/class first"

Read:

- Probably low risk in isolation
- But can reduce flexibility and cause premature narrowing if phrased too aggressively

### B. Repro-before-edit requirement

Added in `P3.13`:

- write focused repro to `/tmp/repro.py`
- run repro before editing
- cleanup scratch file before finishing

Read:

- Highest-risk delta
- This directly encourages scratch-file creation and extra steps before editing
- It is the opposite of the behavior that later caused loop contamination in harvest

### C. Edit hygiene / exact-span behavior

Added in `P3.13`:

- read exact span before edit
- prefer exact `old_str`
- keep edits small
- reread edited range

Read:

- Conceptually good for correctness
- But may cost decisiveness or add extra turns
- Better candidate than repro rules if we want a "safe" ablation

### D. Softer / shorter budget language

Changed in `P3.13`:

- removed `(under 8-10 turns)` wording
- weakened "make best guess now" urgency
- removed "do not start new exploration after your 20th tool call"
- simplified anti-pattern bullets

Read:

- Medium risk
- Could reduce urgency and let the agent over-explore
- Likely interacts badly with the added repro/edit-hygiene overhead

## Ranked Ablation Order

### P3.14a — Exact 0.18 Repro Submit

Change from standing branch:

- none

Purpose:

- verify we can faithfully reproduce the public `0.18` line from local artifacts before any new ablation

Priority:

- highest

Prepared local artifact:

- dir: `gemma4-agent/results/p314a_exact_018_repro/agent`
- zip: `gemma4-agent/results/p314a_exact_018_repro/submission.zip`
- zip sha256: `6f21a0193c6d56bfc9f66d0fb57224258ce9e21d3236ecff02ae158f24928d05`
- integrity: all four shipped files match local `competitor_verra_0.18` contents exactly

### P3.14b — 0.18 + Hard Context Rules Only

Change from standing branch:

- add only the hard context rules block
- keep original urgency, no repro requirement, no edit-hygiene expansion

Purpose:

- test whether read/output caps help without slowing the solve loop

Priority:

- second

Prepared local artifact:

- dir: `gemma4-agent/results/p314b_018_plus_hard_context_only/agent`
- zip: `gemma4-agent/results/p314b_018_plus_hard_context_only/submission.zip`
- zip sha256: `efb510e53979b768d228cdf37a544715ac41a5c94a51c05b3378a7fc6122723b`
- exact delta from `P3.14a`: only the six-line `Hard Context Rules` block added near the top of `prompts/system.md`

### P3.14c — 0.18 + Edit Hygiene Only

Change from standing branch:

- add only exact-span / exact-old-string / reread-after-edit guidance
- keep original urgency
- no repro file rule

Purpose:

- test whether tighter edit discipline helps without adding scratch-path overhead

Priority:

- third

Prepared local artifact:

- dir: `gemma4-agent/results/p314c_018_plus_edit_hygiene_only/agent`
- zip: `gemma4-agent/results/p314c_018_plus_edit_hygiene_only/submission.zip`
- zip sha256: `af7ae2e8ea4bf242e1437b34bafff6d3990d7f020829934fe44aba0c2befbcca`
- exact delta from `P3.14a`: only four edit-hygiene lines added under `### 2. Implement the Solution Directly`

### P3.14d — 0.18 + Repro-Before-Edit Only

Change from standing branch:

- add only `/tmp/repro.py` reproduce-before-edit rule

Purpose:

- isolate the likely worst offender directly

Priority:

- fourth, and only if we deliberately want to prove that this rule is the regression source

Prepared local artifact:

- dir: `gemma4-agent/results/p314d_018_plus_repro_before_edit_only/agent`
- zip: `gemma4-agent/results/p314d_018_plus_repro_before_edit_only/submission.zip`
- zip sha256: `f137a1fda0c66bab607b39156087d80ddee26aed155c6d12d458a245c815a26e`
- exact delta from `P3.14a`: only the repro-before-edit block, section renumbering, and scratch cleanup line

## What Not To Do

- Do not submit another blended hybrid of multiple prompt deltas.
- Do not compare against `0.06`.
- Do not mix notebook baseline changes with sampling or agent-yaml changes in the same public arm.
- Do not branch new public arms from `P3.13`.

## Working Thesis

Current best thesis is:

1. exact public `0.18` is the real winning branch
2. `P3.13` regressed because it replaced the notebook prompt's urgency with a more careful, multi-step workflow
3. the repro-before-edit rule is the most suspicious delta
4. hard context caps and exact-edit hygiene should only be tested one at a time from exact `0.18`
