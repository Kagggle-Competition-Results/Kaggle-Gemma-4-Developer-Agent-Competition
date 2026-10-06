# Cloud Ops — Meta-Fleet (Optional)

**Kaggle scored runtime is unchanged** (4×L4, 32k, ~12h, ADK zip). This doc covers **optional** acceleration for meta-workbench and training only.

**G16:** No agent may create, start, scale, or spend on cloud resources without explicit operator approval recorded first in [`experiment_log.md`](./experiment_log.md).

Machine counts, GPU SKUs, BoN \(N\), and GRPO \(G\) below are **suggestions**. The operator chooses concrete instances at time of need based on availability, budget, and region.

---

## Providers

| Role | Provider | Typical use |
| --- | --- | --- |
| CPU-heavy / cheap parallel | **AWS Spot** | Data prep, soundness farms, pytest, dry-run fan-out |
| GPU | **RunPod.ai** | Trajectory rollouts, DoRA SFT, GRPO |

Never put credentials, SDKs, or fleet configs inside `gemma4-agent/agent/`. Secrets via env vars only; never commit keys.

---

## Approval protocol

1. Agent proposes: purpose (ticket ID), provider, suggested shape, max budget USD, expiry.
2. **Operator** appends to experiment_log:

```text
YYYY-MM-DDThh:mm:ssZ | cloud | CLOUD_APPROVAL | agent=operator | hash=none | metrics=budget_usd_max=<N>,provider=runpod|aws,purpose=<ticket>,expires=<ISO> | next=<ticket> | blocker=none
```

3. Agent verifies a **non-expired** `CLOUD_APPROVAL` with matching purpose before any spend API.
4. If missing/expired → **STOP** with `blocker=need_cloud_approval`.
5. On ticket done or expiry: tear down workers; log actual spend if known.

---

## Suggested shapes (non-binding)

| Use | Phase | Suggested shape | Notes |
| --- | --- | --- | --- |
| Budget/prompt sweeps | P3 | ~10–20 cheap GPU or CPU workers | KPIs unchanged |
| BoN + STaR harvest | P5A | Many small GPUs; \(N\) up to 32–64 | Local low-\(N\) OK if yield KPIs met |
| DoRA SFT / GRPO | P5B/C | Multi-GPU nodes; \(G\) 16–32 when VRAM allows | Smaller \(G\) OK if reward/token KPIs met |
| Soundness / dry-run | P0–P1, P6 | Spot CPU fleets | Final check on 4×L4-parity path |

```text
        Cloud Fleet (AWS Spot / RunPod)     [OPTIONAL + APPROVAL-GATED]
                         /                \
        Parallel rollouts / sweeps          DoRA / high-G GRPO
                         \                /
                          v              v
                 Artifacts → 4-bit / 4×L4-parity check
                          v
                 Kaggle ADK (merged adapter + skills)
```

---

## What may run where

| Allowed on cloud | Never on cloud as scored runtime |
| --- | --- |
| P3 ablation workers | Host ADK inference for official score |
| P5A rollouts / STaR | Shipping fleet code in submission.zip |
| P5B/C training | Proprietary teacher harvest without policy ALLOW |
| P6 CPU dry-run shards | Storing secrets in repo |

---

## Teardown checklist

- [ ] Workers terminated
- [ ] Volumes/snapshots deleted if temporary
- [ ] Spend logged (approx OK)
- [ ] Artifacts pulled to repo/object store outside `agent/`
- [ ] Adapter re-validated on **4-bit** competition weights before zip

---

## Parity rule

Every cloud-trained adapter must pass the project 4-bit serve check (non-noop outputs) and Phase 5/6 KPIs before inclusion in `submission.zip`.

---

## S3 dataset backup (operator-approved)

Private bucket for competition bug data (snapshots / graphs / embeddings / tasks) so local and cloud workers share one source.

| Item | Value |
| --- | --- |
| Bucket | `gemma4-dev-agent-data-650825122703` |
| Region | `us-east-1` |
| Expanded tree | `s3://gemma4-dev-agent-data-650825122703/competition/full/` |
| Zip (one-shot pull) | `s3://gemma4-dev-agent-data-650825122703/competition/gemma-4-developer-agent.zip` |
| Soundness CSV (resume) | `s3://…/results/soundness_smoke.csv`, `s3://…/results/soundness.csv` |
| Tools (scripts) | `s3://gemma4-dev-agent-data-650825122703/tools/` |
| Local source | `gemma4-agent/data/_full/` |

Public access blocked; SSE-S3 encryption on. Never put credentials or this URI inside `agent/`.

**Spot resume:** soundness writes CSV after each task and `--s3-uri` pushes to `results/`. New instance: `--resume --s3-uri s3://…/results/soundness.csv` pulls prior rows and skips done task_ids.

**Harness verify runbook (versioned):** `gemma4-agent/docs/harness_verify.md` — Starlette pin rule, image extras, recheck/quarantine commands. Always `aws s3 cp` the latest `tools/soundness_check.py` to workers before a run.

```bash
# Push (laptop → S3)
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
aws s3 sync gemma4-agent/data/_full/ \
  s3://gemma4-dev-agent-data-650825122703/competition/full/ \
  --region us-east-1

# Pull (cloud worker → disk)
aws s3 sync \
  s3://gemma4-dev-agent-data-650825122703/competition/full/ \
  ./data/ \
  --region us-east-1

# Or unzip from the archive object
aws s3 cp \
  s3://gemma4-dev-agent-data-650825122703/competition/gemma-4-developer-agent.zip \
  ./gemma-4-developer-agent.zip --region us-east-1
unzip -n gemma-4-developer-agent.zip -d ./data
```
