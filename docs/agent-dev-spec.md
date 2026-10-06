# Agent Development Spec — Executable Contracts

Companion to [`implementation-roadmap.md`](./implementation-roadmap.md) and [`AGENTS.md`](../AGENTS.md).

**Purpose:** I/O, schemas, templates, ownership, and CLIs so any agent can implement tickets without inventing interfaces. **Host schema wins** over templates.

---

## 0. Non-negotiables

1. Scored runtime = host ADK + Gemma 4 only. `scripts/` never ships in `submission.zip`.
2. Pack only `gemma4-agent/agent/` into the zip.
3. Claim ticket → edit owned paths → append `docs/experiment_log.md` → stop at gate.
4. One writer per path (§2).
5. G1–G16 in `AGENTS.md` apply (context eviction, train path, no PRM, cloud approval, numeric gates).

---

## 1. Dependency graph

```text
P0.* → GATE_P0 → P1.* → GATE_P1 → P2.* → GATE_P2 → P3.* → GATE_P3
  → [optional P4.* PASS|PIVOT] → scaffold_freeze
  → P5.0 → P5.1a/b → P5.5 → P5.6 DoRA → P5.10–13 GRPO → P5.19–21 merge
  → GATE_TRAIN_SHIP → P6.* → GATE_P6

P5.SimPO = BACKLOG only if GRPO unstable
PRM tickets = DELETED
```

| Gate | Enum | Condition (see implementation-roadmap KPIs) |
| --- | --- | --- |
| `GATE_P0` … `GATE_P6` | `PASS` \| `FAIL` \| `PIVOT` | All phase KPIs measured; missing KPI = FAIL |
| `GATE_TRAIN_SHIP` | `PASS` \| `FAIL` | ≥1 adapter holdout/dev↑ on 4-bit; provenance clean |
| `CLOUD_APPROVAL` | `YES` (with expiry) | Required before any cloud spend — [`cloud-ops.md`](./cloud-ops.md) |

---

## 2. Path ownership

| Path | Owner tickets |
| --- | --- |
| `gemma4-agent/configs/splits.json` | P0.5 |
| `gemma4-agent/configs/distillation_policy.md` | P0.7, P5.0 |
| `docs/environment.md` / `gemma4-agent/docs/environment.md` | P0.2, P0.6, P0.8 |
| `gemma4-agent/docs/harness_verify.md` | P1.1 soundness Docker/pin runbook (versioned) |
| `gemma4-agent/sandbox/` | P1.1 Docker recipe backup (`Dockerfile.gemma4`, `extras.txt`, `pin_deps.py`) — **not** AMI |
| `docs/experiment_log.md` | **all** (append-only) |
| `docs/cloud-ops.md` | operator + platform (content); agents never spend without approval |
| `gemma4-agent/scripts/soundness_check.py` | P1.1 |
| `gemma4-agent/scripts/apply_quarantine.py` | P1.2 |
| `gemma4-agent/scripts/run_local_eval.py` | P1.3–P1.6, P2.6 |
| `gemma4-agent/traces/_schema.md` | P1.5 |
| `gemma4-agent/agent/agent.yaml` | P2.1, P3.*, P4.* |
| `gemma4-agent/agent/eval_config.yaml` | P2.4, P3.1 |
| `gemma4-agent/agent/prompts/*` | P2.2, P4.* |
| `gemma4-agent/agent/skills/*` | P2.2b, P3.2–P3.5 |
| `gemma4-agent/scripts/build_submission.py` | P2.5 |
| `gemma4-agent/scripts/classify_failures.py` | P2.6+, P3 |
| `gemma4-agent/scripts/rejection_sample.py` / `star_correct.py` | P5.1a/b |
| `gemma4-agent/scripts/train_qlora.py` / `train_grpo.py` / `merge_adapters.py` | P5.6–P5.21 |
| `gemma4-agent/scripts/train_simpo.py` | **P5.SimPO backlog only** |
| `gemma4-agent/agent/adapters/*` | P5.6+, P5.19 |
| `gemma4-agent/prefs/` | P5.SimPO only |

`train_prm.py` — **not required; do not implement.**

---

## 3. Schemas

### 3.1 `configs/splits.json`

