# Gemma 4 Developer Agent — Implementation Roadmap

Executable phase cards for coding agents. Read [`AGENTS.md`](../AGENTS.md) first (anti-drift G1–G16).

**Companions:** [`competition-roadmap.md`](./competition-roadmap.md) (contest) · [`agent-dev-spec.md`](./agent-dev-spec.md) (schemas/CLIs/DoD) · [`cloud-ops.md`](./cloud-ops.md) (optional compute) · [`experiment_log.md`](./experiment_log.md)

**Locked strategy:** scaffold + budgets + context eviction first → BoN+STaR → DoRA SFT → GRPO → TIES merge (Plan A). SimPO backlog only if GRPO unstable. No PRM. Rank ≤64. Cloud = optional acceleration with operator approval.

**Working tree:** `gemma4-agent/` (zip = contents of `gemma4-agent/agent/` only).

**Non-negotiables:** model `gemma-4-31b-it-qat-w4a16-ct` · ADK zip &lt;3 GiB · ≤8 adapters · host 9 tools only · 32k ctx · ~12h / ~120 tasks · all-or-nothing pytest.

---

## Risks (locked mitigations)

| Risk | Mitigation |
| --- | --- |
| A — 32k context cliff | G11: no full-file reads &gt;200 lines; STaR error tail ≤20 lines |
| B — Pipeline over-engineering | G12: critical path SFT→GRPO; SimPO backlog only |
| C — PRM data trap | G13: ORM + rule heuristics; no PRM |
| D — Multi-LoRA swap latency | G14: r≤64; TIES merge Plan A; multi-adapter Plan B |

---

## Phase 0 — Contract & environment lock

### Cold-start (read only these)
1. [`AGENTS.md`](../AGENTS.md)
2. This phase
3. [`agent-dev-spec.md`](./agent-dev-spec.md) §§0–2, 3.1, 4.5, 8 (P0.*)
4. [`experiment_log.md`](./experiment_log.md)

### Goal
100% setup reproducibility; prevent post-training disqualification.

### Prerequisites (hard)
None (entry phase). Human: join Kaggle + accept rules (eligibility by Nov 25).

### Must
- Pin host packages; inventory 129 tasks; freeze disjoint splits; init distillation policy BLOCK proprietary.
- Record 4-bit vLLM load parity locally when GPU available.

### Must not
- Peek holdout answers; invent host schema keys; start cloud without `CLOUD_APPROVAL`.

### Cloud (optional)
- Allowed: yes (AWS Spot CPU for data prep). Needs approval before any Spot fleet.

### Tickets
| ID | Work | Owned paths | Artifact | DoD |
| --- | --- | --- | --- | --- |
| P0.1 | Join + accept rules | (Kaggle UI) | eligibility note in log | rules accepted |
| P0.2 | Pin packages / harness | `gemma4-agent/docs/environment.md` | pins documented | PLACEHOLDERs listed |
| P0.3 | Download dataset | `gemma4-agent/data/` | 129 tasks on disk | inventory done |
| P0.4 | Inventory graphs/embeds | `docs/environment.md` | zero-byte flags | logged |
| P0.5 | Freeze splits | `gemma4-agent/configs/splits.json` | splits JSON | 0% overlap |
| P0.6 | GPU / 4-bit serve path | `docs/environment.md` | load &lt;90s note | 1k-token gen OK |
| P0.7 | Distillation policy | `gemma4-agent/configs/distillation_policy.md` | policy file | default BLOCK |
| P0.8 | Workbench notes | `docs/environment.md` | how agents edit repo | no vendor in zip |
| P0.9 | Ticket board seed | `docs/experiment_log.md` | open tickets listed | claimable |

### Quantitative KPIs
| KPI | Threshold | Where |
| --- | --- | --- |
| Task integrity | 100% of 129 tasks parsed; 0 missing/corrupt | inventory / log |
| Split isolation | 0% ID overlap smoke∩dev∩holdout | `splits.json` |
| 4-bit serve parity | Load &lt;90s; 1000-token gen; 0 VRAM OOM | environment.md |
| Distillation policy | Exists; BLOCK proprietary IDE logs | distillation_policy.md |

