---
name: patch-rules
description: Minimal exact edit_file patches with /tmp repro hygiene (G11).
---

# Patch rules (G11 + edit hygiene)

## Before any `edit_file`
- Write a focused repro to `/tmp/repro.py` and run it with `run_command`.
- `read_file` the exact span you will change (≤40 lines) immediately before editing.

## Edit hygiene
- Prefer `edit_file` with exact `old_str` copied from the last `read_file` / `sed` result.
- Prefer ≤ ~50–100 new tokens of change.
- Re-read the edited range after every successful edit; retry once on mismatch.
- Never invent escape sequences in `old_str` / `new_str`.
- **Never `read_file` an entire file if it is over 40 lines.** Use `rg` / `sed -n`.
- Do not use `write_file` to rewrite an existing file; only for new files.
- Cap tool output: `rg ... | head -30`, `sed -n`, `read_file` ≤40 lines.

## Before `submit_patch`
- Remove scratch files (`/tmp/repro.py` and any other temps).
- Never modify tests.