```json
{
  "version": 1,
  "created_utc": "2026-10-06T00:00:00Z",
  "smoke": ["task_id_1", "task_id_2"],
  "dev": ["..."],
  "holdout": ["..."],
  "quarantine": ["..."],
  "notes": "Holdout never used for prompt/adapter tuning. 0% ID overlap required."
}
```

Targets: smoke 8–12; dev ~40; holdout ≥30.

### 3.2 Trace event (`traces/<run_id>/<task_id>.jsonl`)

```json
{"ts": 0.0, "type": "tool_call", "tool": "read_file", "args": {"filepath": "x.py", "start_line": 1, "end_line": 40}}
{"ts": 1.2, "type": "tool_result", "tool": "read_file", "ok": true, "chars": 1200, "truncated": false}
{"ts": 30.0, "type": "result", "task_id": "fastapi__x", "resolved": false, "seconds": 30.0, "tool_counts": {"read_file": 3, "edit_file": 2}, "patch_sha256": "...", "error": null, "failure_tags": ["edit_fail"], "peak_prompt_tokens": 12000}
```

**`failure_tags`:** `localize` | `understand` | `edit_fail` | `test_fail` | `loop` | `oom_context` | `budget` | `scratch_pollution` | `harness` | `other`

### 3.3 `results/<run_id>.json` (KPI fields required)

```json
{
  "run_id": "20261006T120000Z_mvp_smoke",
  "ticket": "P2.6",
  "phase": "P2",
  "config_sha256": "...",
  "agent_dir_sha256": "...",
  "split": "smoke",
  "n": 10,
  "resolved": 1,
  "resolve_rate": 0.1,
  "pass_at_1": 0.1,
  "mean_seconds": 180.0,
  "p95_seconds": 320.0,
  "extrapolated_hours_120": 6.5,
  "failed_edit_rate": 0.4,
  "loop_rate": 0.2,
  "graph_hit_rate": null,
  "peak_tokens": 18000,
  "truncation_amnesia_events": 0,
  "hard_crash_rate": 0.0,
  "gate": null,
  "tags": {"budget_min": 5, "max_tool_calls": 60}
}
```

### 3.4 Experiment log lines

```text
YYYY-MM-DDThh:mm:ssZ | claim|done|kill|gate|cloud | TICKET_OR_GATE | agent=<name> | hash=<sha256> | metrics=<k=v,...> | next=<ticket|none> | blocker=<text|none>
```

Gate example:
```text
2026-10-06T15:00:00Z | gate | GATE_P3 | agent=cursor-1 | hash=abc | metrics=failed_edit=0.08,loops=0.03,graph_hit=0.62,peak_tokens=27100,extrap_h=9.4 | next=P5 | blocker=none
```

Cloud approval:
```text
2026-10-06T16:00:00Z | cloud | CLOUD_APPROVAL | agent=operator | hash=none | metrics=budget_usd_max=50,provider=runpod,purpose=P5A,expires=2026-10-07T00:00:00Z | next=P5.1a | blocker=none
```

---

## 4. Starter templates (MVP)

### 4.1 `agent/agent.yaml` (P2.1 — validate against host)

```yaml
name: repository_fixer
model: gemma-4-31b-it-qat-w4a16-ct
instruction: !include prompts/system.md
tools:
  - run_command
  - read_file
  - edit_file
  - write_file
  - get_status
  - submit_patch
generate_content_config:
  temperature: 0.2
  max_output_tokens: 8192
  thinking_config:
    include_thoughts: false
```

### 4.2 `agent/eval_config.yaml` (P2.4 — PLACEHOLDER keys)

```yaml
# PLACEHOLDER — rename keys to match competition eval_config schema
per_task:
  time_limit_sec: 300
  max_tool_calls: 60
  max_turns: 80
```

### 4.3 `agent/prompts/system.md`

```markdown
You repair a Python repository for the issue in the user message.
Workspace root is /workspace. Work offline. Dependencies are preinstalled.

Follow skills: debug_workflow, patch_rules, anti_loop (and nav_rules when graph tools are enabled).

Process:
1) Restate expected vs actual behavior.
2) Localize with search + narrow ranged reads (never dump files >200 lines).
3) Reproduce with a focused check when feasible.
4) Apply the smallest edit_file change using exact text from the file.
5) Re-verify; remove scratch files; call submit_patch.

Never invent escape sequences in edit_file. If the same command fails twice, change strategy.
```