### Hard gate
- **PASS** if all KPIs met → `GATE_P0 = PASS | …` → unlock Phase 1.
- **FAIL** otherwise → stop; no Phase 1.

### Hand-off
`done P0.x | hash=… | next=… | blocker=…`

---

## Phase 1 — Local harness & soundness

### Cold-start (read only these)
1. `AGENTS.md` 2. This phase 3. `agent-dev-spec.md` §§3.2–3.3, 5 (soundness/eval), 8 (P1.*) 4. `GATE_P0 = PASS` in experiment_log

### Goal
Evaluation suite is a ground-truth proxy for Kaggle scoring.

### Prerequisites (hard)
`GATE_P0 = PASS`; `data/` present; `splits.json` frozen.

### Must
- Empty patch fails; oracle passes; quarantine unsound; tool-faithful runner; trace schema.

### Must not
- Score agents on unsound tasks; invent tools beyond host nine.

### Cloud (optional)
- AWS Spot CPU for parallel soundness — approval required.

### Tickets
| ID | Work | Owned paths | Artifact | DoD |
| --- | --- | --- | --- | --- |
| P1.1 | Soundness checker | `scripts/soundness_check.py` | `results/soundness.csv` | CSV + usable count |
| P1.2 | Quarantine | `configs/splits.json` quarantine | updated splits | unsound excluded |
| P1.3 | Tool-faithful runner | `scripts/run_local_eval.py` | runner | mirrors 9 tools |
| P1.4 | Patch extract parity | runner / docs | scratch rejected | clean diff rule |
| P1.5 | Trace schema | `traces/_schema.md` | schema + 1 manual traj | committed |
| P1.6 | Skill smoke load | runner | skills load check | parity note |

### Quantitative KPIs
| KPI | Threshold | Where |
| --- | --- | --- |
| Oracle TPR | 100% oracle patches pass pytest | soundness.csv |
| Empty FPR | 0% empty patches pass | soundness.csv |
| Usable tasks | ≥80% retained (≥100) | soundness.csv |
| Harness overhead | &lt;5s setup/exec per task (excl. inference) | results JSON |

### Hard gate
- **PASS** → `GATE_P1 = PASS` → unlock Phase 2.
- **FAIL** → do not build MVP against a lying harness.

### Hand-off
`GATE_P1 = PASS|FAIL | usable=… | oracle_tpr=… | empty_fpr=… | overhead_s=… | next=P2`

---

## Phase 2 — MVP single agent (no LoRA) + portable skills

### Cold-start (read only these)
1. `AGENTS.md` 2. This phase 3. `agent-dev-spec.md` §§4, 5 (build/eval), 8 (P2.*) 4. `GATE_P1 = PASS`

### Goal
Valid submission pipeline + non-zero baseline. Encode G11 context eviction in skills.

### Prerequisites (hard)
`GATE_P1 = PASS`.

### Must
- Single LlmAgent; lean system prompt; skills (`debug_workflow`, `patch_rules` with eviction, `anti_loop`); sane budgets (not 1min/10 calls); zip builder; smoke eval; first Kaggle submit when ready.

### Must not
- Add LoRA; full-file `read_file` &gt;200 lines in skills; put IDE config in `agent/`.

### Cloud (optional)
- Prefer local. No train cloud needed.

### Tickets
| ID | Work | Owned paths | Artifact | DoD |
| --- | --- | --- | --- | --- |
| P2.1 | `agent.yaml` | `agent/agent.yaml` | YAML | host schema valid |
| P2.2 | system prompt | `agent/prompts/system.md` | prompt | points at skills |
| P2.2b | Skills pack | `agent/skills/*.md` | 3–4 skills | G11 eviction in patch/nav |
| P2.3 | Sampling | `agent.yaml` gen config | temp 0.2; max_out 4–8k | thoughts off init |
| P2.4 | Budgets | `agent/eval_config.yaml` | ~4–6 min, 40–80 tools | not sample defaults |
| P2.5 | Zip builder | `scripts/build_submission.py` | `submission.zip` | &lt;3 GiB + sha256 |
| P2.6 | Smoke eval | traces + results | `results/smoke.json` | 0 hard crashes |
| P2.7 | First Kaggle submit | experiment_log | Public score | logged or blocker |

