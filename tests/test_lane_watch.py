#!/usr/bin/env python3
"""Pin lane-watch's behaviour against a scratch state.db.

The script reads state.db, and tests that exercise it have to write a
database shaped like the one hermes actually uses. The columns it reads
(sessions.cwd, sessions.started_at, sessions.ended_at, messages.role,
messages.content, messages.tool_calls, messages.tool_name, messages.id,
messages.session_id) come from the real profile's state.db, which the
schema probe below reads once at test start, read-only.

Run directly: `python3 tests/test_lane_watch.py`. No test runner -- stdlib
subprocess + a scratch HERMES_HOME under TMPDIR is enough. LANE_WATCH_POLL
is forced to a fraction of a second here so the poll loop is not the
bottleneck of the suite.
"""
import os
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LANE_WATCH = REPO_ROOT / "bin" / "lane-watch"
# LANE_WATCH_POLL = 0.05s keeps the suite under a second per follow case
# while still letting the script see a row that the test inserted after
# it started.
POLL = "0.05"
# Generous because the follow cases race the script: the script must
# observe the row the test inserted, and a slow host can hide the race.
# Two seconds at the test's poll rate is more than enough wall clock for
# any reasonable run.
TIMEOUT = 8


def build_db(db_path: Path) -> None:
    """A state.db with the columns lane-watch reads from hermes's schema.
    messages.id must be an INTEGER PRIMARY KEY: lane-watch pages by id."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path))
    try:
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, "
                  "cwd TEXT, started_at REAL NOT NULL, ended_at REAL)")
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT, "
                  "tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL)")
    finally:
        c.close()


def open_db(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path))


def insert_session(con: sqlite3.Connection, sid: str, cwd: str,
                   started_at: float, ended_at=None) -> None:
    """Insert a sessions row. The columns populated are the only ones
    lane-watch reads; source is required by the schema, so we fill it
    with a placeholder."""
    con.execute(
        "INSERT INTO sessions (id, source, cwd, started_at, ended_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (sid, "test", cwd, started_at, ended_at))
    con.commit()


def insert_message(con: sqlite3.Connection, sid: str, role: str,
                   content: str, tool_calls=None, tool_name=None) -> int:
    """Insert a messages row. Returns the new id. timestamp is NOT NULL
    in the real schema, so we fill it."""
    cur = con.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, role, content, tool_calls, tool_name, time.time()))
    con.commit()
    return int(cur.lastrowid)


def run_lane_watch(args: list[str], env_extra: dict, timeout: int = TIMEOUT
                  ) -> subprocess.CompletedProcess:
    env = {**os.environ, "LANE_WATCH_POLL": POLL, **env_extra}
    return subprocess.run([str(LANE_WATCH), *args], capture_output=True,
                          text=True, env=env, timeout=timeout)


def wait_for(proc, fd, needle, timeout: int) -> str:
    """Read proc.stdout non-blocking until `needle` appears in the
    accumulated output, or `timeout` seconds elapse. Returns whatever
    was read (may be incomplete on timeout, which the caller checks)."""
    assert proc.stdout is not None
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rlist, _, _ = select.select([fd], [], [], 0.1)
        if not rlist:
            if needle in buf:
                return buf
            continue
        chunk = os.read(fd, 4096).decode("utf-8", errors="replace")
        if not chunk:
            return buf
        buf += chunk
        if needle in buf:
            return buf
    return buf


def kill_and_drain(proc, timeout: int = TIMEOUT
                  ) -> tuple[str, str]:
    """Kill proc if alive, then read everything still in its pipes.
    The script enters an indefinite wait loop once --since sees its
    session end, so the only way to finish a case is to stop the
    child and collect whatever it had already written."""
    if proc.poll() is None:
        proc.kill()
        proc.wait()
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return stdout, stderr


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        home = tmp / "hermes-home"
        # A worktree path the test references both as the lane's cwd and
        # as the fragment lane-watch is asked to match on.
        wt = tmp / "wt-12345"
        wt.mkdir()

        # 1. Missing database: clear error, non-zero, no traceback.
        env_extra = {"HERMES_HOME": str(home)}
        r = run_lane_watch([str(wt)], env_extra)
        if r.returncode == 0:
            failures.append("missing db: exit 0")
        if "Traceback" in r.stderr:
            failures.append(f"missing db: traceback in stderr: {r.stderr!r}")
        if "no database at" not in r.stderr:
            failures.append(f"missing db: clear message missing: {r.stderr!r}")

        # 2. No matching session: non-zero, named.
        root_db = home / "state.db"
        build_db(root_db)
        r = run_lane_watch([str(wt)], env_extra)
        if r.returncode == 0:
            failures.append("no match: exit 0")
        if "no" not in r.stderr or "session" not in r.stderr:
            failures.append(f"no match: message missing: {r.stderr!r}")
        if str(wt) not in r.stderr:
            failures.append(f"no match: fragment {wt!r} not in stderr: {r.stderr!r}")

        # 3. --all prints existing user/assistant tool call/tool result and
        # exits on its own once ended_at is set.
        con = open_db(root_db)
        sid = "s-all"
        insert_session(con, sid, str(wt), 1000.0, ended_at=1001.0)
        insert_message(con, sid, "user", "hello from a user")
        # A tool call: JSON in tool_calls, the function name and command
        # are what the describe() branch must show.
        insert_message(con, sid, "assistant", "thinking...",
                       tool_calls=(
                           '[{"function": {"name": "terminal", '
                           '"arguments": "{\\"command\\": \\"ls -la\\"}"}}]'))
        insert_message(con, sid, "tool", "drwxr-xr-x  ...",
                       tool_name="terminal")
        con.close()

        r = run_lane_watch([str(wt), "--all"], env_extra)
        if r.returncode != 0:
            failures.append(f"--all: exit {r.returncode}, stderr={r.stderr!r}")
        out = r.stdout
        if "user" not in out or "hello from a user" not in out:
            failures.append(f"--all: user message not printed: {out!r}")
        if "→ terminal" not in out or "ls -la" not in out:
            failures.append(f"--all: tool call command not shown: {out!r}")
        if "← terminal" not in out or "drwxr-xr-x" not in out:
            failures.append(f"--all: tool result not shown: {out!r}")
        if "session ended" not in out:
            failures.append(f"--all: did not see 'session ended': {out!r}")

        # 4. Without --all, it prints only messages added after it starts.
        # Insert the seeded messages first (so the script's `last` will be
        # > 0 if it ignored the rule), then set the marker to 0, and run.
        con = open_db(root_db)
        sid2 = "s-tail"
        insert_session(con, sid2, str(wt), 2000.0)  # not ended yet
        insert_message(con, sid2, "user", "seeded, must NOT print")
        insert_message(con, sid2, "assistant", "also seeded")
        con.close()

        # Start the script; once it has the follow banner, drop a new
        # message in and set ended_at so it exits. The 0.05s poll
        # dominates this loop.
        proc = subprocess.Popen([str(LANE_WATCH), str(wt)], env={
            **os.environ, "HERMES_HOME": str(home),
            "LANE_WATCH_POLL": POLL},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            # Wait for the script to print the "following" line.
            deadline = time.time() + TIMEOUT
            saw_follow = False
            while time.time() < deadline:
                line = proc.stdout.readline()
                if "following" in line:
                    saw_follow = True
                    break
            if not saw_follow:
                failures.append("tail: never saw 'following' banner")
            else:
                # Insert the live message and end the session.
                con = open_db(root_db)
                insert_message(con, sid2, "user", "LIVE: this must print")
                insert_message(con, sid2, "tool", "LIVE: tool result",
                               tool_name="terminal")
                con.execute(
                    "UPDATE sessions SET ended_at = ? WHERE id = ?",
                    (9999.0, sid2))
                con.commit()
                con.close()
            try:
                stdout, stderr = proc.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                failures.append(f"tail: script hung, stderr={stderr!r}")
            if "LIVE: this must print" not in stdout:
                failures.append(
                    f"tail: live message not printed: stdout={stdout!r} "
                    f"stderr={stderr!r}")
            if "seeded, must NOT print" in stdout:
                failures.append(
                    f"tail: seeded message leaked: stdout={stdout!r}")
            if "also seeded" in stdout:
                failures.append(
                    f"tail: seeded assistant leaked: stdout={stdout!r}")
            if "LIVE: tool result" not in stdout:
                failures.append(
                    f"tail: live tool result not printed: stdout={stdout!r}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # 5. Newest session wins when two share a cwd fragment. Mark the
        # newer session ended so the script can exit; the older one is
        # never read because the newest-wins query picks s-new first.
        other = tmp / "wt-99"
        other.mkdir()
        con = open_db(root_db)
        insert_session(con, "s-old", str(wt), 5000.0)
        insert_message(con, "s-old", "user", "OLD: must NOT print")
        insert_session(con, "s-new", str(wt), 9000.0, ended_at=9001.0)
        insert_message(con, "s-new", "user", "NEW: must print")
        con.close()
        r = run_lane_watch([str(wt), "--all"], env_extra, timeout=TIMEOUT)
        if "NEW: must print" not in r.stdout:
            failures.append(
                f"newest: did not print newer session: {r.stdout!r}")
        if "OLD: must NOT print" in r.stdout:
            failures.append(
                f"newest: printed older session: {r.stdout!r}")

        # 6. Profile database: --profile reads profiles/P/state.db.
        prof_dir = home / "profiles" / "lane"
        prof_db = prof_dir / "state.db"
        build_db(prof_db)
        con = open_db(prof_db)
        insert_session(con, "s-prof", str(wt), 100.0, ended_at=101.0)
        insert_message(con, "s-prof", "user", "profile-mode message")
        con.close()
        # Same fragment, only in the profile database. With --profile, the
        # root database would be silent.
        r = run_lane_watch([str(wt), "--profile", "lane", "--all"],
                           env_extra, timeout=TIMEOUT)
        if "profile-mode message" not in r.stdout:
            failures.append(
                f"--profile: profile db not read: stdout={r.stdout!r} "
                f"stderr={r.stderr!r}")

        # 7. No --profile reads the root database. Same fragment as the
        # profile test, but the root database does not have a session for
        # this fragment; the profile-only row is invisible without
        # --profile. Use a fragment that exists ONLY in the profile
        # database.
        prof_only = tmp / "wt-only-in-profile"
        prof_only.mkdir()
        con = open_db(prof_db)
        insert_session(con, "s-prof-only", str(prof_only), 200.0,
                       ended_at=201.0)
        insert_message(con, "s-prof-only", "user", "prof-only-message")
        con.close()
        r = run_lane_watch([str(prof_only)], env_extra, timeout=TIMEOUT)
        if r.returncode == 0:
            failures.append(
                "no --profile: root db saw a profile-only session")
        if "prof-only-message" in r.stdout:
            failures.append(
                "no --profile: profile db content leaked into root read")

        # 8. Profile flag combined with the no-match path: clear error
        # names the profile.
        empty_home = tmp / "empty-home"
        empty_home.mkdir()
        r = run_lane_watch([str(wt), "--profile", "ghost"],
                           {"HERMES_HOME": str(empty_home)},
                           timeout=TIMEOUT)
        if r.returncode == 0:
            failures.append("missing profile db: exit 0")
        if "no database at" not in r.stderr or "ghost" not in r.stderr:
            failures.append(
                f"missing profile db: stderr missing path/profile: {r.stderr!r}")

        # 9. --since: an older, already-ended session for the same cwd
        # must not win over a newer one. Insert the older session first
        # so it is the only row matching the fragment at script start,
        # then launch lane-watch with --since. While it polls, insert
        # the newer session and end it -- the script must then follow
        # only the newer one, then enter its indefinite wait loop.
        since_home = tmp / "since-home"
        since_db = since_home / "state.db"
        build_db(since_db)
        since_epoch = time.time()
        # A safe gap between the old session's started_at and the epoch:
        # the script only considers sessions with started_at >= epoch, so
        # the old row must be strictly before. Five seconds is far enough
        # that any clock skew between this Python and the subprocess
        # cannot cross it.
        con = open_db(since_db)
        insert_session(con, "s-since-old", str(wt), since_epoch - 5.0,
                       ended_at=since_epoch - 4.0)
        insert_message(con, "s-since-old", "user", "OLD: must NOT print")
        con.commit()
        con.close()

        # Spawn the script with --since and let it settle into its wait
        # loop, then drop the new session in. The 0.05s poll dominates
        # this race, so even a busy host crosses it inside TIMEOUT.
        proc = subprocess.Popen(
            [str(LANE_WATCH), str(wt), "--all", "--since", str(since_epoch)],
            env={**os.environ, "HERMES_HOME": str(since_home),
                 "LANE_WATCH_POLL": POLL},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        buf = ""
        try:
            assert proc.stdout is not None
            # Read whatever the child emits, non-blocking, until a quiet
            # window has passed: the script entered its wait loop and
            # stopped producing output. A quiet window of 1s beats any
            # poll interval the test configures (default 0.05s), and
            # any reasonable "wait for the next line" worth its budget.
            quiet_for = 1.0
            deadline = time.time() + TIMEOUT
            inserted = False
            last_line_time = time.time()
            stdout_fd = proc.stdout.fileno()
            while time.time() < deadline:
                rlist, _, _ = select.select(
                    [stdout_fd], [], [], 0.1)
                if not rlist:
                    if time.time() - last_line_time >= quiet_for:
                        break
                    continue
                chunk = os.read(stdout_fd, 4096).decode(
                    "utf-8", errors="replace")
                if not chunk:
                    break
                last_line_time = time.time()
                buf += chunk
                # Detect that the script entered its wait loop: it
                # produced no "following" line yet (the old session is
                # filtered out by --since, and the new one is not yet
                # in the database). If we see "following" before the
                # insert, the --since clause is broken -- it picked
                # the older session.
                if "following" in buf and not inserted:
                    failures.append(
                        f"--since: followed old session before new one "
                        f"appeared: {buf!r}")
                    break
            else:
                failures.append(
                    "--since: script never reached its poll loop "
                    "(no output for the whole wait period)")
            if not inserted and not failures:
                con = open_db(since_db)
                insert_session(con, "s-since-new", str(wt), since_epoch + 1.0,
                               ended_at=since_epoch + 2.0)
                insert_message(con, "s-since-new", "user",
                               "NEW-since: must print")
                con.commit()
                con.close()
                inserted = True
                # The script now follows the new session, prints it,
                # sees ended_at, prints "session ended", and enters
                # the indefinite wait loop. communicate() is expected
                # to time out -- that is the behaviour the rest of
                # the suite pins. Wait for the lines we expect, then
                # stop the child.
                buf += wait_for(proc, stdout_fd, "waiting", TIMEOUT)
                stdout, stderr = kill_and_drain(proc)
                buf += stdout
                if stderr.strip():
                    # The script is silent on the happy path; any
                    # stderr here is a clue the test is mis-wired.
                    failures.append(
                        f"--since: unexpected stderr: {stderr!r}")
            if "NEW-since: must print" not in buf:
                failures.append(
                    f"--since: newer session not printed: buf={buf!r}")
            if "OLD: must NOT print" in buf:
                failures.append(
                    f"--since: older session leaked through: buf={buf!r}")
            if "session ended" not in buf:
                failures.append(
                    f"--since: never saw 'session ended': buf={buf!r}")
            if "waiting" not in buf:
                failures.append(
                    f"--since: never entered waiting state: buf={buf!r}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # 10. --since: when the same session reopens (ended_at cleared,
        # or new messages beyond the cursor), lane-watch resumes from
        # the cursor it already has. The pre-reopen messages must not be
        # printed twice; the message appended after the reopen must be
        # printed exactly once.
        reopen_home = tmp / "reopen-home"
        reopen_db = reopen_home / "state.db"
        build_db(reopen_db)
        con = open_db(reopen_db)
        since_r = time.time()
        sid_r = "s-reopen"
        insert_session(con, sid_r, str(wt), since_r + 1.0)
        # Pre-reopen messages whose exact-occurrence counts pin the
        # "no earlier message printed twice" rule: the cursor the
        # watcher built before the reopen must be reused, not
        # restarted, so the assistant message does not appear again
        # after the reopen.
        insert_message(con, sid_r, "user", "REOPEN-PRESEED-A: sentinel")
        insert_message(con, sid_r, "assistant",
                       "REOPEN-PRESEED-B: sentinel")
        con.close()

        proc = subprocess.Popen(
            [str(LANE_WATCH), str(wt), "--all", "--since", str(since_r)],
            env={**os.environ, "HERMES_HOME": str(reopen_home),
                 "LANE_WATCH_POLL": POLL},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        buf = ""
        try:
            assert proc.stdout is not None
            stdout_fd = proc.stdout.fileno()
            # Wait for both pre-seeded messages to be printed (the
            # second is the high-water mark the cursor will land on).
            buf += wait_for(proc, stdout_fd, "REOPEN-PRESEED-B", TIMEOUT)
            if "REOPEN-PRESEED-A" not in buf:
                failures.append(
                    f"reopen: REOPEN-PRESEED-A not printed: buf={buf!r}")
            if "REOPEN-PRESEED-B" not in buf:
                failures.append(
                    f"reopen: REOPEN-PRESEED-B not printed: buf={buf!r}")
            # Mark the session ended and wait for the watcher to
            # acknowledge it.
            con = open_db(reopen_db)
            con.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (since_r + 2.0, sid_r))
            con.commit()
            con.close()
            buf += wait_for(proc, stdout_fd, "waiting", TIMEOUT)
            if "session ended" not in buf:
                failures.append(
                    f"reopen: 'session ended' not seen: buf={buf!r}")
            # Reopen: clear ended_at and append a new message. The
            # watcher's cursor is the high-water mark from before the
            # end, so only the new message should be printed.
            con = open_db(reopen_db)
            con.execute(
                "UPDATE sessions SET ended_at = NULL WHERE id = ?",
                (sid_r,))
            insert_message(con, sid_r, "user",
                           "REOPEN-LIVE: sentinel")
            con.commit()
            con.close()
            buf += wait_for(proc, stdout_fd, "REOPEN-LIVE", TIMEOUT)
            if "REOPEN-LIVE" not in buf:
                failures.append(
                    f"reopen: live message not printed: buf={buf!r}")
            # End the session again so the watcher is in a known
            # state, then stop the child. The script's indefinite
            # wait loop means the only way to finish is to kill it.
            con = open_db(reopen_db)
            con.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (since_r + 3.0, sid_r))
            con.commit()
            con.close()
            stdout, stderr = kill_and_drain(proc)
            buf += stdout
            if stderr.strip():
                failures.append(
                    f"reopen: unexpected stderr: {stderr!r}")
            # Exact-occurrence counts: pre-reopen messages must
            # appear once (not twice), live message must appear
            # once.
            for sentinel in ("REOPEN-PRESEED-A", "REOPEN-PRESEED-B",
                             "REOPEN-LIVE"):
                count = buf.count(sentinel)
                if count != 1:
                    failures.append(
                        f"reopen: {sentinel} appeared {count} times, "
                        f"expected 1: buf={buf!r}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # 11. --since: a strictly newer session after the first one
        # ends is followed from its start, not from the first
        # session's cursor.
        newer_home = tmp / "newer-home"
        newer_db = newer_home / "state.db"
        build_db(newer_db)
        con = open_db(newer_db)
        since_n = time.time()
        sid_n1 = "s-newer-first"
        sid_n2 = "s-newer-second"
        insert_session(con, sid_n1, str(wt), since_n + 1.0)
        insert_message(con, sid_n1, "user",
                       "FIRST-SESSION-MSG: sentinel")
        con.close()

        proc = subprocess.Popen(
            [str(LANE_WATCH), str(wt), "--all", "--since", str(since_n)],
            env={**os.environ, "HERMES_HOME": str(newer_home),
                 "LANE_WATCH_POLL": POLL},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        buf = ""
        try:
            assert proc.stdout is not None
            stdout_fd = proc.stdout.fileno()
            buf += wait_for(proc, stdout_fd, "FIRST-SESSION-MSG", TIMEOUT)
            if "FIRST-SESSION-MSG" not in buf:
                failures.append(
                    f"newer: first session msg not printed: buf={buf!r}")
            # End the first session and wait for the watcher to
            # acknowledge it before dropping the second in. Without
            # this gate the inner wait loop may catch the new
            # session's row before the first one's ended_at is
            # visible, and the assertion below is the wrong shape
            # to detect that.
            con = open_db(newer_db)
            con.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (since_n + 2.0, sid_n1))
            con.commit()
            con.close()
            buf += wait_for(proc, stdout_fd, "waiting", TIMEOUT)
            if "session ended" not in buf:
                failures.append(
                    f"newer: 'session ended' not seen: buf={buf!r}")
            # Now insert the strictly newer session.
            con = open_db(newer_db)
            insert_session(con, sid_n2, str(wt), since_n + 3.0)
            insert_message(con, sid_n2, "user",
                           "SECOND-SESSION-MSG: sentinel")
            con.commit()
            con.close()
            buf += wait_for(proc, stdout_fd, "SECOND-SESSION-MSG", TIMEOUT)
            if "SECOND-SESSION-MSG" not in buf:
                failures.append(
                    f"newer: second session msg not printed: buf={buf!r}")
            # End the second session and stop the child.
            con = open_db(newer_db)
            con.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (since_n + 4.0, sid_n2))
            con.commit()
            con.close()
            buf += wait_for(proc, stdout_fd, "waiting", TIMEOUT)
            stdout, stderr = kill_and_drain(proc)
            buf += stdout
            if stderr.strip():
                failures.append(
                    f"newer: unexpected stderr: {stderr!r}")
            for sentinel in ("FIRST-SESSION-MSG", "SECOND-SESSION-MSG"):
                count = buf.count(sentinel)
                if count != 1:
                    failures.append(
                        f"newer: {sentinel} appeared {count} times, "
                        f"expected 1: buf={buf!r}")
            if buf.count("session ended") < 2:
                failures.append(
                    f"newer: 'session ended' appeared "
                    f"{buf.count('session ended')} times, expected >= 2: "
                    f"buf={buf!r}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # 12. Without --since, the watcher exits when the session
        # ends. The "waiting" line that --since adds must NOT appear
        # -- the script's exit path is the one --all's tail test
        # also exercises, but here we assert it explicitly so a
        # future change that re-routes the no-since path through
        # the wait loop fails fast.
        no_since_home = tmp / "no-since-home"
        no_since_db = no_since_home / "state.db"
        build_db(no_since_db)
        con = open_db(no_since_db)
        sid_ns = "s-no-since"
        insert_session(con, sid_ns, str(wt), 100.0, ended_at=101.0)
        insert_message(con, sid_ns, "user", "no-since-msg")
        con.close()

        r = run_lane_watch([str(wt), "--all"],
                           {"HERMES_HOME": str(no_since_home)})
        if r.returncode != 0:
            failures.append(
                f"no --since: exit {r.returncode}, stderr={r.stderr!r}")
        if "no-since-msg" not in r.stdout:
            failures.append(
                f"no --since: msg not printed: {r.stdout!r}")
        if "session ended" not in r.stdout:
            failures.append(
                f"no --since: 'session ended' missing: {r.stdout!r}")
        if "waiting" in r.stdout:
            failures.append(
                f"no --since: 'waiting' must NOT print: {r.stdout!r}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
