# Distillation policy

**Refreshed:** 2026-10-08 (P5.0 — scaffold-freeze train path)

**default:** BLOCK `proprietary_ide` and closed API teacher trajectories

**allow:** `self`, `synth`, `open_teacher` (Apache-2.0 / explicitly permitted)

**review:** Re-check Kaggle Discussions before flipping `proprietary_ide` to ALLOW

**ledger:** Every train row must have `source=` and `license=`

| source | status |
| --- | --- |
| self | ALLOW (BoN / STaR on frozen agent) |
| synth | ALLOW |
| open_teacher | ALLOW if license clear |
| proprietary_ide | BLOCK |

**P5 path note:** First harvest is `source=self` only under frozen P3.8 scaffold. Do not mix blocked sources into `data_train/sft.jsonl`.
