# Anti-loop

- If the same tool name + args fail twice, change strategy (new query, new file, or stop).
- Do not re-read the same filepath with the same line range more than twice without a new hypothesis.
- Cap identical consecutive failing commands; prefer graph/`rg` pivot over dump-and-retry.
- If stuck after budget half consumed with no passing check, submit best clean patch or stop.
