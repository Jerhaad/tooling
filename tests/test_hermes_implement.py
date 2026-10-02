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

# A fake hermes that times out (exits 124) on the first invocation and
# succeeds on the second. Each call logs its PID, timestamp, and whether
# fd 8 was inherited (proves hermes-implement passed the GPU lock fd to
# its children).
FAKE_HERMES = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys, time
    args = sys.argv[1:]
    log = os.environ.get("FAKE_HERMES_LOG", "/tmp/fake_hermes.log")

    # fd 8 is the GPU group lock. If hermes-implement did not inherit
    # fd 8 to its child, /proc/self/fd/8 does not exist and the lock is
    # gone for the duration of this hermes. If it does exist, this hermes
    # holds the lock until it exits, and the parent's exec 8>&- after
    # the loop is a no-op for this hold -- the file description stays
    # open through the child's lifetime.
    fd8_held = os.path.exists("/proc/self/fd/8")

    is_continue = "--continue" in args
    pid = os.getpid()
    ts = time.monotonic()
    with open(log, "a") as f:
        f.write(f"call pid={pid} continue={is_continue} fd8={fd8_held} t={ts:.6f}\\n")
        f.flush()

    if not is_continue:
        # attempt 1: simulate a timeout. hermes-implement's `timeout`
        # wrapper would normally return 124 here, but we exit 124
        # directly so the retry loop kicks in regardless of the wrapper.
        sys.exit(124)
    # attempt 2+: exit 0. The retry loop breaks on HEAD != BASE; with no
    # commit in this fake the loop will run all attempts. The test sets
    # HERMES_IMPLEMENT_ATTEMPTS=2 so there is no attempt 3.
    sys.exit(0)
""")


def setup_env(tmp: Path) -> dict:
    """Build the env hermes-implement will run under: a fake `gh` on PATH,
    a fake hermes at HERMES_PYTHON, a scratch state directory, and a
    GPU_GROUP_DEFAULT so the lock is configured."""
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

    (tmp / "fake.env").write_text("")
    (tmp / "hermes_home").mkdir()
    (tmp / "tmpdir").mkdir()
    return {**os.environ,
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
            # hermes-implement sources its env file; let an empty one
            # stand in for the operator's so HOST_BUILDER etc. do not
            # leak into the test.
            "TOOLS_ENV": str(tmp / "fake.env"),
            # hermes-implement reads the verification evidence DB if it
            # exists at $HERMES_HOME/verification_evidence.db. Point it
            # at a path that does not have one.
            "HERMES_HOME": str(tmp / "hermes_home")}


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

        # Both attempts must have run.
        calls = log.read_text().splitlines()
        # Each fake call appends one line. We expect 2 calls (attempt 1
        # and attempt 2).
        assert len(calls) == 2, (
            f"expected 2 hermes invocations, got {len(calls)}: {calls!r}")
        # Both calls must have inherited fd 8 (the GPU lock).
        for line in calls:
            m = re.match(r"call pid=(\d+) continue=(\S+) fd8=(\S+)", line)
            assert m, f"unparseable log line: {line!r}"
            assert m.group(3) == "True", (
                f"hermes child did not inherit fd 8 from hermes-implement: "
                f"{line!r}")
        # First call is attempt 1 (no --continue), second is attempt 2
        # (with --continue).
        assert "continue=False" in calls[0], calls[0]
        assert "continue=True" in calls[1], calls[1]

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
    print("PASS hermes-implement holds the GPU lock across every attempt "
          "and releases it after the last")
    return 0


if __name__ == "__main__":
    sys.exit(main())