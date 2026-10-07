---
name: anti-loop
description: Stop repeated failing tool calls and enforce budget discipline (edit-by-12).
---

# Anti-loop + budget discipline

## No-loop
- If the same tool name + args fail twice, change strategy (new query, new file, or stop).
- Do not re-read the same filepath with the same line range more than twice without a new hypothesis.
- Cap identical consecutive failing commands; prefer graph/`rg` pivot over dump-and-retry.
- After two failed `edit_file` attempts on the same span, re-`read_file` the range and copy `old_string` exactly (host arg name) — do not retry the same broken args.
- If `edit_file` fails for missing/invalid args, fix arg names to `filepath` / `old_string` / `new_string` on the next try only; never spam the same broken call.
- Hard stop: 5 `edit_file` calls in one task → no more edits; `submit_patch` or stop.

## Budget discipline
- Call `get_status` about every 8 tool calls to check remaining time / calls / turns.
- **Edit source by call 12:** by tool-call #12 you must have attempted at least one `edit_file` on production source (not tests). If still only exploring at #12, stop searching and edit the best-localized candidate immediately.
- When less than 25% of any budget remains (time, tool calls, or turns): stop exploration; apply the smallest plausible fix or `submit_patch` with the best clean patch. Do not open new files unless required for that fix.
- If stuck after half the budget with no passing check, submit best clean patch or stop.
