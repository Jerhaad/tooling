#!/usr/bin/env python3
"""Pin hermes-implement's GPU lock across the retry loop.

The contract being tested: when a GPU group is configured, hermes-implement
takes the lock once before attempt 1, holds it across every attempt (the
spawned hermes children inherit fd 8 so the lock survives even when one of
them exits non-zero), and releases it only after the last attempt completes.
A second hermes-implement call must not be able to acquire the same lock
until the first finishes.

Run directly: `python3 tests/test_hermes_implement.py`.
"""
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "hermes-implement"
LIB = REPO_ROOT / "lib" / "common.sh"

# A fake `gh` that ignores every command except `issue view`. The script's
# one network call is `gh issue view`, which prints an issue body to stdout.
# We use it to make hermes-implement see the issue text the same way it
# would on a real machine.
FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Whatever the operator asks gh to do, return a no-op success unless
    # it is the one command hermes-implement issues: `gh issue view`.
    # `gh issue view` is what populates $WORK/issue.txt; the body never
    # matters to the lock test.
    if [[ "$1" == "issue" && "$2" == "view" ]]; then
        cat <<'BODY'
title:   GPU lock regression
state:   OPEN
BODY
        exit 0
    fi
    exit 0
""")

# Each call logs PID, timestamp, whether fd 8 was inherited (proves
# hermes-implement passed the GPU lock fd to its children), and the
# prompt it received via --query-file. The prompt's sha256 is part of
# the test's assertion -- a hermes that was passed `-z "PROMPT"` would
# get the prompt as argv, but the helper hermes-implement uses reads
# it from `--query-file PATH`, so a stub without `--query-file` on
# argv never sees the prompt at all.
#
# Optional FAKE_HERMES_PLAN: a colon-separated list of actions, one
# per attempt. With the variable unset or empty, attempt 1 times
# out and attempt 2 exits 0. Each action is one of:
#   do_nothing     exit 0, leave worktree untouched
#   timeout        exit 124, leave worktree untouched
#   commit         empty commit, leave worktree clean
#   commit_dirty   empty commit, then write STUB_DIRTY_FILE
#   finish         commit, write HANDOVER.md in the run directory
#   finish_clean   commit_dirty + finish + remove the sentinel
# Attempts past the plan's end behave like do_nothing.
FAKE_HERMES = textwrap.dedent("""\
    #!/usr/bin/env python3
    import hashlib, os, subprocess, sys, time
    args = sys.argv[1:]
    log = os.environ.get("FAKE_HERMES_LOG", "/tmp/fake_hermes.log")

    # fd 8 is the GPU group lock. If hermes-implement did not inherit
    # fd 8 to its child, /proc/self/fd/8 does not exist and the lock is
    # gone for the duration of this hermes. If it does exist, this hermes
    # holds the lock until it exits, and the parent's exec 8>&- after
    # the loop is a no-op for this hold -- the file description stays
    # open through the child's lifetime.
    fd8_held = os.path.exists("/proc/self/fd/8")

    # --query-file is the path of the prompt; the helper writes the
    # prompt there and the stub reads it back. If hermes-implement had
    # gone through `-z "$prompt"` instead, this index would never match
    # and the stub would not know what to record.
    query_path = None
    has_query_file = "--query-file" in args
    if has_query_file:
        query_path = args[args.index("--query-file") + 1]
    query_sha = ""
    query_bytes = 0
    if query_path and os.path.exists(query_path):
        with open(query_path, "rb") as qf:
            data = qf.read()
        query_bytes = len(data)
        query_sha = hashlib.sha256(data).hexdigest()

    is_continue = "--continue" in args
    pid = os.getpid()
    ts = time.monotonic()

    # Each invocation consumes one entry from FAKE_HERMES_PLAN; the
    # counter is recorded before the action runs so the log line also
    # tells the test which step the run reached. With the plan unset
    # (the legacy case the original GPU-lock test relies on) attempt
    # 1 times out and attempt 2 exits 0.
    plan_raw = os.environ.get("FAKE_HERMES_PLAN", "")
    state_dir = os.environ.get("FAKE_HERMES_STATE", "/tmp/fake_hermes.state")
    step = 0
    try:
        with open(state_dir) as f:
            step = int((f.read() or "0").strip() or "0")
    except (FileNotFoundError, ValueError):
        step = 0
    with open(state_dir, "w") as f:
        f.write(str(step + 1))
    if plan_raw:
        actions = plan_raw.split(":")
    elif step == 0:
        # Legacy: attempt 1 times out, attempt 2 does nothing. The
        # original GPU-lock test pins this shape.
        actions = ["timeout", "do_nothing"]
    else:
        actions = ["do_nothing"]
    if step < len(actions):
        action = actions[step]
    else:
        action = "do_nothing"

    with open(log, "a") as f:
        f.write(f"call pid={pid} continue={is_continue} fd8={fd8_held} "
                f"has_query_file={has_query_file} "
                f"query_bytes={query_bytes} "
                f"query_sha={query_sha} action={action} t={ts:.6f}\\n")
        f.flush()

    # Run the action against the worktree the script handed the lane.
    # The fake sets safe GIT_AUTHOR_* defaults so `git commit` works
    # without the lane having exported them.
    wt = os.getcwd()
    work = os.environ.get("TMPDIR", "")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "stub",
           "GIT_AUTHOR_EMAIL": "stub@stub",
           "GIT_COMMITTER_NAME": "stub",
           "GIT_COMMITTER_EMAIL": "stub@stub"}
    def run_git(*a):
        subprocess.check_call(["git", "-C", wt, *a], env=env)
    if action == "timeout":
        sys.exit(124)
    if action in ("commit", "commit_dirty", "finish", "finish_clean"):
        run_git("commit", "--allow-empty", "-m", "stub commit")
    if action in ("commit_dirty", "finish_clean"):
        # Untracked so it shows up in `git status --short` but the
        # lane is not committing it on this attempt.
        with open(os.path.join(wt, "STUB_DIRTY_FILE"), "w") as f:
            f.write("left dirty by fake\\n")
    if action in ("finish", "finish_clean"):
        if work:
            os.makedirs(work, exist_ok=True)
            with open(os.path.join(work, "HANDOVER.md"), "w") as f:
                f.write("stub handover\\n")
    if action in ("finish_clean",):
        # finish_clean clears the sentinel commit_dirty left behind so
        # the next iteration's `finished` check sees an empty tree.
        sentinel = os.path.join(wt, "STUB_DIRTY_FILE")
        if os.path.exists(sentinel):
            os.remove(sentinel)
    sys.exit(0)
