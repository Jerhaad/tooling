#!/usr/bin/env python3
"""Choose the lane a pipeline phase should run on.

MiniMax meters *requests* against a rolling window. Hitting the ceiling is not
loud: hermes fails over to the local model and answers anyway, so a throttled
run looks like a healthy one.

Usage lives in the minimax profile's own state.db, not the root profile's.
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = Path.home() / ".hermes/profiles/minimax/state.db"


def requests_in_window(db, window_s):
    """Requests attributed to minimax whose session was last active in-window.

    api_call_count is per session and last_seen is that session's final
    activity, so a session straddling the boundary is counted whole. That
    overcounts rather than under, which is the right direction for a guardrail.
    """
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cutoff = time.time() - window_s
    row = con.execute(
        "select coalesce(sum(api_call_count), 0) from session_model_usage "
        "where billing_provider = 'minimax' and last_seen >= ?", (cutoff,)
    ).fetchone()
    return int(row[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fallback", default="default", help="profile to use when metered out")
    ap.add_argument("--preferred", default="minimax")
    ap.add_argument("--limit", type=int, default=1500, help="requests per window")
    ap.add_argument("--window", type=int, default=5 * 3600)
    ap.add_argument("--threshold", type=float, default=0.8)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    used = requests_in_window(args.db, args.window)
    if used is None:
        # No usage record means no way to know how close the ceiling is. The
        # local lane is free and unmetered, so unknown resolves to it.
        print(args.fallback)
        print(f"pick-lane: no usage db at {args.db}; using {args.fallback}", file=sys.stderr)
        return

    ceiling = args.limit * args.threshold
    if used >= ceiling:
        print(args.fallback)
        print(f"pick-lane: {used}/{args.limit} requests in the last "
              f"{args.window // 3600}h is over {args.threshold:.0%}; using {args.fallback}",
              file=sys.stderr)
    else:
        print(args.preferred)
        print(f"pick-lane: {used}/{args.limit} requests in the last "
              f"{args.window // 3600}h; using {args.preferred}", file=sys.stderr)


if __name__ == "__main__":
    main()