### Quantitative KPIs
| KPI | Threshold | Where |
| --- | --- | --- |
| Zip validity | &lt;3 GiB; ADK schema OK | build_submission |
| Baseline Pass@1 | ≥10–15% on `splits.smoke` | results/smoke.json |
| Execution stability | 0% hard crashes / unhandled exits | traces |

### Hard gate
- **PASS** → `GATE_P2 = PASS` → unlock Phase 3.
- **FAIL** → fix scaffold before ablations.

### Hand-off
`GATE_P2 = PASS|FAIL | pass_at_1=… | zip_gib=… | crashes=0 | next=P3`

---

## Phase 3 — Reliability, budgets & graph-first nav

### Cold-start (read only these)
1. `AGENTS.md` 2. This phase 3. `agent-dev-spec.md` §§4.4, 4.6, 5 (classify), 8 (P3.1) 4. `GATE_P2 = PASS` + Phase 2 failed_edit baseline

### Goal
Plug context leaks, kill loops, enforce budget discipline.

### Prerequisites (hard)
`GATE_P2 = PASS`. One ablation variable per experiment (G3).

### Must
- Budget Pareto; strengthen edit reliability; anti-loop; context reserve; graph-first + shell fallback; measure peak tokens.

### Must not
- Multi-change ablations; ignore G11; proceed without budget extrapolation.

### Cloud (optional)
- Yes — parallel prompt/budget/tool sweeps. Suggested ~10–20 workers (AWS Spot / RunPod). **Operator chooses SKUs.** Needs `CLOUD_APPROVAL` before spin-up. KPIs do not relax.

### Tickets
| ID | Work | Owned paths | Artifact | DoD |
| --- | --- | --- | --- | --- |
| P3.1 | Budget Pareto | `eval_config.yaml`, `configs/budget_grid.yaml` | winner budgets | extrap ≤10h |
| P3.2 | Edit reliability | `skills/patch_rules.md` | skill v2 | failed_edit ↓ ≥50% vs P2 |
| P3.3 | Anti-loop | `skills/anti_loop.md` | skill | loops &lt;5% |
| P3.4 | Context reserve | gen config / skills | lower max_out if needed | peak ≤28k |
| P3.5 | Graph-first nav | `skills/nav_rules.md` + enable graph tools | nav skill | hit ≥60% or kill |
| P3.6 | Thinking ablate | agent.yaml | on/off result | keep if holdout↑ |
| P3.7 | Temp ablate | agent.yaml | 0.0/0.2/0.7 | pick winner |

### Quantitative KPIs
| KPI | Threshold | vs baseline |
| --- | --- | --- |
| Failed edit rate | &lt;10% of `edit_file` calls | ≥50% reduction vs Phase 2 |
| Duplicate cmd loops | &lt;5% of agent steps | identical consecutive failures |
| Graph tool hit rate | ≥60% successful symbol resolution | `rg` only on empty returns |
| Peak context footprint | ≤28,000 tokens | 0 truncation-amnesia events |
| Extrapolated budget | ≤10h for 120 tasks | ≥2h margin under 12h |

### Hard gate
- **PASS** only if budget extrap holds **and** edit-fail ≤ half of Phase 2 (and &lt;10%) → unlock optional P4 or P5.
- **FAIL** → no P4/P5.

### Hand-off
`GATE_P3 = PASS|FAIL | failed_edit=… | loops=… | graph_hit=… | peak_tokens=… | extrap_h=… | next=P4|P5`

---

## Phase 4 — Multi-stage sub-agent topology (optional)

### Cold-start (read only these)
1. `AGENTS.md` 2. This phase 3. `agent-dev-spec.md` §4.1 (host multi-agent schema) 4. `GATE_P3 = PASS`

### Goal
Decide if specialized sub-agents beat single-agent by numeric Δ.

### Prerequisites (hard)
`GATE_P3 = PASS`. Skip entirely if team chooses single-agent (log `GATE_P4 = PIVOT | topology=single | skipped=true`).

### Must
- Navigator / patcher / verifier prompts; orchestrator; A/B vs Phase 3 single-agent on `dev` same budgets.

