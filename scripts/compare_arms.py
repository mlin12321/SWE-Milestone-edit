#!/usr/bin/env python3
"""One table over the arms of a comparison: the same milestones, different agents.

    python scripts/compare_arms.py --repo go-zero \\
        claude-code=claude-code_claude-sonnet-5-5_seed20261127_n10 \\
        director-solo=director-solo_claude-sonnet-5-5_seed20261127_n10 \\
        director-teams=director-teams_claude-sonnet-5-5_seed20261127_n10 \\
        --out comparison.json

Each argument is `label=trial name` (the latest trial of that name is read).
The headline is the milestones resolved; the rest says what it cost and why.
A milestone an arm never submitted counts as not resolved.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def baseline_cost(trial: Path) -> float | None:
    """What a trial without the director cost: the sum over its invocations."""
    stats = load(trial / "agent_stats.json")
    if stats.get("summary", {}).get("total_cost_usd") is not None:
        return stats["summary"]["total_cost_usd"]
    total, found = 0.0, False
    try:
        lines = (trial / "log" / "agent_stdout.txt").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            one = json.loads(line)
        except ValueError:
            continue
        if isinstance(one, dict) and "total_cost_usd" in one:
            total, found = total + (one["total_cost_usd"] or 0), True
    return total if found else None


def minutes_to_last_submission(trial: Path, results: dict) -> float | None:
    """From the trial's start to the evaluation of its last submission."""
    start = load(trial / "trial_metadata.json").get("start_time")
    stamps = []
    for one in results.values():
        try:
            stamps.append(datetime.strptime(one["timestamp"], "%a %b %d %H:%M:%S %Y"))
        except (KeyError, ValueError):
            pass
    if not start or not stamps:
        return None
    return round((max(stamps) - datetime.fromisoformat(start)).total_seconds() / 60, 1)


def arm(label: str, trial: Path) -> dict:
    # One id per line: the milestones this trial was limited to.
    try:
        asked = [line.strip() for line in (trial / "milestone_selection.txt").read_text().splitlines()
                 if line.strip() and not line.startswith("#")]
    except OSError:
        asked = []
    results = load(trial / "evaluation" / "summary.json").get("results") or {}
    director = trial / "e2e_workspace" / "director"
    result, usage = load(director / "result.json"), load(director / "team_usage.json")
    new_need = new_got = kept_need = kept_got = unbuilt = 0
    milestones = {}
    for name, one in results.items():
        tests = one.get("test_summary") or {}
        need = tests.get("fail_to_pass_required", 0) + tests.get("none_to_pass_required", 0)
        got = tests.get("fail_to_pass_achieved", 0) + tests.get("none_to_pass_achieved", 0)
        new_need, new_got = new_need + need, new_got + got
        kept_need += tests.get("pass_to_pass_required", 0)
        kept_got += tests.get("pass_to_pass_achieved", 0)
        detail = load(trial / "evaluation" / name / "evaluation_result.json")
        unbuilt += bool(detail.get("end_compile_error"))
        milestones[name] = one.get("eval_status")
    resolved = sum(status == "passed" for status in milestones.values())
    is_director = bool(result)
    cost = result.get("cost") if is_director else baseline_cost(trial)
    sessions = result.get("sessions") or []
    reviews = usage.get("reviews") or []
    return {
        "arm": label,
        "trial": trial.name,
        "milestones_asked": len(asked) or None,
        "submitted": len(results),
        "resolved": resolved,
        "new_tests": f"{new_got}/{new_need}",
        "new_tests_pct": round(100 * new_got / new_need, 1) if new_need else None,
        "existing_tests_broken": kept_need - kept_got,
        "submissions_that_do_not_build": unbuilt,
        "minutes_to_last_submission": minutes_to_last_submission(trial, results),
        "cost": round(cost, 2) if cost is not None else None,
        "cost_per_resolved": round(cost / resolved, 2) if cost and resolved else None,
        "outcome": result.get("outcome") if is_director else None,
        "sessions_landed": sum(one.get("outcome") == "landed" for one in sessions) if is_director else None,
        "sessions_not_landed": sum(one.get("outcome") != "landed" for one in sessions) if is_director else None,
        "used_teams": usage.get("used_teams") if is_director else None,
        "sub_teams": len(usage.get("sub_teams") or []) if is_director else None,
        "review_rounds": sum(one.get("rounds") or 0 for one in reviews) if is_director else None,
        "review_findings": sum(one.get("findings") or 0 for one in reviews) if is_director else None,
        "times_told_to_go_on": result.get("times_the_ic_was_told_to_go_on") if is_director else None,
        "escalations_answered": len(result.get("escalations_answered_for_the_human") or []) if is_director else None,
        "web_lookups": len(usage.get("web_lookups") or []) if is_director else None,
        "milestones": milestones,
    }


ROWS = [
    ("milestones resolved", "resolved"), ("  of asked", "milestones_asked"), ("  submitted", "submitted"),
    ("new tests passing", "new_tests"), ("  percent", "new_tests_pct"),
    ("existing tests broken", "existing_tests_broken"),
    ("submissions that do not build", "submissions_that_do_not_build"),
    ("minutes to last submission", "minutes_to_last_submission"),
    ("cost ($)", "cost"), ("cost per resolved ($)", "cost_per_resolved"),
    ("how the run ended", "outcome"),
    ("sessions landed", "sessions_landed"), ("sessions not landed", "sessions_not_landed"),
    ("used teams", "used_teams"), ("sub-teams", "sub_teams"),
    ("review rounds", "review_rounds"), ("review findings", "review_findings"),
    ("times told to go on", "times_told_to_go_on"), ("escalations answered", "escalations_answered"),
    ("web lookups", "web_lookups"),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("arms", nargs="+", metavar="LABEL=TRIAL")
    parser.add_argument("--repo", required=True, help="part of the repo's folder name")
    parser.add_argument("--data-root", default=os.environ.get("SWE_MILESTONE_DATA_ROOT"))
    parser.add_argument("--out")
    args = parser.parse_args()
    repos = [one for one in sorted(Path(args.data_root).iterdir()) if args.repo in one.name and (one / "e2e_trial").is_dir()]
    if len(repos) != 1:
        raise SystemExit(f"--repo {args.repo!r} names {len(repos)} repos")
    arms = []
    for spec in args.arms:
        label, _, name = spec.partition("=")
        trials = sorted((repos[0] / "e2e_trial").glob(f"{name}*"))
        if not trials:
            raise SystemExit(f"no trial {name!r} in {repos[0].name}")
        arms.append(arm(label, trials[-1]))
    width = max([len(one["arm"]) for one in arms] + [len("not submitted")]) + 2
    print(f"{repos[0].name}")
    print(f"{'':<32}" + "".join(f"{one['arm']:>{width}}" for one in arms))
    for title, key in ROWS:
        print(f"{title:<32}" + "".join(f"{'-' if one[key] is None else str(one[key]):>{width}}" for one in arms))
    names = sorted({name for one in arms for name in one["milestones"]})
    print()
    for name in names:
        print(f"{name:<32}" + "".join(f"{one['milestones'].get(name, 'not submitted'):>{width}}" for one in arms))
    if args.out:
        Path(args.out).write_text(json.dumps({"repo": repos[0].name, "arms": arms}, indent=2) + "\n")


if __name__ == "__main__":
    main()
