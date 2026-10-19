---
name: anti-loop
description: Stop repeated failing tool calls and enforce budget discipline (edit-by-12).
---

# Anti-loop + budget discipline

## No-loop
- If the same tool name + args fail twice, change strategy (new query, new file, or stop).
- Do not re-read the same filepath with the same line range more than twice without a new hypothesis.
- Cap identical consecutive failing commands; prefer graph/`rg` pivot over dump-and-retry.
- If a search command fails because `rg` is missing, switch once to `grep -R -n ... | head -30` or `find ... | head -30`; do not retry `rg`.
- Bare or generic pytest commands are loops for this configuration. After one `pytest`, `pytest -v`, `pytest | head`, or discovery-only pytest call, the next move must be an exact test path, repo read, edit, or stop.
- Treat repeated shell setup commands as loops too. After one `export PYTHONPATH=...` or other env-only command, the next shell call must combine setup with a targeted read or targeted pytest path.
- Standalone `export PYTHONPATH=...` is already a failed pattern. After one such command, do not issue another `export`; move to repo reads, edits, or one exact test path from the traceback.
- If the same shell command or same shell prefix repeats twice (for example identical `export ...`, `pytest ...`, or `python -m pytest ...` setup), stop and issue one different targeted command or move on to the edit.
- Repeating the same direct verify family is a loop too. After one failed `python -c` or exact pytest verify, the next move must be `read_file`, `edit_file`, or a different exact pytest path.
- Multiline `python -c "..."` commands count as malformed direct verifies. Do not retry them; use one single-line verify or move to source reads.
- If `run_command` shows `error` with `exit 0`, count it as a failed verify and do not repeat the same command shape.
- Treat repeated Python introspection as loops too. After one probe like `python -c ... dir(...)`, `inspect.signature(...)`, `__defaults__`, `__annotations__`, `help(...)`, `pip list`, or package version print on the same symbol/module, the next move must be a `read_file`, repro, or edit.
- Do not vary the same introspection across `dir`, `__defaults__`, `__kwdefaults__`, and `__annotations__` for the same callable; that still counts as one loop family.
- Treat repeated zero-hit search as loops too. After two no-hit searches for the same token/symbol, stop searching and switch to likely source files inferred from the issue, then `read_file` or edit.
- Blank shell search output counts as a no-hit too. Repeating the same token search after one blank/no-hit result is already a loop.
- Treat repeated same-hit search as loops too. If the same grep/search returns the same relevant line twice, stop searching that token and move to `read_file`, `edit_file`, or an exact test path.
- Treat pipe-filter churn as loops too. Reusing the same base grep/search and only changing trailing `| grep ...` filters counts as one loop family.
- Using `search_similar_code` with free-text issue prose instead of an exact symbol/API name counts as a loop.
- After a real source hit, searching invented neighboring symbol names instead of reading or editing that hit counts as a loop.
- If the failure is `unexpected keyword argument`, editing a class attribute while never patching the accepting callable signature or `__init__` counts as a loop.
- Treat repeated reads of the same file as loops too. After two `read_file` calls on the same file, the next move must be `edit_file`, a direct verify command, or a different file.
- Do not repeat whole-tree search families like `grep -R ... .`, `find . -name '*.py' | xargs grep ...`, `find fastapi -name '*.py' | xargs grep ...`, or `ls -R` for the same token. That is one loop family even if the flags vary.
- Treat repeated host-package diagnosis as loops too. After one `pip list`, version print, `__file__`, `sys.path`, or host-package hypothesis, the next move must be a repo read, repro, edit, or targeted test.
- If a direct verify command already imports the package from the workspace checkout, any later `pip list`, version print, `__file__`, `sys.path`, or constructor-signature probe is a loop immediately.
- After two failed `edit_file` attempts on the same span, re-`read_file` the range and copy `old_str` exactly — do not retry the same broken args.
- A malformed `edit_file` call counts as a failure. After one missing-parameter error, re-read the target span and issue a complete `edit_file(filepath, old_string, new_string)` call; never repeat the incomplete shape.
- A bare `edit_file(filepath)` call is malformed. The next edit must include `old_string` copied exactly from the last read span.
- Editing a later file from stale context is a loop too. For the second or later edit on a task, the last source read before `edit_file` must be that same file and target span.
- For class or dataclass edits, patching without first reading the exact class block is a loop. After `grep -n` identifies the class line, the next read must be that class block before `edit_file`.
- Once the exact file for a symbol/class is already known, searching guessed alternate files or whole-tree rediscovery for that same symbol is a loop. Re-read or edit the known file, or verify.
- After you successfully edit a known symbol file and then patch downstream files, going back to rediscover that same symbol/file without a new failing verify or exact downstream evidence is a loop.
- Any `write_file` call counts as a loop for this configuration. Skip it and move to `read_file`, `edit_file`, or one direct verify command.
- Scratch-file creation through shell redirection or heredocs counts as a failure too. After one `cat <<EOF > repro.py`, `echo ... > repro.py`, or similar attempt, stop file creation and use direct repo reads plus an exact test path.
- If a task keeps trying to create scratch repro files or retrying malformed edits, stop and make one smallest plausible production edit instead of extending the loop.
- A quoted filepath in a tool call (for example `"fastapi/params.py"`) counts as malformed. Retry once with the raw repo-relative path only.
- If direct verifies already show the bug via a real behavior delta like `200` vs `422`, more localization is a loop. Edit the best-localized file.
- After a real behavior delta like `200` vs `422`, more than one semantic search plus one shell search is a loop. The next move must be `read_file` or `edit_file`.
- Empty `submit_patch` counts as a loop/failure too. If no production file changed, do not submit; keep localizing, edit, or stop.

## Budget discipline
- Call `get_status` about every 8 tool calls to check remaining time / calls / turns.
- **Edit source by call 12:** by tool-call #12 you must have attempted at least one `edit_file` on production source (not tests). If still only exploring at #12, stop searching and edit the best-localized candidate immediately.
- When less than 25% of any budget remains (time, tool calls, or turns): stop exploration; apply the smallest plausible fix or `submit_patch` with the best clean patch. Do not open new files unless required for that fix.
- If stuck after half the budget with no passing check, submit best clean patch or stop.
