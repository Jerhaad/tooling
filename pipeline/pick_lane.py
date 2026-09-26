#!/usr/bin/env python3
"""Choose the lane a pipeline phase should run on.

The minimax profile meters weekly tokens and concurrent main sessions, and
fails over silently to a local model when it refuses. ``pick`` returns
``(profile, reason)``; whichever rule fires first wins, with the reason on
stderr. Usage lives in the minimax profile's own state.db, not the root
profile's.
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = Path.home() / ".hermes/profiles/minimax/state.db"

# How far back each rule looks. Named so the rule names on stderr and the rule
# logic cannot drift apart.
FALLBACK_LOOKBACK_S = 30 * 60
CONCURRENCY_LOOKBACK_S = 30 * 60
WEEKLY_LOOKBACK_S = 7 * 24 * 3600

# Max publishes roughly 5.05B tokens / month; the 5-hour and weekly window
# sizes it meters against are not published. Weekly budget defaults to that
# monthly figure divided by 4.35 weeks.
DEFAULT_WEEKLY_TOKENS = 1_160_000_000
DEFAULT_MAX_CONCURRENT = 4


def _open(db: Path) -> sqlite3.Connection | None:
    """Open the usage db read-only, or return None if it does not exist."""
    if not db.exists():
        return None
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def observed_fallback(con: sqlite3.Connection, now: float) -> int:
    """Main-conversation rows billed to a provider other than minimax in the
    last 30 minutes: NULL, empty, or any other value -- the signal is
    "minimax refused and something else answered"."""
    cutoff = now - FALLBACK_LOOKBACK_S
    row = con.execute(
        "select count(*) from session_model_usage "
        "where (task is null or task = '') "
        "and (billing_provider is null or billing_provider != 'minimax') "
        "and last_seen >= ?",
        (cutoff,),
    ).fetchone()
    return int(row[0])


def concurrent_minimax(con: sqlite3.Connection, now: float) -> int:
    """Distinct main-conversation sessions billed to minimax whose session
    is still open (sessions.ended_at IS NULL) and whose last_seen is within
    the last 30 minutes. The last_seen cap ages out sessions that crashed
    without being closed; without it, an open session that has stopped
    answering stays counted as concurrent forever."""
    cutoff = now - CONCURRENCY_LOOKBACK_S
    row = con.execute(
        "select count(distinct u.session_id) from session_model_usage u "
        "join sessions s on s.id = u.session_id "
        "where (u.task is null or u.task = '') "
        "and u.billing_provider = 'minimax' "
        "and s.ended_at is null "
        "and u.last_seen >= ?",
        (cutoff,),
    ).fetchone()
    return int(row[0])


def weekly_tokens(con: sqlite3.Connection, now: float) -> int:
    """Tokens over every minimax-billed row in the last 7 days, auxiliary
    tasks included because they spend the plan. Cache reads count too,
    since the plan's metering is unpublished and overcounting is safe."""
    cutoff = now - WEEKLY_LOOKBACK_S
    row = con.execute(
        "select coalesce(sum(input_tokens), 0) "
        " + coalesce(sum(output_tokens), 0) "
        " + coalesce(sum(cache_read_tokens), 0) "
        " + coalesce(sum(cache_write_tokens), 0) "
        "from session_model_usage "
        "where billing_provider = 'minimax' "
        "and last_seen >= ?",
        (cutoff,),
    ).fetchone()
    return int(row[0])


def pick(db: Path, *, preferred: str, fallback: str,
         weekly_tokens_budget: int, max_concurrent: int,
         threshold: float, now: float | None = None) -> tuple[str, str]:
    """Decide the lane. Order: observed fallback (minimax refusing now),
    concurrency (the cap that actually trips), weekly tokens (slowest)."""
    now = time.time() if now is None else now
    con = _open(db)
    if con is None:
        return fallback, (f"no usage db at {db}; using {fallback}")

    try:
        fell = observed_fallback(con, now)
        if fell > 0:
            return (fallback,
                    f"pick-lane: observed fallback {fell} time(s) in the last "
                    f"{FALLBACK_LOOKBACK_S // 60}m; using {fallback}")

        active = concurrent_minimax(con, now)
        if active >= max_concurrent:
            return (fallback,
                    f"pick-lane: {active} concurrent minimax session(s) in the "
                    f"last {CONCURRENCY_LOOKBACK_S // 60}m >= {max_concurrent}; "
                    f"using {fallback}")

        used = weekly_tokens(con, now)
        ceiling = weekly_tokens_budget * threshold
        if used >= ceiling:
            return (fallback,
                    f"pick-lane: {used} tokens in the last 7d >= "
                    f"{weekly_tokens_budget} * {threshold:.2f} = {ceiling:.0f}; "
                    f"using {fallback}")

        return (preferred,
                f"pick-lane: {used} tokens in the last 7d, "
                f"{active} concurrent; using {preferred}")
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fallback", default="default",
                    help="profile to use when metered out")
    ap.add_argument("--preferred", default="minimax",
                    help="profile to use when not metered out")
    ap.add_argument("--weekly-tokens", type=int,
                    default=DEFAULT_WEEKLY_TOKENS,
                    help="weekly token budget; threshold is applied to this")
    ap.add_argument("--max-concurrent", type=int,
                    default=DEFAULT_MAX_CONCURRENT,
                    help="max active minimax main sessions before falling over")
    ap.add_argument("--threshold", type=float, default=0.8,
                    help="fraction of --weekly-tokens that triggers fallback")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    profile, reason = pick(
        args.db,
        preferred=args.preferred,
        fallback=args.fallback,
        weekly_tokens_budget=args.weekly_tokens,
        max_concurrent=args.max_concurrent,
        threshold=args.threshold,
    )
    print(profile)
    print(reason, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
