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
5. If two searches for the same token return no hits, treat that as a localization result, not a prompt to search more broadly. Pivot to likely files from the stack, issue text, or nearby module names.
6. Avoid whole-tree scans like `find . -name "*.py" | xargs grep ...` unless you are using them once as a fallback. Repeating them for the same token is a loop.
7. After a real verify delta like `200` vs `422`, allow at most one semantic search and one shell search. Then stop navigating and read or edit the current best source hit.
8. Once you already have a real source hit, do not invent adjacent symbol names and search them. Read or edit the hit you already found.
9. Once you know the exact file for a symbol/class, do not search guessed alternate files for that same symbol. Re-read or edit the known file, or verify.
