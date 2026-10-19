---
name: debug-workflow
description: Observe-plan-search-patch-verify loop with direct verification before edits.
---

# Observe → Plan → Search → Verify → Patch → Verify

Run one cycle, then re-plan on failure. Do not chain unbounded tool spam.

1. **Observe / plan** — expected vs actual from the issue.
2. **Search** — localize with `rg` / graph tools + ranged reads in production source first (never dump files >40 lines).
3. **Verify** — before editing, run the exact failing pytest path from the traceback, or one short single-line `python -c` command. Do not create a repro file. Do not run generic `pytest`, `pytest -v`, or discovery-only commands.
4. **Patch** — smallest `edit_file` with whitespace-exact `old_str` from the last read.
5. **Verify** — re-run the same direct command or exact pytest path; then `submit_patch`.

Prefer localization before edits. Prefer small patches. Verify before `submit_patch`.
Use at most one Python introspection shell probe per symbol/module before switching to `read_file`. Repeating `dir(...)`, signature dumps, or attribute prints is a loop, not progress.
If a literal token search returns no hits twice, stop widening the search. Use the issue context to pick likely modules and read them directly.
Blank shell search output counts as a no-hit too. After one repeated blank/no-hit search on the same token, switch to `read_file` or edit.
If the same grep/search returns the same useful hit twice, stop searching and read that code span directly.
Do not keep the same base grep and only change the trailing pipe filter. After one grep family, move to `read_file` or edit.
If two direct verify commands show a real behavior delta like `200` vs `422`, stop exploring and patch the best-localized source file.
After a real behavior delta like `200` vs `422`, allow at most one semantic search and one shell search before switching to `read_file` or `edit_file`.
Do not use `search_similar_code` with free-text issue summaries. Use it only with an exact symbol or API name already seen in source.
If the failure is `unexpected keyword argument`, patch the accepting callable signature first. For a normal class, changing only a class attribute is not enough to accept constructor kwargs.
For class or dataclass field edits, find the class line with `grep -n` / `rg -n`, then read that exact class block and patch it immediately.
Once you know the exact file for a symbol/class, do not search guessed alternate files for that same symbol. Re-read or edit the known file, or run one exact verify.
After you successfully edit a known symbol file and then patch downstream files, do not backtrack to rediscover that same symbol/file without a new failing verify or exact downstream evidence.
Do not conclude that installed package versions are the root cause just because `pip list`, version prints, `__file__`, or `sys.path` look odd. Trust the repository stack trace and patch the workspace code unless the sandbox itself is demonstrably broken.
If the direct verify command imports the package from the workspace checkout, do not run `pip list`, version prints, `__file__`, `sys.path`, or constructor signature probes afterward. Move straight to repo reads and the smallest plausible edit.
Do not create repro files with `write_file`, shell redirection, or heredocs. If a direct verify command fails once, use the traceback plus direct repo reads.
Do not use multiline `python -c "..."` verifies. Keep inline Python to one shell line.
If `run_command` returns `error` even with `exit 0`, treat that verify as failed and move to source reads, the exact pytest path, or an edit.
If the traceback already names a test file, use that exact test path before any generic pytest target.
Do not quote tool filepaths, and do not `read_file` the same file more than twice in a row without editing or switching files.
For the second or later edit on a task, immediately re-read the exact target span in that same file before `edit_file`.
Once you already have a real source hit, do not invent neighboring symbol names and search them. Read or edit the real hit.
Do not call `submit_patch` unless a production source file changed and you verified that specific change.
