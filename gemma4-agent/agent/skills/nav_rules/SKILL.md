# Navigation rules (enable with graph tools in P3.5)

1. Prefer symbol → `get_code_neighbors` / `get_code_subgraph` / `search_similar_code` (symbol-keyed; not free-text issue embedding).
2. If graph tools return empty/error, fall back to `run_command` (`rg`, `python -c` AST) + ranged `read_file`.
3. Never dump whole files &gt;200 lines (G11). Read only the ranges you need.
4. Use graph/AST before flat full-text reads when a symbol is known.
