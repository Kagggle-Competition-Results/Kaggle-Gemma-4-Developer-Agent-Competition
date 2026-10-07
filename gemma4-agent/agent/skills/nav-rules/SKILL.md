---
name: nav-rules
description: Graph-first then shell fallback navigation with G11 ranged reads.
---

# Navigation rules

P3.5 diagnostic logged graph Hit@5 below 60% (smoke 0.40 / dev 0.29), but **grep-first smoke regressed** Pass@1 0.20 → 0.10 — keep graph-first until a better nav ablation wins.

1. Prefer symbol → `get_code_neighbors` / `get_code_subgraph` / `search_similar_code` (symbol-keyed; not free-text issue embedding).
2. If graph tools return empty/error, fall back to `run_command` (`rg`, `python -c` AST) + ranged `read_file`.
3. Never dump whole files over 200 lines (G11). Read only the ranges you need.
4. Use graph/AST before flat full-text reads when a symbol is known; if no symbol yet, `rg` first is fine.