""")


def setup_env(tmp: Path, **extra) -> dict:
    """Build the env hermes-implement will run under: a fake `gh` on
    PATH, a fake hermes at HERMES_PYTHON, a scratch state directory,
    and a GPU_GROUP_DEFAULT so the lock is configured. `**extra`
    overrides the defaults (HERMES_IMPLEMENT_ATTEMPTS, FAKE_HERMES_PLAN)."""
    bin_dir = tmp / "fakebin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(FAKE_GH)
    fake_gh.chmod(0o755)
    fake_hermes = bin_dir / "fake_hermes"
    fake_hermes.write_text(FAKE_HERMES)
    fake_hermes.chmod(0o755)

    state = tmp / "state"
    state.mkdir()

    log = tmp / "fake_hermes.log"
    log.write_text("")

    (tmp / "fake_hermes.state").write_text("0")

    (tmp / "fake.env").write_text("")
    (tmp / "hermes_home").mkdir()
    (tmp / "tmpdir").mkdir()
    base = {**os.environ,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            # The fake HERMES_PYTHON script is its own python interpreter
            # (shebang), so the script's -m hermes_cli.main invocation is
            # answered by the fake rather than the real hermes package.
            "HERMES_PYTHON": str(fake_hermes),
            # Tight per-attempt timeout. The fake exits 124 on attempt 1
            # before this fires, but it's here in case a future change
            # makes the fake wait instead.
            "HERMES_IMPLEMENT_TIMEOUT": "30",
            # Two attempts only -- the test cares about attempt 1 timing
            # out and attempt 2 succeeding; further attempts are noise.
            "HERMES_IMPLEMENT_ATTEMPTS": "2",
            "AGENT_STATE_DIR": str(state),
            "GPU_GROUP_DEFAULT": "test-gpu-group",
            "TMPDIR": str(tmp / "tmpdir"),
            "FAKE_HERMES_LOG": str(log),
            "FAKE_HERMES_STATE": str(tmp / "fake_hermes.state"),
            # hermes-implement sources its env file; let an empty one
            # stand in for the operator's so HOST_BUILDER etc. do not
            # leak into the test.
            "TOOLS_ENV": str(tmp / "fake.env"),
            # hermes-implement reads the verification evidence DB if it
            # exists at $HERMES_HOME/verification_evidence.db. Point it
            # at a path that does not have one.
            "HERMES_HOME": str(tmp / "hermes_home")}
    base.update(extra)
    return base


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True, env=env)
    run("init", "--initial-branch=main")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n")
    run("add", "a.txt")
    run("commit", "-m", "base")
    return repo


def test_hermes_implement_holds_gpu_lock_across_retry():
    """hermes-implement must take the GPU group lock once, hold it across
    every attempt (including ones that exit non-zero), and release it only
    after the last attempt. The fake hermes exits 124 on attempt 1 and 0
    on attempt 2; both calls must record fd 8 held, and a concurrent
    caller running while the loop is in flight must block on the same
    lock."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp)
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        # The first hermes-implement call. It will run attempt 1 (which
        # exits 124) and attempt 2 (which exits 0). Both must run while
        # fd 8 is held, and the lock must be released at the end.
        proc = subprocess.run(
            [str(BIN), "--issue", "999", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")

        # Neither attempt committed, so the run must say so on stderr. The
        # lock release after the loop once redirected the rest of the
        # script's stderr to /dev/null, and this line was the first casualty.
        assert "FAILED:" in proc.stderr, (
            f"no FAILED line after attempts that never committed: "
            f"stderr={proc.stderr!r}")

        # Both attempts must have run.
        calls = log.read_text().splitlines()
        # Each fake call appends one line. We expect 2 calls (attempt 1
        # and attempt 2).
        assert len(calls) == 2, (
            f"expected 2 hermes invocations, got {len(calls)}: {calls!r}")
        # Both calls must have inherited fd 8 (the GPU lock).
        for line in calls:
            m = re.match(r"call pid=(\d+) continue=(\S+) fd8=(\S+) "
                         r"has_query_file=(\S+) query_bytes=(\d+) "
                         r"query_sha=(\S*) action=(\S+) t=(\S+)", line)
            assert m, f"unparseable log line: {line!r}"
            assert m.group(3) == "True", (
                f"hermes child did not inherit fd 8 from hermes-implement: "
                f"{line!r}")
            # The agent must have been called with --query-file, and the
            # prompt file must have been readable. A hermes-implement that
            # passed the prompt as `-z "$prompt"` would fail this: the
            # stub never sees `--query-file` and query_bytes stays 0.
            assert m.group(4) == "True", (
                f"hermes-implement should call agent with --query-file; "
                f"this means the prompt went over argv (-z) instead: "
                f"{line!r}")
            assert int(m.group(5)) > 0, (
                f"hermes-implement's --query-file pointed at an empty or "
                f"missing file: {line!r}")
        # First call is attempt 1 (no --continue, legacy action=timeout),
        # second is attempt 2 (with --continue, legacy action=do_nothing).
        assert "continue=False" in calls[0], calls[0]
        assert "action=timeout" in calls[0], calls[0]
        assert "continue=True" in calls[1], calls[1]


def _read_attempt_prompts(run_dir: Path) -> list[str]:
    """Read each attempt's prompt the script wrote under $WORK. Returns
    the bodies in attempt order, so a test that wants to assert on the
    resume prompt knows which file to look at."""
    out = []
    for path in sorted(run_dir.glob("attempt*.prompt"),
                       key=lambda p: int(re.match(r"attempt(\d+)", p.stem).group(1))):
        out.append(path.read_text())
    return out


def test_hermes_implement_resumes_after_one_commit_with_dirty_tree():
    """Central pin: attempt 1 commits and dirties the tree; attempt 2
    must run with --continue and the resume prompt must name the
    commit and the dirty file. The regression: a retry loop that
    ended on the first committed item."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="3",
                        # attempt 1: commit + leave dirty sentinel.
                        # attempt 2: commit on top, write HANDOVER.md,
                        # remove sentinel. attempt 3 is a no-op the
                        # loop must NOT reach.
                        FAKE_HERMES_PLAN="commit_dirty:finish_clean:do_nothing")
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "145", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        # The pin is also the post-loop shape: no FAILED line, since
        # attempt 2 wrote HANDOVER.md and the tree was left clean.
        assert "FAILED:" not in proc.stderr, (
            f"run that finished cleanly reported FAILED: "
            f"stderr={proc.stderr!r}")

        calls = log.read_text().splitlines()
        assert len(calls) == 2, (
            f"expected 2 invocations, got {len(calls)}: {calls!r}")
        # Attempt 1 (no --continue) ran commit_dirty; attempt 2
        # (--continue) ran finish_clean.
        assert "continue=False" in calls[0]
        assert "action=commit_dirty" in calls[0], calls[0]
        assert "continue=True" in calls[1]
        assert "action=finish_clean" in calls[1], calls[1]

        # The resume prompt attempt 2 received names the commit and
        # the dirty file. The script writes each attempt's prompt
        # under $WORK and prints the run directory on its last
        # stdout line; the test reads it from there.
        run_dir = Path(proc.stdout.strip().splitlines()[-1])
        prompts = _read_attempt_prompts(run_dir)
        assert len(prompts) == 2, prompts
        resume = prompts[1]
        # The commit attempt 1 left shows up under 'Committed since base'.
        assert "stub commit" in resume, (
            f"resume prompt does not name attempt 1's commit: "
            f"{resume!r}")
        # The dirty sentinel attempt 1 left shows up under
        # 'Uncommitted files' (a `??` line in `git status --short`).
        assert "STUB_DIRTY_FILE" in resume, (
            f"resume prompt does not name the uncommitted file: "
            f"{resume!r}")
        assert "HANDOVER.md: missing" in resume, resume
        # The prompt must describe the work and forbid discarding it:
        # finished() cannot tell a tree cleaned by `git stash` from
        # one cleaned by finishing the work. See the parallel
        # assertion in tests/test_lane_settled.py.
        assert "Never discard, stash, or reset work" in resume, (
            f"resume prompt does not forbid discarding the work to "
            f"get a clean tree: {resume!r}")
        assert "deliverable is a clean tree" not in resume, (
            f"resume prompt still frames a clean tree as the "
            f"deliverable: {resume!r}")

        # Two stub commits (one per attempt), the sentinel is gone,
        # the tree is clean.
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True, text=True, check=True).stdout
        assert dirty == "", (
            f"worktree still dirty after finish: {dirty!r}")
        commits = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True).stdout
        assert commits.count("stub commit") == 2, (
            f"expected base + 2 stub commits, got: {commits!r}")


def test_hermes_implement_resumes_after_timeout_with_dirty_tree():
    """A 124 is just another non-zero exit: the resume policy is the
    same whether the attempt died on its own timer or was killed by
    the script's `timeout` wrapper."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="3",
                        # attempt 1: exit 124. attempt 2: finish.
                        # attempt 3: no-op the loop must NOT reach.
                        FAKE_HERMES_PLAN="timeout:finish:do_nothing")
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "145", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")
        assert "FAILED:" not in proc.stderr, (
            f"timed-out attempt that resumed cleanly reported FAILED: "
            f"stderr={proc.stderr!r}")

        calls = log.read_text().splitlines()
        # Attempts 1 and 2 ran, attempt 3 was a no-op after attempt 2
        # reached the finish state -- the loop's break on `finished`
        # stops before it.
        assert len(calls) == 2, (
            f"expected exactly 2 invocations under a 124 then finish plan, "
            f"got {len(calls)}: {calls!r}")
        assert "continue=False" in calls[0]
        assert "action=timeout" in calls[0], calls[0]
        assert "continue=True" in calls[1]
        assert "action=finish" in calls[1], calls[1]


def test_hermes_implement_does_not_retry_when_finished():
    """A finished attempt (clean tree + non-empty HANDOVER.md) has
    reached the run's goal: attempt 2 must not run. The
    committed-but-no-handover case was the regression the lane
    reported; the assertion checks clean + present together."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="3",
                        FAKE_HERMES_PLAN="finish:do_nothing:do_nothing")
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "145", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")
        assert "FAILED:" not in proc.stderr
        assert "WARNING" not in proc.stderr

        calls = log.read_text().splitlines()
        assert len(calls) == 1, (
            f"a finished attempt must not trigger a retry, got "
            f"{len(calls)} invocations: {calls!r}")
        assert "continue=False" in calls[0]
        assert "action=finish" in calls[0], calls[0]


def test_hermes_implement_reports_uncommitted_files_when_attempts_exhausted():
    """At the cap with uncommitted work left, the run's final stderr
    line must name the uncommitted files. The lane reported an
    auto-commit stranded unverified work; the test pins the loop
    leaves it stranded."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        env = setup_env(tmp,
                        HERMES_IMPLEMENT_ATTEMPTS="3",
                        # Three attempts, no finish -- the cap is the
                        # only thing that stops the loop. Each
                        # attempt commits then re-dirties the
                        # sentinel, so `finished` stays false.
                        FAKE_HERMES_PLAN=(
                            "commit_dirty:commit_dirty:commit_dirty"))
        repo = make_repo(tmp)
        log = tmp / "fake_hermes.log"

        proc = subprocess.run(
            [str(BIN), "--issue", "145", "--worktree", str(repo),
             "--verify", "true"],
            env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f"hermes-implement failed: rc={proc.returncode} "
            f"stderr={proc.stderr!r}")

        calls = log.read_text().splitlines()
        assert len(calls) == 3, (
            f"expected 3 invocations under ATTEMPTS=3, got {len(calls)}: "
            f"{calls!r}")
        # The script must not pile on a final "fix the failing test"
        # commit when the loop gives up. base + 3 commits = 4 lines,
        # every stub commit body starts with 'stub commit'.
        commits = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True, text=True, check=True).stdout
        assert commits.count("stub commit") == 3, (
            f"expected 3 stub commits (one per attempt), got: "
            f"{commits!r}")
        assert "STUB_DIRTY_FILE" in proc.stderr, (
            f"final stderr does not name the uncommitted file: "
            f"stderr={proc.stderr!r}")
        assert "FAILED: 3 attempts" in proc.stderr, (
            f"final FAILED line missing or wrong: stderr={proc.stderr!r}")
        assert "?? STUB_DIRTY_FILE" in proc.stderr, (
            f"untracked file not flagged on the FAILED line: "
            f"stderr={proc.stderr!r}")




        # After the script finishes, fd 8 must be released -- a fresh
        # process must be able to flock the lock without blocking.
        flock_check = subprocess.run(
            ["bash", "-c",
             f'set -eu; . "{LIB}" >/dev/null 2>&1; '
             f'acquire_gpu_lock ""; echo acquired; exec 8>&-'],
            env=env, capture_output=True, text=True, timeout=10)
        assert flock_check.returncode == 0, (
            f"GPU lock not released after hermes-implement exit: "
            f"stderr={flock_check.stderr!r}")
        assert "acquired" in flock_check.stdout, (
            f"post-exit acquire did not print 'acquired': "
            f"stdout={flock_check.stdout!r}")


