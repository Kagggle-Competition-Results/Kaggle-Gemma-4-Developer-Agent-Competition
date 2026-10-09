# P5.1 train script inventory (2026-10-09, post-STaR r1)

Spec: [`docs/agent-dev-spec.md`](../../docs/agent-dev-spec.md) §5.
**Ops:** [`harvest_runpod.md`](./harvest_runpod.md).

| Script | Spec CLI | Present |
| --- | --- | --- |
| `rejection_sample.py` | `--tasks --n --agent-dir --out traces/bon/` | **PRESENT** |
| `star_correct.py` | `--tasks --max-retries 1 --error-tail-lines 20` | **PRESENT** |
| `ensure_subprocess_host_deps.py` | `--data` | **PRESENT** |
| `filter_bon_keepers.py` | P5.2 loops/scratch/peak28k/dedupe | **PRESENT** |
| `render_trajectories.py` | `--in --out data_train/sft.jsonl --mask-tool-results` | **PRESENT** |
| `train_qlora.py` | DoRA SFT | **MISSING** |
| `train_grpo.py` / `merge_adapters.py` | later | **MISSING** |

## Results

| Run | Yield | Note |
| --- | --- | --- |
| P5.1a-r4 | 0.75 (6/8) | host deps + oracle |
| **P5.1a-r5c** | **0.9375 (60/64)** | N=8 @0.2; torchvision fix |
| **P5.2** | **57 clean / 8 tasks** | drop 9 loops; `results/p51_clean/` |
| **P5.5** | **57 rows** | `data_train/sft.jsonl` masked |
| **P5.1a-r6** | **~0.13 yield** | broadened 16 train_pool IDs × N=8 @0.2 |
| **P5.5 merged** | **65 rows / 9 tasks** | r4+r5c+r6 merged into `data_train/sft.jsonl` |
| **P5.1b-star-r1** | **1/9 recoveries** | 1 clean non-duplicate row; 5 misses blocked by missing deps |

**Next:** add missing subprocess deps (`inline_snapshot`, `dirty_equals`) before more STaR/BoN; merge the held-out STaR row after env fix review. No holdout.
