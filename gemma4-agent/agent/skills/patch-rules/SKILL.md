---
name: patch-rules
description: Minimal exact edit_file patches with no scratch-file repros (G11).
---

# Patch rules (G11 + edit hygiene)

## Before any `edit_file`
- Prefer a direct verify command, not a scratch file: use the exact failing pytest path from the traceback, or one short single-line `python -c` command. Do not run generic `pytest`, `pytest -v`, or discovery-only commands.
- Prefer one combined command over shell setup spam. If you need `PYTHONPATH`, include it inline on the same targeted command instead of separate `export ...` retries.
- Once a direct verify command imports the package from the workspace checkout, do not spend turns on `pip list`, version prints, `__file__`, `sys.path`, or constructor signature probes. Read the repo and patch it.
- If the failing error is `unexpected keyword argument`, patch the callable signature first: the function signature, method signature, or `__init__` that receives kwargs. For a normal Python class, adding a class attribute alone does not make constructor kwargs valid.
- When direct verifies show a real behavior delta like `200` vs `422`, stop exploring and edit the best-localized source file.
- Search production source first, not docs/tests, unless the traceback explicitly points there.
- `read_file` the exact span you will change (≤40 lines) immediately before editing.
- Do not create scratch repros at all.

## Edit hygiene
- `edit_file` requires `filepath`, `old_string`, and `new_string` every time.
- Prefer `edit_file` with exact `old_str` copied from the last `read_file` / `sed` result.
- Never call bare `edit_file(filepath)`. Before any patch, copy 2-8 exact source lines into `old_string`.
- For the second or later edit on a task, re-read the exact target span in that same file immediately before `edit_file`.
- For class or dataclass field edits, first locate the line with `grep -n` / `rg -n`, then read the exact class block and patch that block immediately.
- Never call `edit_file` with only `new_string` or an invented `old_string`.
- Prefer ≤ ~50–100 new tokens of change.
- Re-read the edited range after every successful edit; retry once on mismatch.
- After one successful edit, the next source step should be: re-read the edited span, run one exact verify, or read the exact span for the next file you will edit. Do not patch a later file from stale earlier reads.
- If the target edit is inside a class block, the last read before `edit_file` must be that exact class block, not imports or a distant helper in the same file.
- Once you know the exact file for a symbol/class, do not search guessed alternate files for that same symbol. Re-read or edit the known file, or run one exact verify.
- After you successfully edit a known symbol file and then patch downstream files, do not go back and rediscover that same symbol/file without a new failing verify or exact downstream evidence.
- Never invent escape sequences in `old_str` / `new_str`.
- **Never `read_file` an entire file if it is over 40 lines.** Use `rg` / `sed -n`; if `rg` is missing, use `grep -R -n ... | head -30`.
- Do not quote tool filepaths. Use raw repo-relative paths like `fastapi/params.py`.
- If a search result returns the same relevant hit twice, stop searching and read that code span directly.
- Do not keep the same base grep and only change the trailing pipe filter. That is one search family; after it, read or edit.
- Blank shell search output counts as a no-hit too. After one no-hit on a token, do not rerun the same token search family; read the best existing source hit or edit.
- After two `read_file` calls on the same file, the next move must be `edit_file`, exact verify, or a different file.
- After one failed direct verify command or one failed inline-`PYTHONPATH` run, stop environment setup and move to repo reads, edits, or the exact failing test path from the traceback.
- Multiline `python -c "..."` commands count as malformed direct verifies. Keep them to one shell line only.
- If `run_command` reports `error` even with `exit 0`, count that as a failed direct verify and switch to source reads, an exact pytest path, or an edit.
- Cap tool output: `rg ... | head -30`, `sed -n`, `read_file` ≤40 lines.

## Before `submit_patch`
- Ensure at least one production source file changed. Never submit an empty patch.
- Never modify tests.
