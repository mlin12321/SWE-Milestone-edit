#!/usr/bin/env python3
"""One table for a director trial: milestones resolved, cost, whether teams were used.

    python scripts/summarize_director_eval.py --trial NAME [--ledger F] [--out summary.json]
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", required=True, help="trial name, with or without its _NNN suffix")
    parser.add_argument("--data-root", default=os.environ.get("SWE_MILESTONE_DATA_ROOT"))
    parser.add_argument("--ledger")
    parser.add_argument("--out")
    args = parser.parse_args()
    rows, repos = [], []
    for repo in sorted(Path(args.data_root).iterdir()):
        trials = sorted((repo / "e2e_trial").glob(f"{args.trial}*")) if (repo / "e2e_trial").is_dir() else []
        if not trials:
            continue
        trial = trials[-1]
        result = load(trial / "e2e_workspace" / "director" / "result.json")
        usage = load(trial / "e2e_workspace" / "director" / "team_usage.json")
        summary = usage.get("summary") or {}
        repos.append(
            {
                "repo": repo.name, "trial": trial.name, "outcome": result.get("outcome"),
                "used_teams": usage.get("used_teams"), "sub_teams": summary.get("sub_teams_started"),
                "proposals": summary.get("proposals"), "team_tool_calls": summary.get("team_tool_calls"),
                "agents": summary.get("agents"), "cost": result.get("cost"),
                "cost_estimated": (usage.get("usage") or {}).get("estimated"),
                "confinement": result.get("confinement"),
                "web_lookups": usage.get("web_lookups"),
                "sessions": result.get("sessions"),
            }
        )
        for name, one in (load(trial / "evaluation" / "summary.json").get("results") or {}).items():
            tests = one.get("test_summary") or {}
            new_need = tests.get("fail_to_pass_required", 0) + tests.get("none_to_pass_required", 0)
            new_got = tests.get("fail_to_pass_achieved", 0) + tests.get("none_to_pass_achieved", 0)
            rows.append(
                {
                    "repo": repo.name, "milestone": name, "eval_status": one.get("eval_status"),
                    "new_tests": f"{new_got}/{new_need}",
                    "kept_tests": f"{tests.get('pass_to_pass_achieved', 0)}/{tests.get('pass_to_pass_required', 0)}",
                }
            )
    print(f"{'repo':<40} {'milestone':<22} {'status':<10} {'new tests':<12} kept tests")
    for row in rows:
        print(f"{row['repo'][:39]:<40} {row['milestone']:<22} {str(row['eval_status']):<10} {row['new_tests']:<12} {row['kept_tests']}")
    for repo in repos:
        print(json.dumps({k: v for k, v in repo.items() if k != "sessions"}))
    total = {
        "trial": args.trial, "milestones_evaluated": len(rows),
        "passed": sum(row["eval_status"] == "passed" for row in rows),
        "cost": round(sum(repo["cost"] or 0 for repo in repos), 2),
        "ledger": load(Path(args.ledger)) if args.ledger else None,
    }
    print(json.dumps({k: v for k, v in total.items() if k != "ledger"}))
    if args.out:
        Path(args.out).write_text(json.dumps({"total": total, "repos": repos, "milestones": rows}, indent=2) + "\n")


if __name__ == "__main__":
    main()
