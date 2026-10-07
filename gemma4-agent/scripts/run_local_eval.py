#!/usr/bin/env python3
"""P1.3 / P2.6 — Local eval runner (tool-faithful scaffold).

Full 9-tool ADK parity requires HARNESS_README + swegemma pins after P0.1/P0.3.
This scaffold validates config, loads skills, writes KPI-shaped results JSON,
and records a blocker when the host harness is unavailable.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path


HOST_TOOLS = [
    "run_command",
    "read_file",
    "edit_file",
    "write_file",
    "get_status",
    "submit_patch",
    "get_code_neighbors",
    "get_code_subgraph",
    "search_similar_code",
]


def load_split(split_arg: str) -> list[str]:
    # format: configs/splits.json:smoke
    path_s, _, name = split_arg.partition(":")
    data = json.loads(Path(path_s).read_text())
    if name not in data:
        raise SystemExit(f"unknown split {name} in {path_s}")
    return list(data[name])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agent-dir", type=Path, default=Path("agent"))
    p.add_argument("--data", type=Path, default=Path("data"))
    p.add_argument("--split", default="configs/splits.json:smoke")
    p.add_argument("--out-dir", type=Path, default=Path("traces/smoke"))
    p.add_argument("--results", type=Path, default=Path("results/smoke.json"))
    args = p.parse_args()

    agent_yaml = args.agent_dir / "agent.yaml"
    skills_dir = args.agent_dir / "skills"
    if not agent_yaml.is_file():
        raise SystemExit(f"missing {agent_yaml}")

    skill_files = (
        sorted(skills_dir.glob("*/SKILL.md")) if skills_dir.is_dir() else []
    )
    tasks = load_split(args.split)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    harness_ready = (args.data / "tasks.jsonl").is_file()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_local_eval")
    t0 = time.time()

    # Skill smoke: ensure G11 eviction text present (host layout: skills/<name>/SKILL.md)
    patch_rules_path = skills_dir / "patch-rules" / "SKILL.md"
    patch_rules = patch_rules_path.read_text() if patch_rules_path.is_file() else ""
    g11_ok = "200 lines" in patch_rules

    result = {
        "run_id": run_id,
        "ticket": "P1.3" if not harness_ready else "P2.6",
        "phase": "P1" if not harness_ready else "P2",
        "config_sha256": None,
        "agent_dir_sha256": None,
        "split": args.split.split(":")[-1],
        "n": len(tasks),
        "resolved": 0,
        "resolve_rate": 0.0,
        "pass_at_1": 0.0,
        "mean_seconds": 0.0,
        "p95_seconds": 0.0,
        "extrapolated_hours_120": None,
        "failed_edit_rate": None,
        "loop_rate": None,
        "graph_hit_rate": None,
        "peak_tokens": None,
        "truncation_amnesia_events": 0,
        "hard_crash_rate": 0.0,
        "gate": None,
        "tags": {
            "host_tools": HOST_TOOLS,
            "skills_loaded": [s.name for s in skill_files],
            "g11_eviction_skill": g11_ok,
            "harness_ready": harness_ready,
            "blocker": None
            if harness_ready
            else "need_P0.1_rules_accept_and_P0.3_data_download",
        },
        "task_ids": tasks,
        "elapsed_s": round(time.time() - t0, 3),
    }

    args.results.parent.mkdir(parents=True, exist_ok=True)
    args.results.write_text(json.dumps(result, indent=2) + "\n")

    # One stub trace explaining status
    manual = args.out_dir / "_harness_status.jsonl"
    with manual.open("w") as f:
        f.write(
            json.dumps(
                {
                    "ts": 0.0,
                    "type": "result",
                    "task_id": "_status",
                    "resolved": False,
                    "seconds": result["elapsed_s"],
                    "error": result["tags"]["blocker"],
                    "failure_tags": ["harness"] if not harness_ready else [],
                }
            )
            + "\n"
        )

    print(json.dumps({"results": str(args.results), "harness_ready": harness_ready, "g11_ok": g11_ok}, indent=2))
    return 0 if g11_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
