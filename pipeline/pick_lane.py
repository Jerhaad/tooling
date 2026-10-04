#!/usr/bin/env python3
"""Choose the lane a pipeline phase should run on.

``pick`` returns ``(profile, reason)``, with the reason on stderr; the first
rule to fire wins: minimax refusing in the last 30 minutes, too many
concurrent minimax sessions, then MiniMax's own report of the plan's remaining
weekly and 5-hour quota. When that report is unavailable the phase runs on
minimax, since the first rule still catches a real quota wall.
"""
import argparse
import http.client
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_DB = Path.home() / ".hermes/profiles/minimax/state.db"

# How far back the first two rules look. Named so the rule names on
# stderr and the rule logic cannot drift apart.
FALLBACK_LOOKBACK_S = 30 * 60
CONCURRENCY_LOOKBACK_S = 30 * 60

# Overridable so tests can point at a local stub.
DEFAULT_REMAINS_URL = "https://api.minimax.io/v1/token_plan/remains"
REMAINS_URL_ENV = "PICK_LANE_REMAINS_URL"
REMAINS_MODEL = "general"
REMAINS_TIMEOUT_S = 10.0
DEFAULT_MIN_REMAINING_PERCENT = 20
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


def api_key() -> str | None:
    """MINIMAX_API_KEY from the environment, else from the minimax profile's
    .env. Never logged."""
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    env = Path.home() / ".hermes/profiles/minimax/.env"
    if env.exists():
        for line in env.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "MINIMAX_API_KEY" and value.strip():
                return value.strip().strip("\"'")
    return None


def quota(url: str, key: str | None) -> tuple[float, float] | str:
    """(weekly, 5-hour) remaining percent for the general quota, or the reason
    the endpoint gave no answer. The reason never carries the key."""
    if not key:
        return "no MINIMAX_API_KEY"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=REMAINS_TIMEOUT_S) as resp:
            body = json.load(resp)
    except (OSError, ValueError, http.client.HTTPException) as e:
        return f"endpoint unreachable: {type(e).__name__}"
    if not isinstance(body, dict):
        return "response is not a JSON object"
    status = (body.get("base_resp") or {}).get("status_code")
    if status != 0:
        return f"status_code={status}"
    for row in body.get("model_remains") or []:
        if row.get("model_name") == REMAINS_MODEL:
            try:
                return (float(row["current_weekly_remaining_percent"]),
                        float(row["current_interval_remaining_percent"]))
            except (KeyError, TypeError, ValueError):
                break
    return f"no {REMAINS_MODEL!r} quota in the response"


def pick(db: Path, *, preferred: str, fallback: str,
         max_concurrent: int, min_remaining_percent: float,
         now: float | None = None) -> tuple[str, str]:
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

        answer = quota(os.environ.get(REMAINS_URL_ENV) or DEFAULT_REMAINS_URL, api_key())
        if isinstance(answer, str):
            return (preferred,
                    f"pick-lane: quota check skipped ({answer}); using {preferred}")
        weekly_pct, interval_pct = answer

        below: list[str] = []
        if weekly_pct < min_remaining_percent:
            below.append(f"{weekly_pct:.0f}% weekly")
        if interval_pct < min_remaining_percent:
            below.append(f"{interval_pct:.0f}% 5-hour")
        if below:
            return (fallback,
                    f"pick-lane: {', '.join(below)} below {min_remaining_percent:.0f}%; "
                    f"using {fallback}")

        return (preferred,
                f"pick-lane: {weekly_pct:.0f}% weekly, {interval_pct:.0f}% 5-hour; "
                f"using {preferred}")
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fallback", default="default",
                    help="profile to use when metered out")
    ap.add_argument("--preferred", default="minimax",
                    help="profile to use when not metered out")
    ap.add_argument("--max-concurrent", type=int,
                    default=DEFAULT_MAX_CONCURRENT,
                    help="max active minimax main sessions before falling over")
    ap.add_argument("--min-remaining-percent", type=float,
                    default=DEFAULT_MIN_REMAINING_PERCENT,
                    help="fall back when weekly or 5-hour percent is below this")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args()

    profile, reason = pick(
        args.db,
        preferred=args.preferred,
        fallback=args.fallback,
        max_concurrent=args.max_concurrent,
        min_remaining_percent=args.min_remaining_percent,
    )
    print(profile)
    print(reason, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