### Must not
- Require multi-LoRA at this phase; ship r=128; ignore handoff/context regressions.

### Cloud (optional)
- Parallel A/B runs with approval.

### Tickets
| ID | Work | Owned paths | Artifact | DoD |
| --- | --- | --- | --- | --- |
| P4.1 | repo_navigator | prompts + yaml | read-only nav agent | no edits |
| P4.2 | diff_patcher | prompts + yaml | surgical edits | patch_rules |
| P4.3 | test_verifier | prompts + yaml | focused tests | one repair handoff max |
| P4.4 | lead_orchestrator | `agent.yaml` | root wiring | host schema |
| P4.5 | Adapter fields | yaml stubs | empty/omit until P5 | clean A/B |
| P4.6 | A/B vs single | results | Δ Pass@1 on dev | kill rule applied |

### Quantitative KPIs
| KPI | Threshold |
| --- | --- |
| Topology Δ Pass@1 | ≥ **+5% absolute** on `dev` vs Phase 3 single-agent |
| Handoff latency | &lt;2% of total task time |

### Hard gate
- **PASS** (≥+5%, no context-truncation regression) → freeze multi for P5.
- **PIVOT** if Δ &lt;+5% or truncation → keep single-agent; `GATE_P4 = PIVOT | topology=single`.

### Hand-off
`GATE_P4 = PASS|PIVOT|FAIL | delta_pass=… | handoff_pct=… | topology=… | next=P5`

---

## Phase 5 — Execution-guided post-training

### Cold-start (read only these)
1. `AGENTS.md` 2. This phase 3. `agent-dev-spec.md` §§5–6 4. `GATE_P3 = PASS` + scaffold freeze (`GATE_P4` PASS or PIVOT) 5. `cloud-ops.md` if using GPU fleet 6. `distillation_policy.md`

### Goal
Train adapters that raise Pass@1 under 4-bit serve without forgetting or context bloat.

### Prerequisites (hard)
Scaffold freeze logged; `GATE_P3 = PASS`; policy current (P5.0).

### Must (critical path)
```text
5A BoN + STaR (error ≤20 lines) → 5B DoRA SFT → 5C GRPO → 5E TIES merge Plan A
```
Reward: \(R = R_{exec} + R_{syntax} + R_{heuristic} - R_{penalty}\) (heuristics = rules, not PRM).

### Must not
- SimPO on critical path (backlog only if GRPO fails); train PRM; default r=128; train holdout IDs; blocked distillation sources; start RunPod/AWS without approval.

### Cloud (optional)
- **5A:** RunPod GPU workers; suggested \(N\) up to 32–64 — operator decides.
- **5B/5C:** multi-GPU nodes; suggested GRPO \(G\) 16–32 when VRAM allows — operator decides.
- Always re-check adapters on 4-bit / 4×L4-parity before ship.

### Tickets
| ID | Work | Owned paths | Artifact | DoD |
| --- | --- | --- | --- | --- |
| P5.0 | Refresh distillation policy | `configs/distillation_policy.md` | policy | ALLOW/BLOCK current |
| P5.1a | BoN rejection sampling | `scripts/rejection_sample.py` | `traces/bon/keep/` | ≥1000 clean |
| P5.1b | STaR recovery | `scripts/star_correct.py` | `traces/star/` | ≥200 recoveries; `--error-tail-lines 20` |
| P5.2 | Hard filters | filter scripts | clean set | no loops/scratch/&gt;28k |
| P5.5 | Render trajs | `scripts/render_trajectories.py` | `data_train/sft.jsonl` | mask tool toks |
| P5.6 | DoRA SFT | `scripts/train_qlora.py` + config | `agent/adapters/*` | r≤64; 4-bit check |
| P5.7 | 4-bit serve check | results | Δ outputs proof | non-noop |
| P5.8 | Holdout/dev A/B | results | metrics | Δ Pass@1 logged |
| P5.10–13 | GRPO | `scripts/train_grpo.py` | adapter + curves | E[R]↑; token −15% |
| P5.19–21 | TIES/DARE merge | `scripts/merge_adapters.py` | `master_swe_adapter` | ≥95% parity or Plan B |
| P5.SimPO | **Backlog** | train_simpo | prefs | only if GRPO unstable |

