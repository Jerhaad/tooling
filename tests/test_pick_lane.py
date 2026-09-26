#!/usr/bin/env python3
"""Pin that pick_lane reads the three rules out of a real session_model_usage.

Every case builds a sqlite db with the columns the production query actually
names, runs the script as a subprocess against --db, and checks both the
chosen profile and the reason line on stderr. The reason line matters
because the caller decides what to log; a picker that sends work local for
the wrong reason silently breaks the next operator's runbook.
"""
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PICK = REPO_ROOT / "pipeline" / "pick_lane.py"

# Columns the production query names, exactly as the real minimax state.db
# names them. A drift between this fixture and pipeline/pick_lane.py reads
# as "picker does not work" rather than "fixture is wrong", so keep them
# in lockstep.
SCHEMA = """
create table session_model_usage (
    session_id        text primary key,
    model             text,
    billing_provider  text,
    task              text,
    api_call_count    integer,
    input_tokens      integer,
    output_tokens     integer,
    cache_read_tokens integer,
    cache_write_tokens integer,
    first_seen        real,
    last_seen         real
);
create table sessions (
    id        text primary key,
    ended_at  real
);
"""


class TmpDb:
    """Build a fresh sqlite db with the columns the production query expects,
    drop it on exit. ``row(...)`` and ``session(...)`` add one each."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "usage.db"
        con = sqlite3.connect(self.path)
        con.executescript(SCHEMA)
        con.commit()
        con.close()

    def session(self, *, session_id: str, ended_at: float | None = None) -> None:
        """Set or replace a sessions row's ``ended_at``; ``None`` means open."""
        con = sqlite3.connect(self.path)
        con.execute(
            "insert into sessions (id, ended_at) values (?, ?) "
            "on conflict(id) do update set ended_at = excluded.ended_at",
            (session_id, ended_at),
        )
        con.commit()
        con.close()

    def row(self, *, session_id: str, model: str = "m",
            task: str | None = "",
            billing_provider: str | None = "minimax",
            api_call_count: int = 1,
            input_tokens: int = 0, output_tokens: int = 0,
            cache_read_tokens: int = 0, cache_write_tokens: int = 0,
            minutes_ago: float = 0.0) -> None:
        """Insert one session_model_usage row whose last_seen is
        ``minutes_ago`` from now. Also creates the matching sessions row
        (ended_at NULL) so the concurrency join finds it."""
        now = time.time()
        last_seen = now - minutes_ago * 60
        first_seen = last_seen - 1.0
        con = sqlite3.connect(self.path)
        con.execute(
            "insert or ignore into sessions (id, ended_at) values (?, null)",
            (session_id,),
        )
        con.execute(
            "insert into session_model_usage "
            "(session_id, model, billing_provider, task, api_call_count, "
            " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
            " first_seen, last_seen) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, model, billing_provider, task, api_call_count,
             input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
             first_seen, last_seen),
        )
        con.commit()
        con.close()

    def close(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> "TmpDb":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def run_pick(db_path: Path, *extra: str) -> tuple[str, str, int]:
    """Run pick_lane.py with --db db_path, return (stdout, stderr, rc)."""
    proc = subprocess.run(
        ["python3", str(PICK), "--db", str(db_path), *extra],
        capture_output=True, text=True)
    return proc.stdout.strip(), proc.stderr.strip(), proc.returncode


def main() -> int:
    failures: list[str] = []

    def expect(label: str, *, db_path: Path, want_profile: str,
               want_reason_substr: str | None = None,
               extra_args: tuple[str, ...] = ()) -> None:
        profile, reason, rc = run_pick(db_path, *extra_args)
        if rc != 0:
            failures.append(f"{label}: pick_lane exited {rc}: {reason}")
            return
        if profile != want_profile:
            failures.append(
                f"{label}: chose {profile!r}, want {want_profile!r}; "
                f"reason was {reason!r}")
            return
        if want_reason_substr is not None and want_reason_substr not in reason:
            failures.append(
                f"{label}: profile matched but reason {reason!r} does not "
                f"contain {want_reason_substr!r}")

    # 1. An empty db picks minimax.
    with TmpDb() as empty:
        expect("empty db picks minimax",
               db_path=empty.path, want_profile="minimax")

    # 2. A missing db picks the fallback.
    missing = Path(tempfile.mkdtemp()) / "does-not-exist.db"
    expect("missing db picks fallback",
           db_path=missing, want_profile="default",
           extra_args=("--fallback", "default"),
           want_reason_substr="no usage db")

    # 3. Weekly tokens over the threshold pick the fallback; the same total
    #    8 days ago picks minimax; rows billed to 'custom' do not count;
    #    auxiliary rows billed to minimax do.
    over = TmpDb()
    over.row(session_id="big-recent",
             input_tokens=1_000_000_000, output_tokens=100_000_000,
             minutes_ago=60)
    expect("weekly tokens over threshold pick fallback",
           db_path=over.path, want_profile="default",
           extra_args=("--fallback", "default"),
           want_reason_substr="tokens in the last 7d")
    over.close()

    stale = TmpDb()
    stale.row(session_id="big-old",
              input_tokens=1_000_000_000, output_tokens=100_000_000,
              minutes_ago=8 * 24 * 60)
    expect("weekly tokens 8 days old are not counted",
           db_path=stale.path, want_profile="minimax")
    stale.close()

    custom_billed = TmpDb()
    custom_billed.row(session_id="custom-big",
                      input_tokens=1_000_000_000, output_tokens=100_000_000,
                      billing_provider="custom", minutes_ago=60)
    expect("custom-billed tokens do not count toward weekly total",
           db_path=custom_billed.path, want_profile="minimax")
    custom_billed.close()

    # Auxiliary rows billed to minimax DO count toward the weekly total:
    # their tokens were spent on the plan. This case fails if the query
    # ever gains a task filter (e.g. "and (task is null or task = '')").
    aux_minimax = TmpDb()
    aux_minimax.row(session_id="compress-heavy", task="compression",
                    input_tokens=1_000_000_000, minutes_ago=60)
    expect("auxiliary minimax-billed row counts toward the weekly total",
           db_path=aux_minimax.path, want_profile="default",
           extra_args=("--fallback", "default"),
           want_reason_substr="tokens in the last 7d")
    aux_minimax.close()

    # 4. Concurrency counts open sessions only. Four open sessions in the
    #    last 30 minutes trip the cap; three do not. A session that ended
    #    one minute ago is no longer concurrent even if last_seen is fresh.
    four = TmpDb()
    for i in range(4):
        four.row(session_id=f"open-{i}", minutes_ago=i)
    expect("four open sessions trip concurrency",
           db_path=four.path, want_profile="default",
           extra_args=("--fallback", "default"),
           want_reason_substr="concurrent minimax session")
    four.close()

    three = TmpDb()
    for i in range(3):
        three.row(session_id=f"open-{i}", minutes_ago=i)
    expect("three open sessions do not trip concurrency",
           db_path=three.path, want_profile="minimax")
    three.close()

    ended_recently = TmpDb()
    for i in range(4):
        ended_recently.row(session_id=f"closed-{i}", minutes_ago=i)
    now = time.time()
    for i in range(4):
        ended_recently.session(session_id=f"closed-{i}",
                               ended_at=now - 60)  # ended one minute ago
    expect("session ended one minute ago does not count toward concurrency",
           db_path=ended_recently.path, want_profile="minimax")
    ended_recently.close()

    crashed_open = TmpDb()
    for i in range(4):
        crashed_open.row(session_id=f"stale-{i}", minutes_ago=40 + i)
    expect("open session with last_seen 40m ago does not count as concurrent",
           db_path=crashed_open.path, want_profile="minimax")
    crashed_open.close()

    # 5. One main row billed to 'custom' 10 minutes ago picks the fallback;
    #    the same row 40 minutes ago does not.
    recent_custom = TmpDb()
    recent_custom.row(session_id="fell-back", minutes_ago=10,
                      billing_provider="custom")
    expect("recent custom-billed main row trips observed fallback",
           db_path=recent_custom.path, want_profile="default",
           extra_args=("--fallback", "default"),
           want_reason_substr="observed fallback")
    recent_custom.close()

    old_custom = TmpDb()
    old_custom.row(session_id="fell-back", minutes_ago=40,
                   billing_provider="custom")
    expect("40-minute-old custom-billed row does not trip observed fallback",
           db_path=old_custom.path, want_profile="minimax")
    old_custom.close()

    # 6. Auxiliary rows (task='title_generation') are billed elsewhere and
    #    never count toward any rule.
    aux_empty = TmpDb()
    aux_empty.row(session_id="aux-empty", task="title_generation",
                  billing_provider="", minutes_ago=1)
    expect("auxiliary row billed to '' does not trip observed fallback",
           db_path=aux_empty.path, want_profile="minimax")
    aux_empty.close()

    aux_custom = TmpDb()
    aux_custom.row(session_id="aux-custom", task="title_generation",
                   billing_provider="custom", minutes_ago=1)
    expect("auxiliary row billed to 'custom' does not trip observed fallback",
           db_path=aux_custom.path, want_profile="minimax")
    aux_custom.close()

    aux_concurrency = TmpDb()
    for i in range(4):
        aux_concurrency.row(session_id=f"aux-{i}", task="title_generation",
                            minutes_ago=i)
    aux_concurrency.row(session_id="main-0", task="", minutes_ago=0)
    expect("auxiliary rows do not count toward concurrency",
           db_path=aux_concurrency.path, want_profile="minimax")
    aux_concurrency.close()

    # 7. Cache-read tokens count toward the weekly total.
    cache = TmpDb()
    cache.row(session_id="cache-heavy", cache_read_tokens=1_200_000_000,
              minutes_ago=60)
    expect("cache-read tokens count toward the weekly total",
           db_path=cache.path, want_profile="default",
           extra_args=("--fallback", "default"),
           want_reason_substr="tokens in the last 7d")
    cache.close()

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS pick_lane honours the three new rules end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
