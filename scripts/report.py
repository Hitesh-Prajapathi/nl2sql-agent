"""Regenerate the README results tables from ``results/bird_minidev_*.json``.

Replaces everything between ``<!-- RESULTS:START -->`` and ``<!-- RESULTS:END -->``.
Only COMPLETE (500-question) runs appear; subsets and smoke runs are excluded by design.

Usage: ``uv run python scripts/report.py [--readme README.md] [--results-dir results] [--check]``
``--check`` exits 1 if the README is out of date (for CI) instead of writing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nl2sql.eval.results import (
    README_END,
    README_START,
    render_readme_tables,
    update_readme,
)

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--readme", type=Path, default=ROOT / "README.md")
    ap.add_argument("--results-dir", type=Path, default=ROOT / "results")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    results = []
    for path in sorted(args.results_dir.glob("bird_minidev_*.json")):
        data = json.loads(path.read_text())
        if isinstance(data, dict) and "summary" in data and "label" in data:
            results.append(data)  # skips e.g. bird_minidev_gold_check_*.json
    block = render_readme_tables(results)

    text = args.readme.read_text()
    if README_START not in text or README_END not in text:
        print(f"{args.readme}: missing {README_START} / {README_END} markers", file=sys.stderr)
        return 2
    if args.check:
        head, rest = text.split(README_START, 1)
        _, tail = rest.split(README_END, 1)
        current = f"{head}{README_START}\n{block}{README_END}{tail}"
        if current != text:
            print("README results tables are stale: run `make report`", file=sys.stderr)
            return 1
        return 0
    changed = update_readme(args.readme, block)
    print(f"{'updated' if changed else 'unchanged'}: {args.readme} ({len(results)} result files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
