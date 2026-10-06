# Gemma 4 Developer Agent Competition

Autonomous Python coding agent for the [Kaggle Gemma 4 Developer Agent](https://www.kaggle.com/competitions/gemma-4-developer-agent/overview) competition.

## Start here

| Audience | Read |
| --- | --- |
| **Any implementing agent** | [`AGENTS.md`](AGENTS.md) |
| Phase tickets + KPIs | [`docs/implementation-roadmap.md`](docs/implementation-roadmap.md) |
| Schemas / CLIs / DoD | [`docs/agent-dev-spec.md`](docs/agent-dev-spec.md) |
| Contest constraints | [`docs/competition-roadmap.md`](docs/competition-roadmap.md) |
| Optional AWS Spot / RunPod | [`docs/cloud-ops.md`](docs/cloud-ops.md) |
| Claims & gates | [`docs/experiment_log.md`](docs/experiment_log.md) |

**Working tree:** [`gemma4-agent/`](gemma4-agent/). Submission artifact = contents of `gemma4-agent/agent/` only (`submission.zip` &lt; 3 GiB).

**Cloud:** never start AWS/RunPod without operator `CLOUD_APPROVAL` in the experiment log.

## Locked strategy

Scaffold + budgets + context eviction → BoN+STaR → DoRA SFT → GRPO → TIES merge (Plan A). No PRM. SimPO backlog only. Adapter rank ≤64.
