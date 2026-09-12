#!/usr/bin/env python3
"""Pin the lane-settled predicate's three-way answer.

The predicate has three states and two inert ones, and the difference between
them is what a caller gating a tree relies on. The bug the issue opens with
was a lane amending over a person twelve seconds after the resume loop had
decided the lane was done. The same answer the predicate gives -- "settled",
"still running", or "drifted" -- has to distinguish a tree the lane never
touched from one it just left, and a tree it just left from one somebody else
has been editing since.

Run directly: `python3 tests/test_lane_settled.py`. No test runner -- stdlib
subprocess + a fresh git repo per scenario is enough.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LANE_SETTLED = REPO_ROOT / "bin" / "lane-settled"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """A git invocation that fails the test on error and returns the
    CompletedProcess for inspection. `cwd` is the git working tree."""
    return subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True, text=True, check=True)


def make_worktree(tmpdir: Path) -> Path:
    """Stand up a throwaway git repo with one commit. The worktree path
    lane-settled resolves is what the predicate reads, so the repo's
    `rev-parse --show-toplevel` is the value the test pins against."""
    repo = tmpdir / "wt"
    repo.mkdir(parents=True)
    git(repo, "init", "--quiet")
    # user/email must be set; an unset identity leaves commits failing in CI
    # for a reason nothing in the test reports.
    for k, v in [("user.email", "test@test"), ("user.name", "test"),
                 ("init.defaultBranch", "main")]:
        git(repo, "config", k, v)
    (repo / "README").write_text("seed\n")
    git(repo, "add", "README")
    git(repo, "commit", "--quiet", "-m", "seed")
    return repo


def write_record(state_root: Path, worktree: Path,
                 sha: str, last_edit: str) -> None:
    """Plant the lane-final record lane-settled reads. Mirrors what
    hermes-implement writes: a sha256 of the worktree path as the filename,
    because a path contains separators and cannot be one."""
    import hashlib
    target = state_root / "lane-final" / hashlib.sha256(
        str(worktree).encode()).hexdigest()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"sha={sha}\nlast_edit_at={last_edit}\nrecorded_at=2026-09-12T00:00:00\n")


def write_active(state_root: Path, worktree: Path, pid: str) -> None:
    """Plant the lane-active marker with `pid` as the live (or dead)
    process id. Mirrors what hermes-implement writes at start."""
    import hashlib
    target = state_root / "lane-active" / hashlib.sha256(
        str(worktree).encode()).hexdigest()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pid + "\n")


def run_predicate(worktree: Path, state_root: Path) -> subprocess.CompletedProcess:
    """Invoke bin/lane-settled with AGENT_STATE_DIR pointed at a scratch
    directory so real lane records cannot leak into the test."""
    env = {**os.environ, "AGENT_STATE_DIR": str(state_root)}
    return subprocess.run(
        [str(LANE_SETTLED), str(worktree)],
        capture_output=True, text=True, env=env)


def assert_eq(label: str, want, got, failures: list) -> None:
    if want != got:
        failures.append(f"{label}: want {want!r}, got {got!r}")


def assert_in(label: str, needle: str, haystack: str, failures: list) -> None:
    if needle not in haystack:
        failures.append(f"{label}: {needle!r} not in {haystack!r}")


def main() -> int:
    failures: list = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wt = make_worktree(tmp / "real")
        # AGENT_STATE_DIR is what the predicate reads, so a directory under
        # `tmp` stands in for it.
        state = tmp / "agent-tools"

        # 1. No record, no marker. Older tree, lane finished before this check
        # existed. Must be inert: exit 0, with a message that names why.
        r = run_predicate(wt, state)
        assert_eq("no record exit code", 0, r.returncode, failures)
        assert_in("no record message",
                  "no final record", r.stderr, failures)

        # 2. Record present, no marker, current tip matches. The happy path:
        # the lane finished and the tree has not moved since.
        head = git(wt, "rev-parse", "HEAD").stdout.strip()
        write_record(state, wt, head, "2026-09-12T00:00:00")
        r = run_predicate(wt, state)
        assert_eq("settled exit code", 0, r.returncode, failures)
        assert_in("settled message", "settled", r.stdout, failures)

        # 3. Record present, current tip differs. Drift: the lane finished but
        # someone (a person rebasing, an autopilot pushing) has since moved
        # the tip. Must surface the recorded and current shas on stderr so a
        # caller has the information to decide what to do.
        (wt / "NEW").write_text("drifted\n")
        git(wt, "add", "NEW")
        git(wt, "commit", "--quiet", "-m", "drift")
        r = run_predicate(wt, state)
        assert_eq("drift exit code", 3, r.returncode, failures)
        assert_in("drift names recorded sha", head, r.stderr, failures)
        assert_in("drift names current sha",
                  git(wt, "rev-parse", "HEAD").stdout.strip(),
                  r.stderr, failures)
        assert_in("drift message", "drifted", r.stderr, failures)

        # 4. Live marker (our own pid). kill -0 succeeds, so the predicate must
        # report the lane is still running. Restoring the tip keeps this case
        # about the marker alone, not drift.
        git(wt, "reset", "--hard", head)
        write_active(state, wt, str(os.getpid()))
        r = run_predicate(wt, state)
        assert_eq("active exit code", 1, r.returncode, failures)
        assert_in("active message", "still running", r.stderr, failures)
        # The pid must be in the message -- "the lane is running" without the
        # pid leaves a reader to guess which one.
        assert_in("active names pid", str(os.getpid()), r.stderr, failures)
        write_active(state, wt, "")

        # 5. Stale marker (a pid that almost certainly isn't alive). A crash
        # without cleanup is a real failure mode; reporting "settled" in that
        # case is the dangerous wrong answer. Must fail (exit 1) with a
        # message that distinguishes "stale marker" from "lane still running"
        # -- one means clear the file, the other means wait.
        write_active(state, wt, "999999999")
        r = run_predicate(wt, state)
        assert_eq("stale exit code", 1, r.returncode, failures)
        assert_in("stale message", "stale", r.stderr, failures)

        # 6. Path that is not a git worktree. The predicate must say so on
        # stderr (a silent exit would mask the wrong invocation).
        bogus = tmp / "not-a-repo"
        bogus.mkdir()
        r = run_predicate(bogus, state)
        # exit 2 (usage / input problem), with the path named on stderr.
        assert_eq("not-a-worktree exit code", 2, r.returncode, failures)
        assert_in("not-a-worktree message",
                  "not inside a git worktree", r.stderr, failures)

        # 7. Corrupted record. A file present but unreadable sha must not
        # produce a misleading "settled". Pin that the predicate refuses --
        # silently reading sha=<empty> and comparing it against HEAD would
        # be the wrong answer.
        import hashlib
        rec = state / "lane-final" / hashlib.sha256(
            str(wt).encode()).hexdigest()
        rec.write_text("not a record at all\n")
        r = run_predicate(wt, state)
        # Either the missing-sha exit or a non-settled exit; the contract is
        # "never silently reports settled on a corrupt record".
        if r.returncode == 0 and "settled" in r.stdout:
            failures.append(
                "corrupt record: predicate reported settled without a sha")
        # Cleanup so the test does not leave a stale-active behind in any
        # future reuse of this tmpdir (the TemporaryDirectory is removed
        # anyway, but the assertion is on the contract, not on cleanup).
        (state / "lane-active" / hashlib.sha256(
            str(wt).encode()).hexdigest()).unlink(missing_ok=True)

        # 7. A lane that is running right now: marker with a live pid, and no
        # final record, because the record is only written when the lane
        # exits. This is the state every live lane is in, and answering
        # "settled" for it makes the predicate useless for the one question
        # it exists to answer.
        state2 = tmp / "agent-tools-live"
        live = subprocess.Popen(["sleep", "60"])
        try:
            write_active(state2, wt, str(live.pid))
            r = run_predicate(wt, state2)
            assert_eq("live lane, no record: exit code", 1, r.returncode, failures)
            assert_in("live lane, no record: message",
                      "still running", r.stderr, failures)
        finally:
            live.terminate()
            live.wait()

        # 8. The same marker once that process is gone, still with no record:
        # a lane that died before its trap ran. There is no tip to compare
        # against, so the caller is owed the fact rather than a verdict.
        r = run_predicate(wt, state2)
        assert_eq("dead lane, no record: exit code", 1, r.returncode, failures)
        assert_in("dead lane, no record: message",
                  "without recording a final", r.stderr, failures)

        # 9. No marker and no record at all is the inert case, and must stay
        # distinguishable from both of the above.
        r = run_predicate(wt, tmp / "agent-tools-empty")
        assert_eq("no marker, no record: exit code", 0, r.returncode, failures)

        # 10. The record is found by hashing a path, so both halves have to
        # hash the same string. A caller naming the tree with a trailing slash
        # or through `..` must still land on the record the lane wrote, or the
        # predicate silently reports "settled" for a tree it has no record of.
        state3 = tmp / "agent-tools-paths"
        head = git(wt, "rev-parse", "HEAD").stdout.strip()
        write_record(state3, wt, head, "2026-09-12T00:00:00")
        for spelling in (f"{wt}/", f"{wt}/../{wt.name}"):
            r = run_predicate(Path(spelling), state3)
            assert_eq(f"non-canonical path {spelling!r}: exit code",
                      0, r.returncode, failures)
            assert_in(f"non-canonical path {spelling!r}: found the record",
                      "settled at", r.stdout, failures)

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
