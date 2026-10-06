# Gemma 4 Developer Agent — Roadmap to Win

**Competition:** [Google - The Gemma 4 Developer Agent Competition](https://www.kaggle.com/competitions/gemma-4-developer-agent/overview)  
**Host:** Google DeepMind / Gemma + Kaggle  
**Paper track:** [gemma-4-developer-agent-paper](https://www.kaggle.com/competitions/gemma-4-developer-agent-paper)

**For build execution:** start at [`AGENTS.md`](../AGENTS.md) → [`implementation-roadmap.md`](./implementation-roadmap.md) (phase cards + KPIs) → [`agent-dev-spec.md`](./agent-dev-spec.md) (schemas/CLIs). Cloud meta-fleet: [`cloud-ops.md`](./cloud-ops.md).

Re-verify dates/limits on live Kaggle Rules / Evaluation / Data tabs before submit.

---

## 1. What it asks for

Build an **autonomous Python coding agent** that, given a frozen repo + PR/issue description, investigates and patches real bugs so **hidden tests pass**.

You do **not** pick the base model. Every entry runs the same:

| Fixed | Value |
| --- | --- |
| Model | `gemma-4-31b-it-qat-w4a16-ct` (4-bit QAT Gemma 4 31B) |
| Serve | vLLM, **32,768** context, **4× NVIDIA L4** (~96 GB VRAM) |
| Sandbox | Offline Docker: **4 GiB RAM / 2 vCPU**, no network |
| Score | Fraction of tasks whose patch passes verifier (`pytest` on fresh copy + held-out tests) |

**What you submit:** `submission.zip` — declarative **Google ADK** config (YAML agents, Markdown prompts, sampling, budgets), optional ADK skills, optional **LoRA/DoRA** adapters. **No host-side Python from your zip runs.**

**Levers:** prompts, multi-agent structure, sampling, budgets, skills, ≤**8** adapters (rank ≤**128** host max; **project default ≤64**), unpacked zip **&lt; 3 GiB**.

---

## 2. Key constraints & evaluation

- **Metric:** resolution rate; near-miss = **0**.
- **~120 hidden tasks**; final rank = **private** half. Public dev set: **129** tasks (fastapi / rich / requests / httpx).
- **1 submission / day**; select up to **2** finals.
- **12 hours** wall clock for all patch generation.
- **9 tools:** workspace (`run_command`, `read_file`, `edit_file`, `write_file`, `get_status`, `submit_patch`) + graph (`get_code_neighbors`, `get_code_subgraph`, `search_similar_code`).

**Hard failure modes:** sample budgets (1 min / 10 calls); failed `edit_file` loops; context overflow; scratch in git diff; empty graphs (must fall back to shell); public-repo overfitting.

**Prizes:** Main $65k · Paper track $35k · Teams ≤5.

---

## 3. Important dates (UTC)

| Milestone | Deadline |
| --- | --- |
| Competition opened | ~Sep 22–23, 2026 |
| **Paper track submit** | **Nov 12, 2026 23:59 UTC** |
| **Entry + team merge** (accept rules) | **Nov 25, 2026 23:59 UTC** |
| **Final agent submission** | **Dec 2, 2026 23:59 UTC** |

Join + accept rules by **Nov 25** even if the agent is not ready.

---

## 4. Execution phases (summary)

Detailed tickets, KPIs, and gates live in [`implementation-roadmap.md`](./implementation-roadmap.md).

| Phase | Goal |
| --- | --- |
| 0 | Contract lock, splits, distillation BLOCK, 4-bit serve parity |
| 1 | Soundness harness (oracle 100% / empty 0%) |
| 2 | MVP single-agent + context-eviction skills; smoke ≥10–15% |
| 3 | Edit/loop/graph/context/budget KPIs; ≤10h/120 extrap |
| 4 | Optional multi-agent; kill unless ≥+5% Δ Pass@1 |
| 5 | BoN+STaR → DoRA SFT → GRPO → TIES merge Plan A |
| 6 | Dry run &lt;10.5h; SHA256 100%; select 2 finals |

**Locked path:** scaffold first, then post-train. SimPO backlog only if GRPO unstable. No PRM.

---

## 5. Risks A–D (project locks)

| Risk | Mitigation |
| --- | --- |
| **A** 32k context cliff | No full-file reads &gt;200 lines; STaR error tail ≤20 lines |
| **B** SFT→SimPO→GRPO over-engineering | Critical path = DoRA SFT → GRPO only |
| **C** PRM data trap | ORM + rule heuristics; never train/ship PRM |
| **D** Multi-LoRA swap latency | Rank ≤64; merged `master_swe_adapter` Plan A |

Other classics: all-or-nothing patches; 12h timeout; edit fragility; graph holes; distillation ambiguity; Nov 25 eligibility.

---

## 6. Compute split

| Layer | Where | Notes |
| --- | --- | --- |
| **Scored runtime** | Kaggle ADK | 4×L4, 32k, 12h — unchanged |
| **Meta / train** | Local and/or **AWS Spot (CPU)** + **RunPod (GPU)** | Optional; **explicit operator approval required** before spend; SKUs are suggestions only — see [`cloud-ops.md`](./cloud-ops.md) |

---

## 7. Next actions (this week)

1. Join Kaggle + accept rules.  
2. Team ≤5 / roles.  
3. Download data; start Phase 0–1 KPIs.  
4. Ship MVP zip with sane budgets; first submit.  
5. Paper track decision by ~Oct 20 if pursuing.  
6. Watch Discussions for distillation policy.

---

## 8. Links

- [Overview](https://www.kaggle.com/competitions/gemma-4-developer-agent/overview) · [Evaluation](https://www.kaggle.com/competitions/gemma-4-developer-agent/overview/evaluation) · [Data](https://www.kaggle.com/competitions/gemma-4-developer-agent/data) · [Rules](https://www.kaggle.com/competitions/gemma-4-developer-agent/rules) · [Paper](https://www.kaggle.com/competitions/gemma-4-developer-agent-paper)
