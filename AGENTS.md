# AGENTS.md — Multi-Agent Entry Point

**Read this first.** Then open the **one phase card** you are executing in [`docs/implementation-roadmap.md`](docs/implementation-roadmap.md) and the linked [`docs/agent-dev-spec.md`](docs/agent-dev-spec.md) sections. Do not need chat history.

Human strategy: [`docs/competition-roadmap.md`](docs/competition-roadmap.md). 
Cloud (only if ticket needs it): [`docs/cloud-ops.md`](docs/cloud-ops.md). 
Hand-offs: [`docs/experiment_log.md`](docs/experiment_log.md).
Harness verify / soundness Docker pins: [`gemma4-agent/docs/harness_verify.md`](gemma4-agent/docs/harness_verify.md).

Working tree: `gemma4-agent/`. **Submission zip = `gemma4-agent/agent/` only.**

---

## Mission

Build a Kaggle ADK agent that patches real Python bugs under host tools so hidden tests pass. Base model is fixed: `gemma-4-31b-it-qat-w4a16-ct`.

**Locked path:** scaffold + budgets + context eviction → BoN+STaR → DoRA SFT → GRPO → TIES merge (Plan A). SimPO backlog only if GRPO unstable. No PRM. Rank ≤64.

---

## Anti-drift guards (G1–G16)

Violation = invalid work (discard / revert).

| ID | Rule |
| --- | --- |
| G1 Scope | Touch only paths for the claimed ticket. No drive-by refactors. |
| G2 Invent | Do not invent tools, YAML keys, CLIs, or schemas. Use agent-dev-spec; if missing, `blocker=` and STOP. Host wins over templates. |
| G3 One change | One ablation variable per experiment (budget XOR prompt/skill XOR tools XOR adapter). |
| G4 Holdout | Never peek/tune/train on holdout task IDs. |
| G5 Zip boundary | Zip = `agent/` only. No IDE config, `scripts/`, or credentials in zip. |
| G6 Model | Only `gemma-4-31b-it-qat-w4a16-ct`. |
| G7 Gate | Do not start tickets past a closed gate. Check experiment_log. |
| G8 Claim | Claim ticket in experiment_log before edit; one writer per path. |
| G9 Stop | On DoD met or blocker: append hand-off and stop. No extras. |
| G10 Distill | Train only sources allowed in `configs/distillation_policy.md`. Default BLOCK proprietary. |
| G11 Context | No full-file reads &gt;200 lines; STaR errors ≤ last 20 lines. |
| G12 Train path | Critical path = DoRA SFT → GRPO; SimPO only if GRPO fails. |
| G13 No PRM | ORM + rule heuristics only; never train/ship PRM. |
| G14 Adapters | Rank ≤64; merge Plan A; multi-LoRA Plan B. |
| G15 Numeric gates | Phase complete only with measured KPIs; gate = `PASS` \| `FAIL` \| `PIVOT`. Missing KPI = FAIL. |
| G16 Cloud spend | Do not create/start/scale cloud without live `CLOUD_APPROVAL = YES` in experiment_log. SKUs are suggestions — operator picks. |

---

## Claim / done / gate formats

```text
YYYY-MM-DDThh:mm:ssZ | claim | P0.5 | agent=<name> | hash=none | metrics= | next= | blocker=none
YYYY-MM-DDThh:mm:ssZ | done  | P0.5 | agent=<name> | hash=<sha> | metrics=k=v | next=P0.7 | blocker=none
YYYY-MM-DDThh:mm:ssZ | gate  | GATE_P3 | agent=<name> | hash=<sha> | metrics=failed_edit=0.08,... | next=P5 | blocker=none
YYYY-MM-DDThh:mm:ssZ | cloud | CLOUD_APPROVAL | agent=operator | hash=none | metrics=budget_usd_max=50,provider=runpod,purpose=P5A,expires=... | next= | blocker=none
```

---

## Gates index

| Gate | Unlocks |
| --- | --- |
| `GATE_P0` | Phase 1 |
| `GATE_P1` | Phase 2 |
| `GATE_P2` | Phase 3 |
| `GATE_P3` | Phase 4 (optional) or Phase 5 |
| `GATE_P4` PASS or PIVOT | Scaffold freeze → Phase 5 |
| `GATE_TRAIN_SHIP` | Phase 6 training finalist path |
| `GATE_P6` | Official final selection / submit |

KPI thresholds: phase cards in `docs/implementation-roadmap.md`.

---

## Dispatch now (parallel)

| Stream | Tickets |
| --- | --- |
| Platform | `P0.1`–`P0.4`, `P0.5`, `P0.7`, `P0.9` |
| After GATE_P0 | `P1.1` |
| Scaffold draft (no smoke until GATE_P1) | `P2.1`–`P2.2b` |

---

## Implementer prompt (copy/paste)

```text
You are implementing ticket {ID}.
Read only: AGENTS.md + that ticket's phase card in docs/implementation-roadmap.md
+ linked agent-dev-spec sections. If cloud needed: docs/cloud-ops.md + live CLOUD_APPROVAL.
Obey G1–G16 and phase Must/Must-not. Touch owned paths only. Write artifacts + KPIs.
Append experiment_log. Stop at DoD or blocker. Do not provision cloud without approval.
```
