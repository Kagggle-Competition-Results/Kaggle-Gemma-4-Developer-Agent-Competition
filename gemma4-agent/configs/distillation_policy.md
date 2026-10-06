# Distillation policy

**default:** BLOCK `proprietary_ide` and closed API teacher trajectories

**allow:** `self`, `synth`, `open_teacher` (Apache-2.0 / explicitly permitted)

**review:** Re-check Kaggle Discussions before flipping `proprietary_ide` to ALLOW

**ledger:** Every train row must have `source=` and `license=`

| source | status |
| --- | --- |
| self | ALLOW |
| synth | ALLOW |
| open_teacher | ALLOW if license clear |
| proprietary_ide | BLOCK |
