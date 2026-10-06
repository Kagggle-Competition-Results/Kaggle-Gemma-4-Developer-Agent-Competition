# Sandbox harness backups (NOT the EC2 AMI)

| File | Role |
| --- | --- |
| `Dockerfile.gemma4` | Full image recipe with `extras.txt` packages baked in |
| `extras.txt` | Pip packages missing from competition `/wheels` |
| `pin_deps.py` | Per-task constraint-aware install (Starlette pins, etc.) |

**AMI** `ami-0794230e150561868` = host only (Docker daemon, AWS CLI). Re-baking AMI is optional later; day-to-day fixes ship here + `scripts/soundness_check.py`.

See `docs/harness_verify.md`.
