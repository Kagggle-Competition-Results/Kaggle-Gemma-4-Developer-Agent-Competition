# Harness verify notes (P1.1 soundness)

**Version:** `2026-10-06.3`  
**Owner ticket:** P1.1 → P1.2  
**Code:** `gemma4-agent/scripts/soundness_check.py`, `gemma4-agent/scripts/apply_quarantine.py`  
**Repo backup (image + pin):** `gemma4-agent/sandbox/` (`Dockerfile.gemma4`, `extras.txt`, `pin_deps.py`)  
**Canonical data:** `data/` or `data/_full/` (same layout as S3 `competition/full/`)

Future agents: read this before changing Docker verify or blaming the model for low usable %.

---

## AMI vs Docker (where fixes live)

| Layer | What | Do we put oracle fixes here? |
| --- | --- | --- |
| **AWS AMI** `ami-0794230e150561868` | Host OS: Docker daemon, AWS CLI, disk | **No** — optional later bake only |
| **Docker image** `gemma4-sandbox:base` | Pytest env + `extras.txt` packages | **Yes** — `sandbox/Dockerfile.gemma4` |
| **Per-task pin** `pin_deps.py` | Starlette/constraint-aware `/wheels` install | **Yes** — `sandbox/pin_deps.py` |
| **EC2 userdata** | Sync S3 data/tools, run soundness | Orchestration only |

**Always keep the Docker/pin recipe in git** under `gemma4-agent/sandbox/`. Push the same tree to `s3://…/tools/sandbox/` for workers. Do not rely on a hand-baked AMI for extras.

---

## What “usable” means

A task is **usable** only if:

1. **Empty patch fails** pytest (`empty_pass=false`)
2. **Oracle/gold patch passes** pytest (`oracle_pass=true`)

Gate KPI (Phase 1): ≥80% retained (≥100 / 129). Unusable IDs go to `configs/splits.json` → `quarantine` via `apply_quarantine.py`.

---

## Critical bug we hit (do not regress)

**Never** `pip install` every file under `/wheels/*.whl` before pytest.

Competition `wheels/` contains **many** `starlette-*.whl` versions (including `1.x`). Installing all of them makes the **newest** win. FastAPI snapshots pin e.g. `starlette>=0.40,<0.49`. Newest Starlette drops `on_startup` → mass `Router.__init__() unexpected keyword argument 'on_startup'` oracle fails.

Official `sandbox/setup.py` has the same class of bug: it **strips version constraints** then picks max wheel version per name.

### Correct approach (implemented)

In `soundness_check.py` → `run_verify`:

1. **`_pin_deps.py`** (injected): parse `pyproject.toml` / `requirements*.txt` **with** version pins; `pip install --no-index --find-links=/wheels <req>`; on failure retry `--no-deps`; then best-effort common transitives (`httpx`, `h11`, …) **without** unconstrained starlette.
2. **`SWE_PREBAKED=1`** + `sandbox/setup.py` for git exclude / pytest / editable only (skip broken max-version resolve).
3. Re-run pin after editable.
4. Apply gold (optional) + test patch → pytest.

Probe that must stay green after changes: **`fastapi_11194`** (was `on_startup` / multipart failure; now usable with pin + image extras).

---

## Sandbox image extras (`SANDBOX_EXTRA_PIP`)

Baked into `gemma4-sandbox:base` at build (not all exist in competition wheels):

| Package | Why |
| --- | --- |
| `typing_inspection` | pydantic 2.x |
| `inline_snapshot` | FastAPI oracle tests |
| `dirty_equals` | FastAPI oracle tests |
| `attrs` | some rich/tests |
| `python-multipart` | FastAPI form/file tests (`Form data requires python-multipart`) |
| `email-validator` | FastAPI email field tests |
| `ujson` / `orjson` | FastAPI UJSONResponse / ORJSONResponse tests |
| `greenlet` | sqlalchemy (not in `/wheels`; blocks sqlmodel install) |
| `pytest-httpbin` | requests oracle suite (194 errors without it) |

Rebuild after changing the tuple:

```bash
export SOUNDNESS_IMAGE=gemma4-sandbox:base
export SOUNDNESS_DOCKER_PLATFORM=linux/amd64   # required on Apple Silicon
python scripts/soundness_check.py --data data/_full --rebuild-image --task-id fastapi_11194 --out /tmp/probe.csv
```

---

## CLI (operators / cloud)

```bash
# Full set, Spot-safe checkpoint to S3
python scripts/soundness_check.py \
  --data data/_full \
  --out results/soundness.csv \
  --workers 8 --timeout 420 \
  --resume \
  --s3-uri s3://gemma4-dev-agent-data-650825122703/results/soundness.csv \
  --s3-every 1

# Recheck only some IDs (drop those rows from CSV first, or they are skipped by --resume)
python scripts/soundness_check.py --data data/_full --out results/soundness.csv \
  --resume --s3-uri s3://…/results/soundness.csv \
  --task-id fastapi_11194 --task-id fastapi_14609

# Quarantine non-usable into splits.json
python scripts/apply_quarantine.py \
  --soundness results/soundness.csv \
  --splits configs/splits.json
# dry-run first: add --dry-run
```

Env overrides: `SOUNDNESS_IMAGE`, `SOUNDNESS_DOCKER_PLATFORM`, `SOUNDNESS_S3_URI`.

---

## S3 layout (shared with cloud workers)

| Object | Purpose |
| --- | --- |
| `s3://gemma4-dev-agent-data-650825122703/competition/full/` | snapshots/wheels/docker/sandbox/tasks |
| `s3://…/tools/soundness_check.py` | **push script here before EC2/Spot runs** |
| `s3://…/tools/apply_quarantine.py` | quarantine helper |
| `s3://…/results/soundness.csv` | live resume checkpoint |
| `s3://…/results/soundness_smoke.csv` | smoke split |
| `s3://…/results/P11_DONE` | optional completion flag |

Spot userdata / rerun pattern: sync data → `aws s3 cp` tools script → run with `--resume --s3-uri` → spot metadata watcher flushes CSV on interrupt. Details: `docs/cloud-ops.md`.

---

## Failure taxonomy (how to triage)

| Symptom | Likely cause | Fix direction |
| --- | --- | --- |
| `unexpected keyword argument 'on_startup'` | Wrong Starlette (too new) | Ensure pin_deps; no all-wheels loop |
| `No module named 'inline_snapshot'` / `dirty_equals` | Missing image extra | `SANDBOX_EXTRA_PIP` + `--rebuild-image` |
| `Form data requires python-multipart` | Not in `/wheels` | Image extra `python-multipart` |
| `httpx` / `h11` missing | Transitive pin too new for wheel set | `--no-deps` fallback + EXTRA list |
| `empty_unexpected_pass` | Tests don’t catch bug | Quarantine |
| Oracle assert after green env | Real task / gold issue | Quarantine or inspect patch |

---

## Changelog

| Ver | Date | Change |
| --- | --- | --- |
| `2026-10-06.1` | 2026-10-06 | S3 `--resume` / per-task CSV sync; typing_inspection in image |
| `2026-10-06.2` | 2026-10-06 | Constraint-aware pin_deps; drop all-wheels preseed; multipart/email-validator/attrs extras; apply_quarantine.py |
| `2026-10-06.3` | 2026-10-06 | Image extras: ujson, orjson, greenlet, pytest-httpbin; pin EXTRA += sqlalchemy/sqlmodel |
