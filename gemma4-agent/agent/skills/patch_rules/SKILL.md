# Patch rules (G11 context eviction)

- Prefer `edit_file` with exact `old_str` copied from the last `read_file` result.
- Target ≤ ~50–100 new tokens of change when possible.
- Re-read the edited range after every successful edit; retry once on mismatch.
- Never invent escape sequences in `old_str` / `new_str`.
- **Never `read_file` an entire file if it is over 200 lines.** Use `run_command` (`rg`, `sed -n`) or ranged reads only.
- Do not use `write_file` to rewrite an existing file; only for new files.
- Remove scratch files before `submit_patch`.
