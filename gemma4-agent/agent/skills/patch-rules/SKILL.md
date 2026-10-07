---
name: patch-rules
description: Minimal exact edit_file patches with host arg names and thrash caps.
---

# Patch rules (G11 + edit hygiene)

## Host `edit_file` contract (mandatory)
- Tool args must be exactly: `filepath`, `old_string`, `new_string` (not `old_str` / `new_str` / `path`).
- Copy `old_string` **verbatim** from the latest `read_file` / `sed` output. No paraphrasing.
- Do **not** add extra backslash escapes. Paste the file text as shown (indentation and quotes unchanged).
- Never omit any of the three args.

## Before any `edit_file`
- Write a focused repro to `/tmp/repro.py` and run it with `run_command`.
- `read_file` the exact span you will change (≤40 lines) immediately before editing.

## Edit thrash caps
- Prefer ≤ ~50–100 new tokens of change.
- On failure: re-`read_file` the span, rebuild `old_string` from that output, retry **at most once**.
- **At most 3 `edit_file` calls per filepath** per task. If still failing, stop editing that file; switch file or `submit_patch` best clean patch.
- **At most 5 `edit_file` calls total** per task. Then verify or submit — no more edits.
- After a successful edit (`status: ok`), re-run repro / targeted pytest; do not keep editing the same lines without new evidence.
- **Never `read_file` an entire file if it is over 40 lines.** Use `rg` / `sed -n`.
- Do not use `write_file` to rewrite an existing file; only for new files.
- Cap tool output: `rg ... | head -30`, `sed -n`, `read_file` ≤40 lines.

## Before `submit_patch`
- Remove scratch files (`/tmp/repro.py` and any other temps).
- Never modify tests.
