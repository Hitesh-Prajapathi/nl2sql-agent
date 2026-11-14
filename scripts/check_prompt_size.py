"""Offline prompt-size check (no LLM, no database): ``make prompt-size VARIANT=full_small``.

Builds the planner request for all 500 BIRD Mini-Dev questions under a variant, estimates its
tokens (prompt + the output tokens the call reserves against the per-minute limit) and exits
non-zero if any question exceeds the cap. Without a cap (``full`` / ``naive``) it only reports.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nl2sql.config import get_settings
from nl2sql.eval.dataset import load_questions
from nl2sql.eval.promptsize import compute_prompt_sizes, size_stats
from nl2sql.eval.variants import SMALL_VARIANTS, VARIANTS
from nl2sql.schema.capped import DEFAULT_TOTAL_CAP_TOKENS
from nl2sql.schema.store import load_profile, profile_path


def main(argv: list[str] | None = None) -> int:
    s = get_settings()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--variant", default="full_small", choices=VARIANTS)
    ap.add_argument("--cap", type=int, default=DEFAULT_TOTAL_CAP_TOKENS)
    ap.add_argument("--max-output", type=int, default=s.planner_max_tokens)
    ap.add_argument("--no-evidence", action="store_true")
    ap.add_argument("--data-dir", type=Path, default=Path(s.bird_data_dir))
    ap.add_argument("--profile-dir", type=Path, default=Path(s.profile_dir))
    ap.add_argument("--json", action="store_true", help="print the stats as JSON")
    args = ap.parse_args(argv)

    questions = load_questions(args.data_dir)
    profiles = {
        d: load_profile(profile_path(d, args.profile_dir))
        for d in sorted({q.db_id for q in questions})
    }
    sizes = compute_prompt_sizes(
        args.variant,
        questions,
        profiles,
        use_evidence=not args.no_evidence,
        total_cap_tokens=args.cap,
        max_output_tokens=args.max_output,
    )
    capped = args.variant in SMALL_VARIANTS
    st = size_stats(sizes, args.cap if capped else None)
    if args.json:
        print(json.dumps(st, indent=1))
    else:
        print(f"variant={args.variant} questions={st['n']} cap={st['cap_total_tokens']}")
        print(
            f"total (prompt + {args.max_output} output) tokens: max={st['max_total_tokens']} "
            f"mean={st['mean_total_tokens']} p95={st['p95_total_tokens']}"
        )
        print(
            f"prompt tokens: max={st['max_prompt_tokens']} mean={st['mean_prompt_tokens']} "
            f"p95={st['p95_prompt_tokens']}; worst-case correction request total="
            f"{st['max_correction_total_tokens']}"
        )
    bad = [s_ for s_ in sizes if capped and s_.total_tokens > args.cap]
    bad_corr = [s_ for s_ in sizes if capped and s_.correction_total_tokens > args.cap]
    if bad or bad_corr:
        print(f"FAIL: {len(bad)} planner and {len(bad_corr)} correction requests over the cap")
        return 1
    if capped:
        print(f"OK: all {st['n']} requests are within {args.cap} tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
