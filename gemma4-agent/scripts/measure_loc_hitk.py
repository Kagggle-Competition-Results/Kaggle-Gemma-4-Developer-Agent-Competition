#!/usr/bin/env python3
"""P3.5 — Lexical Hit@5 + graph-ranking diagnostic (Roman TF-IDF style proxy).

Reads tasks + code graphs under data/. Writes results/loc_hitk.json.
Does not use holdout splits (G4).
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path

TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")
PATCH_FILE_RE = re.compile(r"^[+-]{3} [ab]/(.+)$", re.MULTILINE)


def load_split(split_arg: str) -> list[str]:
    path_s, _, name = split_arg.partition(":")
    data = json.loads(Path(path_s).read_text())
    if name not in data:
        raise SystemExit(f"unknown split {name} in {path_s}")
    return list(data[name])


def gold_files_from_patch(patch: str) -> set[str]:
    files = set(PATCH_FILE_RE.findall(patch or ""))
    return {f.strip() for f in files if f.strip() and not f.endswith(".md")}


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "")]


def symbol_to_file(symbol: str) -> str | None:
    parts = symbol.split(".")
    if len(parts) < 2:
        return None
    root = parts[0]
    if root in ("rich", "fastapi", "httpx", "requests", "starlette", "tests"):
        if parts[1] == "__init__":
            return f"{root}/__init__.py"
        return f"{root}/{parts[1]}.py"
    if root in ("examples", "benchmarks", "scripts", "docs"):
        return f"{root}/{parts[1]}.py" if len(parts) > 1 else f"{root}.py"
    return f"{root}/{parts[1]}.py"


def graph_path(data_dir: Path, task: dict) -> Path | None:
    bc = task["base_commit"][:40]
    repo = task.get("repo", "").split("/")[-1] or task["instance_id"].split("_")[0]
    gdir = data_dir / "graphs"
    hits = list(gdir.glob(f"{repo}_{bc}.json"))
    if hits:
        return hits[0]
    hits = list(gdir.glob(f"*{bc}*"))
    return hits[0] if hits else None


def build_file_corpus(graph: dict) -> dict[str, str]:
    """Map repo-relative file path -> concatenated symbol text for TF-IDF."""
    buckets: dict[str, list[str]] = {}
    for node in graph.get("nodes") or []:
        name = node.get("name") or node.get("id") or ""
        text = node.get("text") or ""
        fpath = symbol_to_file(name)
        if not fpath:
            continue
        buckets.setdefault(fpath, []).append(f"{name} {text}")
    return {k: "\n".join(v) for k, v in buckets.items()}


def tfidf_rank(query: str, corpus: dict[str, str], k: int = 5) -> list[tuple[str, float]]:
    q_tokens = tokenize(query)
    if not q_tokens or not corpus:
        return []
    q_counts = Counter(q_tokens)
    doc_tokens = {path: Counter(tokenize(body)) for path, body in corpus.items()}
    df: Counter[str] = Counter()
    for counts in doc_tokens.values():
        for tok in counts:
            df[tok] += 1
    n_docs = len(corpus)

    def score(path: str) -> float:
        counts = doc_tokens[path]
        s = 0.0
        for tok, qf in q_counts.items():
            if tok not in counts:
                continue
            idf = math.log((1 + n_docs) / (1 + df[tok])) + 1.0
            s += qf * counts[tok] * idf
        return s

    ranked = sorted(((p, score(p)) for p in corpus), key=lambda x: -x[1])
    return ranked[:k]


def hit_at_k(ranked_files: list[str], gold: set[str], k: int = 5) -> bool:
    if not gold:
        return False
    top = ranked_files[:k]
    return any(g in top for g in gold)


def graph_symbol_rank(query: str, graph: dict, k: int = 5) -> list[tuple[str, float]]:
    """Rank symbols by query token overlap (proxy for graph tool retrieval)."""
    q = set(tokenize(query))
    if not q:
        return []
    scored: list[tuple[str, float]] = []
    for node in graph.get("nodes") or []:
        name = node.get("name") or node.get("id") or ""
        text = node.get("text") or ""
        tokens = set(tokenize(f"{name} {text}"))
        if not tokens:
            continue
        overlap = len(q & tokens)
        if overlap:
            scored.append((name, float(overlap)))
    scored.sort(key=lambda x: -x[1])
    return scored[:k]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, default=Path("data/_full"))
    p.add_argument("--split", default="configs/splits.json:smoke")
    p.add_argument("--out", type=Path, default=Path("results/loc_hitk.json"))
    p.add_argument("--k", type=int, default=5)
    args = p.parse_args()

    tasks_path = args.data / "tasks.jsonl"
    if not tasks_path.is_file():
        raise SystemExit(f"missing {tasks_path}")

    want = set(load_split(args.split))
    tasks = []
    for line in tasks_path.read_text().splitlines():
        if not line.strip():
            continue
        t = json.loads(line)
        if t["instance_id"] in want:
            tasks.append(t)

    per_task = []
    lex_hits = 0
    graph_hits = 0
    graph_nonempty = 0

    for task in tasks:
        gold = gold_files_from_patch(task.get("patch", ""))
        query = (task.get("problem_statement") or "") + "\n" + (task.get("hints_text") or "")
        gpath = graph_path(args.data, task)
        row = {
            "task_id": task["instance_id"],
            "gold_files": sorted(gold),
            "graph_path": str(gpath) if gpath else None,
        }
        if not gpath or not gpath.is_file():
            row["error"] = "missing_graph"
            per_task.append(row)
            continue

        graph = json.loads(gpath.read_text())
        n_nodes = len(graph.get("nodes") or [])
        row["n_nodes"] = n_nodes
        if n_nodes == 0:
            row["error"] = "empty_graph"
            per_task.append(row)
            continue

        graph_nonempty += 1
        corpus = build_file_corpus(graph)
        lex_rank = tfidf_rank(query, corpus, k=args.k)
        lex_files = [f for f, _ in lex_rank]
        row["lexical_top5"] = [{"file": f, "score": s} for f, s in lex_rank]
        lex_ok = hit_at_k(lex_files, gold, args.k)
        row["lexical_hit_at_5"] = lex_ok
        if lex_ok:
            lex_hits += 1

        sym_rank = graph_symbol_rank(query, graph, k=args.k)
        sym_files = []
        for sym, sc in sym_rank:
            fp = symbol_to_file(sym)
            if fp:
                sym_files.append(fp)
        row["graph_symbol_top5"] = [
            {"symbol": s, "file": symbol_to_file(s), "score": sc} for s, sc in sym_rank
        ]
        graph_ok = hit_at_k(sym_files, gold, args.k)
        row["graph_file_hit_at_5"] = graph_ok
        if graph_ok:
            graph_hits += 1

        per_task.append(row)

    n = len(tasks)
    n_g = graph_nonempty or 1
    out = {
        "ticket": "P3.5",
        "split": args.split.split(":")[-1],
        "k": args.k,
        "n_tasks": n,
        "n_graph_nonempty": graph_nonempty,
        "lexical_hit_at_5_rate": round(lex_hits / n, 4) if n else 0.0,
        "graph_file_hit_at_5_rate": round(graph_hits / n_g, 4) if graph_nonempty else 0.0,
        "graph_hit_rate": round(graph_hits / n_g, 4) if graph_nonempty else 0.0,
        "graph_kill_threshold": 0.60,
        "recommendation": "grep_first"
        if (graph_hits / n_g if graph_nonempty else 0) < 0.60
        else "graph_first",
        "per_task": per_task,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in out if k != "per_task"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
