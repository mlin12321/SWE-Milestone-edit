#!/usr/bin/env python3
"""Draw a fixed random sample of milestones, the same for a given seed.

    python scripts/sample_milestones.py --seed 20261127 --num 30 --out sample.json

A trial works through one repo's milestones in dependency order, so a sample
is a set of repos, each with the first N of its milestones: the repos are
shuffled by the seed and taken in that order until `--num` milestones are
allotted. With `--local-images-only` (the default) only repos whose base image
is already pulled are drawn from; the others would need `scripts/pull_images.sh`.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
from pathlib import Path


def milestone_count(repo: Path) -> int:
    selected = repo / "selected_milestone_ids.txt"
    if selected.exists():
        return len([line for line in selected.read_text().splitlines() if line.strip()])
    return max(len((repo / "milestones.csv").read_text().splitlines()) - 1, 0)


def has_images(repo: str) -> bool:
    listed = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}", f"swe-milestone/{repo.lower()}__base*"],
        capture_output=True, text=True,
    )
    return bool(listed.stdout.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--num", type=int, required=True, help="milestones to allot at most")
    parser.add_argument("--data-root", default=os.environ.get("SWE_MILESTONE_DATA_ROOT"))
    parser.add_argument("--repos", default="", help="only repos whose name contains one of these, comma separated")
    parser.add_argument("--all-repos", action="store_true", help="also draw repos whose images are not pulled yet")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(args.data_root)
    repos = sorted(p.name for p in root.iterdir() if (p / "milestones.csv").exists())
    wanted = [w for w in args.repos.split(",") if w]
    if wanted:
        repos = [r for r in repos if any(w in r for w in wanted)]
    pool = repos if args.all_repos else [r for r in repos if has_images(r)]
    order = pool[:]
    random.Random(args.seed).shuffle(order)
    left, sample = args.num, {}
    for repo in order:
        if left <= 0:
            break
        sample[repo] = min(milestone_count(root / repo), left)
        left -= sample[repo]
    Path(args.out).write_text(
        json.dumps({"seed": args.seed, "num": args.num, "pool": pool, "milestones": sample}, indent=2) + "\n"
    )
    print(f"{sum(sample.values())} milestones in {len(sample)} repo(s) of {len(pool)} -> {args.out}")
    for repo, count in sample.items():
        print(f"  {repo}: first {count}")


if __name__ == "__main__":
    main()