def main() -> int:
    failures: list[tuple[str, str]] = []
    for name, fn in [
        ("hermes-implement holds the GPU lock across a retry and "
         "inherits fd 8 to its hermes children",
         test_hermes_implement_holds_gpu_lock_across_retry),
        ("hermes-implement resumes with --continue after one commit "
         "and a dirty tree",
         test_hermes_implement_resumes_after_one_commit_with_dirty_tree),
        ("hermes-implement resumes after an attempt 1 that exits 124 "
         "with a dirty tree",
         test_hermes_implement_resumes_after_timeout_with_dirty_tree),
        ("hermes-implement does not retry when attempt 1 already "
         "finished (clean tree + non-empty HANDOVER.md)",
         test_hermes_implement_does_not_retry_when_finished),
        ("hermes-implement names the uncommitted files on the final "
         "FAILED line and does not commit leftover work itself",
         test_hermes_implement_reports_uncommitted_files_when_attempts_exhausted),
    ]:
        try:
            fn()
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures.append((name, repr(e)))
            print(f"FAIL {name}: {e!r}")
        else:
            print(f"ok   {name}")
    if failures:
        return 1
    print("PASS hermes-implement holds the GPU lock across every attempt, "
          "and resumes with --continue until the tree is clean and "
          "HANDOVER.md is present")
    return 0


if __name__ == "__main__":
    sys.exit(main())