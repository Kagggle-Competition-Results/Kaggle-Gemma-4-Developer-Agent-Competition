# Trace schema (P1.5)

One JSON object per line under `traces/<run_id>/<task_id>.jsonl`. Final line `type=result`.

See repo `docs/agent-dev-spec.md` §3.2 for full schema and `failure_tags` enum.

Required on result lines when available: `peak_prompt_tokens`, `failure_tags`, `resolved`, `seconds`.