### 4.4 Skill contracts (P2.2b / P3)

**`debug_workflow.md`:** Observe → Plan → Search → Patch → Verify; one cycle then re-plan.

**`patch_rules.md` (G11):** Prefer `edit_file`; exact `old_str` from last read; ≤~100 new tokens when possible; re-read after edit; **never `read_file` a whole file if &gt;200 lines** — use `rg` / ranged reads; no full-file `write_file` rewrites except new files.

**`anti_loop.md`:** Identical failing tool args ≥2 → change strategy; cap repeated reads of same path.

**`nav_rules.md`:** Symbol → `get_code_neighbors` / `get_code_subgraph` / `search_similar_code`; if empty → `rg` + ranged `read_file`.

### 4.5 `configs/distillation_policy.md`

```markdown
# Distillation policy
default: BLOCK proprietary_ide and closed API teacher trajectories
allow: self, synth, open_teacher (Apache-2.0 / explicitly permitted)
review: re-check Kaggle Discussions before flipping proprietary_ide to ALLOW
ledger: every train row must have source= and license=
```

### 4.6 `configs/budget_grid.yaml`

```yaml
grids:
  - {time_limit_sec: 180, max_tool_calls: 40}
  - {time_limit_sec: 300, max_tool_calls: 60}
  - {time_limit_sec: 360, max_tool_calls: 80}
  - {time_limit_sec: 480, max_tool_calls: 100}
selection_rule: max resolve_rate on dev subject to extrapolated_hours_120 <= 10.0
```

---

## 5. Script CLI contracts

Always write `results/` JSON with KPI fields (§3.3). Default compute = **local**. Optional `--workers` for cloud fan-out only after `CLOUD_APPROVAL`.

### `scripts/build_submission.py`
```bash
python scripts/build_submission.py --agent-dir agent --out submission.zip
# Exit 0 if unpacked < 3 GiB; print sha256. Exit 2 if oversize/missing.
```

### `scripts/soundness_check.py`
```bash
python scripts/soundness_check.py --data data --out results/soundness.csv
# Columns: task_id,empty_pass,oracle_pass,error,usable
# Spot-safe: --resume --s3-uri s3://…/results/soundness.csv (--s3-every N)
# Image rebuild: --rebuild-image  (applies SANDBOX_EXTRA_PIP)
# NEVER install all /wheels/*.whl — see gemma4-agent/docs/harness_verify.md
```

### `scripts/apply_quarantine.py`
```bash
python scripts/apply_quarantine.py --soundness results/soundness.csv --splits configs/splits.json
# Moves usable=false task_ids into splits.quarantine; strips them from smoke/dev/holdout/train_pool
# --dry-run to print only
```

### `scripts/run_local_eval.py`
```bash
python scripts/run_local_eval.py \
  --agent-dir agent --data data \
  --split configs/splits.json:smoke \
  --out-dir traces/<run_id> --results results/<run_id>.json
```

### `scripts/classify_failures.py`
```bash
python scripts/classify_failures.py --traces traces/<run_id> --out results/<run_id>_failures.json
```

### `scripts/rejection_sample.py`
```bash
python scripts/rejection_sample.py --tasks <list> --n 8 --agent-dir agent --out traces/bon/
# Keeps only resolved=true under traces/bon/keep/
# --n is operator-chosen; cloud may use higher N after approval
```

### `scripts/star_correct.py`
```bash
python scripts/star_correct.py --tasks <list> --agent-dir agent --max-retries 1 \
  --error-tail-lines 20 --out traces/star/
# REQUIRED: truncate verifier/stderr to last 20 lines before retry prompt (G11)
```

### `scripts/render_trajectories.py`
```bash
python scripts/render_trajectories.py --in traces/... --out data_train/sft.jsonl --mask-tool-results
```

### `scripts/train_qlora.py`
```bash
python scripts/train_qlora.py --config configs/train_qlora.yaml --data data_train/sft.jsonl --out agent/adapters/<name>
# peft_type: dora (preferred) | lora; rank ∈ {32,64}
```