### Quantitative KPIs
| Stage | KPI | Threshold |
| --- | --- | --- |
| 5A | BoN yield | ≥1000 clean test-passing trajs (non-holdout) |
| 5A | STaR yield | ≥200 successful 2-turn recoveries |
| 5B | Loss | Stabilizes; no divergence |
| 5B | Forgetting | Python eval PPL ≤1.1× base |
| 5B | Dev Δ Pass@1 | +8% to +12% absolute vs P3/P4 prompt-only |
| 5C | Reward trajectory | Mean group E[R] increases over steps |
| 5C | Token efficiency | ≥15% fewer tokens per successful task vs SFT |
| 5E | Merge parity | Merged ≥95% of multi-adapter Pass@1 on dev |
| 5E | vLLM latency | Multi-adapter swap overhead = 0 (single load) |

### Hard gate
- 5A FAIL → no SFT.
- 5B FAIL → no GRPO; iterate data/SFT.
- 5C FAIL → backlog SimPO **or** ship best SFT; log choice.
- 5E &lt;95% → **PIVOT** Plan B multi-adapter (r≤64) or best single specialist.
- **PASS** overall → `GATE_TRAIN_SHIP = PASS` when ≥1 adapter holds on 4-bit + provenance clean.

### Hand-off
`GATE_P5A = …` / `GATE_P5B = …` / `GATE_P5C = …` / `GATE_P5E = PASS|PIVOT | … | next=P6`

---

## Phase 6 — Polish, finals & submission

### Cold-start (read only these)
1. `AGENTS.md` 2. This phase 3. `agent-dev-spec.md` §5 build_submission 4. `GATE_TRAIN_SHIP` or best scaffold-only finalist ready

### Goal
Zero-risk official scoring execution; select ≤2 finals by Dec 2.

### Prerequisites (hard)
At least one candidate with Phase 2+3 PASS; training ship optional for scaffold-only finalist.

### Must
- Full-budget dry run; freeze 2 finals (prefer scaffold + merged adapter); hygiene; SHA256 traceability; entry/merge by Nov 25; finals by Dec 2.

### Must not
- Submit without GATE_P6 PASS; leave scratch in diff; dead adapter refs.

### Cloud (optional)
- Spot CPU farm for dry-run parallelism — approval required. Final check on 4×L4-parity path.

### Tickets
| ID | Work | Owned paths | Artifact | DoD |
| --- | --- | --- | --- | --- |
| P6.1 | Full-budget dry run | results | extrap &lt;10.5h | 0 unhandled/OOM |
| P6.2 | Freeze 2 finals | experiment_log | hashes + scores | diversified |
| P6.3 | Hygiene | agent/ | clean zip | no scratch/dead refs |
| P6.4 | Repro pack | docs | pins + provenance | winner-ready |
| P6.5 | Paper (optional) | paper draft | by Nov 12 | if opted |
| P6.6 | Entry/merge | Kaggle | by Nov 25 | rules accepted |
| P6.7 | Final submit | submission.zip | by Dec 2 23:59 UTC | selected |

### Quantitative KPIs
| KPI | Threshold |
| --- | --- |
| Full benchmark simulation | 120-task local dry run &lt;10.5h; 0 unhandled exceptions/OOM |
| Determinism | 3× smoke: Pass@1 within ±1 task |
| Artifact traceability | 100% prompts/skills/adapters in zip have SHA256 in experiment_log |

### Hard gate
- **PASS** → eligible to select ≤2 finals / submit.
- **FAIL** → no final selection.

### Hand-off
`GATE_P6 = PASS|FAIL | dry_h=… | smoke_var=… | sha_coverage=1.0 | next=submit`

---

## First parallel tickets (dispatch now)

1. `P0.5` + `P0.7` + `P0.9` — splits, policy, board  
2. `P0.1`–`P0.4` — human/platform (join, pins, data)  
3. After `GATE_P0`: `P1.1`  
4. Draft-only (no smoke until `GATE_P1`): `P2.1`–`P2.2b` YAML/skills  

Claim in `experiment_log.md` before editing.
