"""Draw the fixed, difficulty-stratified BIRD Mini-Dev subset used for headline runs.

The Groq free tier caps gpt-oss-120b at 200K tokens/day (~70 questions), so headline runs use a
150-question subset instead of all 500. Allocation is proportional to the full set's difficulty
split (148/250/102 -> 44/75/31), sampled with a fixed seed so the subset is reproducible.

Usage: uv run python scripts/make_subset.py [--n 150] [--seed 20261114]
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

DATASET = Path("data/bird_minidev/raw/minidev/MINIDEV/mini_dev_postgresql.json")
OUT = Path("config/bird_subset_150.txt")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--seed", type=int, default=20261114)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    items = json.loads(DATASET.read_text())
    by_diff: dict[str, list[int]] = {}
    for it in items:
        by_diff.setdefault(it["difficulty"], []).append(int(it["question_id"]))

    total = len(items)
    alloc = {d: round(args.n * len(ids) / total) for d, ids in by_diff.items()}
    # Fix rounding so the allocation sums to exactly n (adjust the largest stratum).
    largest = max(alloc, key=lambda d: len(by_diff[d]))
    alloc[largest] += args.n - sum(alloc.values())

    rng = random.Random(args.seed)  # noqa: S311 - reproducible sampling, not security
    chosen: list[int] = []
    for d in sorted(by_diff):
        chosen += rng.sample(sorted(by_diff[d]), alloc[d])
    chosen.sort()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# BIRD Mini-Dev stratified subset: n={args.n}, seed={args.seed}, "
        f"allocation={dict(sorted(alloc.items()))}\n"
    )
    args.out.write_text(header + "\n".join(map(str, chosen)) + "\n")
    print(f"wrote {args.out}: {len(chosen)} ids, allocation {dict(sorted(alloc.items()))}")


if __name__ == "__main__":
    main()
