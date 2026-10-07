---
name: debug-workflow
description: Observe-plan-search-patch-verify loop with /tmp repro before edits.
---

# Observe → Plan → Search → Repro → Patch → Verify

Run one cycle, then re-plan on failure. Do not chain unbounded tool spam.

1. **Observe / plan** — expected vs actual from the issue.
2. **Search** — localize with `rg` / graph tools + ranged reads (never dump files >40 lines).
3. **Repro** — before editing, write `/tmp/repro.py` and run it to confirm the failure.
4. **Patch** — smallest `edit_file` with whitespace-exact `old_str` from the last read.
5. **Verify** — re-run the repro and/or a targeted pytest path; then remove scratch and `submit_patch`.

Prefer localization before edits. Prefer small patches. Verify before `submit_patch`.