### `scripts/train_grpo.py`
```bash
python scripts/train_grpo.py --config configs/train_grpo.yaml --out agent/adapters/<name>
# R = R_exec + R_syntax + R_heuristic - R_penalty
# Heuristics (rules): +0.1 graph/AST before first edit; -0.2 exact-repeat tool args
```

### `scripts/merge_adapters.py`
```bash
python scripts/merge_adapters.py --config configs/merge_ties.yaml --out agent/adapters/master_swe_adapter
# Plan A: TIES (DARE ablate). Must 4-bit serve-check.
```

### `scripts/train_simpo.py` (backlog only)
```bash
python scripts/train_simpo.py --config configs/train_simpo.yaml --out agent/adapters/<name>
# Only if GATE notes GRPO unstable
```

---

## 6. Training numeric defaults

| Knob | Default | Notes |
| --- | --- | --- |
| BoN \(N\) | 8 local; up to 32–64 if cloud approved | Operator chooses |
| STaR retries | 1 | `--error-tail-lines 20` required |
| PEFT | **DoRA** preferred | LoRA ablate OK |
| Rank | **32 or 64** | α=2r; do **not** default 128 |
| LR | 2e-4 | QLoRA/DoRA |
| Targets | attn + MLP | |
| Seq len train | ≤28000 | under 32768 |
| GRPO \(G\) | 4–8 local; 16–32 if multi-GPU approved | Operator chooses |
| \(\lambda_t, \lambda_u\) | 1e-5 / token, 0.01 / tool | Tune \(\lambda_t\) hard |
| \(R_{heuristic}\) | graph-before-edit +0.1; loop −0.2 | Not a learned PRM |
| SimPO | **backlog** | Only if GRPO fails |
| PRM | **deleted** | Do not train |
| Merge | TIES Plan A | ≥95% ensemble parity or Plan B |

---

## 7. Makefile targets

```makefile
.PHONY: soundness smoke zip submit-check
soundness:
	python scripts/soundness_check.py --data data --out results/soundness.csv
smoke:
	python scripts/run_local_eval.py --agent-dir agent --data data --split configs/splits.json:smoke --out-dir traces/smoke --results results/smoke.json
zip:
	python scripts/build_submission.py --agent-dir agent --out submission.zip
submit-check: zip
	@echo "Upload submission.zip via Kaggle UI; log Public score in docs/experiment_log.md"
```

---

## 8. Per-ticket DoD (early)

### P0.5
- [ ] Disjoint smoke/dev/holdout; 0% overlap; counts in log

### P0.7
- [ ] Policy exists; default BLOCK proprietary_ide

### P1.1
- [ ] CSV one row/task; usable summary printed

### P1.3–P1.5
- [ ] Runner path for read/run/submit; schema doc; 1 manual traj

### P2.1–P2.5
- [ ] Templates present; `make zip` &lt;3 GiB; zip = `agent/` only; skills encode G11

### P2.6
- [ ] `results/smoke.json` with KPI fields; `hard_crash_rate=0`; pass_at_1 logged

### P3.1
- [ ] ≥3 grid points on `dev`; winner in `eval_config.yaml`; `extrapolated_hours_120 <= 10.0`

Phase KPI gates (PASS/FAIL/PIVOT) are authoritative in [`implementation-roadmap.md`](./implementation-roadmap.md).

---

## 9. Implementing-agent prompt

```text
You are implementing ONE ticket {ID}.
Read: AGENTS.md (G1–G16) + the matching phase card in docs/implementation-roadmap.md
     + linked sections of docs/agent-dev-spec.md.
If the ticket needs cloud: docs/cloud-ops.md and a live CLOUD_APPROVAL=YES line, else STOP.
Rules: owned paths only; no inventing schemas; zip boundary; stop at DoD or blocker;
append experiment_log; host schema wins; never start cloud without approval.
```

---

## 10. Host-dependent PLACEHOLDERS (fill in P0.2 / P1.3)

- Exact `eval_config.yaml` keys
- ADK skill include syntax
- `submit_patch` / git ref behavior
- Adapter directory layout for host vLLM
- Official pins (`swegemma`, `adk-submission`, …)

Track in `gemma4-agent/docs/environment.md`.
