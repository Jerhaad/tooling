#!/usr/bin/env python3
"""Pin that gate-prove runs the suite before reverting, and refuses to
prove a branch whose suite was already red.

The bug this covers is the gate reporting "tests depend on the change"
when no test ever ran: a build that fails before any test executes (a
regenerated offline cache the revert removes, a renamed recipe) exits
non-zero, which the gate used to read as proof. The fix is a pre-run on
the unmodified tree: if it does not pass, the gate must say so and
refuse to claim either answer.

The three cases the issue names are the three things the verdict must
be able to say:

- red before the revert reaches exit 3 (unprovable)
- passes then fails reaches exit 0 (the tests depend on the change)
- passes then passes reaches exit 1 (the tests do not depend on the change)

Each case builds its own throwaway repo so the suite passes or fails on
demand, through `gates.toml`'s `prove.command` pointing at a script
under tests/. The script's exit code is what the gate reads; making
the script's verdict depend on a marker file the source change carries
is what makes "post" behave differently from "pre".

Run directly: `python3 tests/test_gate_prove.py`. No runner, no
dependency.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_PROVE = REPO_ROOT / "bin" / "gate-prove"


def build_repo(tmpdir: Path, *, test_post_condition: str) -> str:
    """Make a throwaway repo with one base commit and one branch commit.

    `test_post_condition` is the shell snippet a test script under
    tests/ runs to decide pass/fail. It is sourced into a generated
    test, which exits 0 when the snippet exits 0 and 1 otherwise.

    The branch commit changes `src.sh` (so something can be reverted)
    and `tests/test_smoke.sh` (so a test file changed). The branch's
    source value is `BRANCH_VALUE`; the base's is `BASE_VALUE`. The
    snippet's job is to decide whether the current value matches what
    a test on the branch would expect.

    Returns the SHA of the base commit, for PROVE_BASE.
    """
    # Each case names its own snippet: the gate's verdict is a function of
    # the snippet's exit code on each run.
    src_base = "BASE_VALUE=1\n"
    src_branch = "BRANCH_VALUE=2\n"
    test_script = (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        f"{test_post_condition}\n"
    )

    (tmpdir / "src.sh").write_text(src_base)
    (tmpdir / "tests").mkdir()
    (tmpdir / "tests" / "test_smoke.sh").write_text(test_script)
    (tmpdir / "tests" / "test_smoke.sh").chmod(0o755)

    # gates.toml declares the patterns and the command. The command
    # runs the test script; src.sh's value is what the snippet reads.
    (tmpdir / "gates.toml").write_text(
        "[prove]\n"
        "tests = '^tests/.*\\.sh$'\n"
        "sources = '^src\\.sh$'\n"
        "command = 'bash tests/test_smoke.sh'\n"
    )

    def git(*args: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(tmpdir), *args],
            capture_output=True, text=True, check=True, **kwargs)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base_sha = git("rev-parse", "HEAD").stdout.strip()

    # The branch: src.sh carries the marker the snippet reads; tests/
    # test_smoke.sh is rewritten so the diff against base has a real
    # test change, not a touch.
    git("checkout", "-q", "-b", "feat")
    (tmpdir / "src.sh").write_text(src_branch)
    (tmpdir / "tests" / "test_smoke.sh").write_text(
        test_script + "# feature branch\n")
    (tmpdir / "tests" / "test_smoke.sh").chmod(0o755)
    git("add", "-A")
    git("commit", "-q", "-m", "feat")
    return base_sha


def run_prove(worktree: Path, base_sha: str) -> subprocess.CompletedProcess:
    """Invoke gate-prove against a throwaway worktree with PROVE_BASE
    pointed at the base commit. The worktree must be clean (the gate
    refuses dirty trees), so the repo is set up to leave it that way."""
    return subprocess.run(
        [str(GATE_PROVE), str(worktree)],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin",
             "HOME": str(Path.home()),
             "PROVE_BASE": base_sha})


def assert_exit(label: str, want: int, r: subprocess.CompletedProcess,
                failures: list) -> None:
    """A single verdict comparison. The gate's exit code is the contract;
    the messages on stdout/stderr are evidence the test was the one the
    issue asked for, not something with the same code by accident."""
    if r.returncode != want:
        failures.append(
            f"{label}: exit={r.returncode} want={want} "
            f"stdout={r.stdout.strip()[:200]!r} "
            f"stderr={r.stderr.strip()[:200]!r}")
        return
    # Red-on-arrival must say so; the gate's wording names the unmodified
    # tree, which is what distinguishes it from the existing post-run
    # unprovable message.
    if label == "red before the revert" and "unmodified tree" not in (
            r.stderr + r.stdout).lower():
        failures.append(
            f"{label}: exit 3 reached, but message did not name the "
            f"unmodified tree (where the red run happened): "
            f"stderr={r.stderr.strip()!r}")
    # "tests depend on the change" is the gate's existing wording; both
    # a passing and a failing post-run speak that, but exit 0 means
    # post-run failed -- the test depended on the change.
    if label == "passes then fails" and "tests depend on the change" not in (
            r.stderr + r.stdout).lower():
        failures.append(
            f"{label}: exit 0 reached, but verdict line missing: "
            f"stdout={r.stdout.strip()!r}")


def assert_clean_tree(worktree: Path, label: str,
                      failures: list) -> None:
    """The gate's restore trap must leave the worktree clean on every
    exit path. A green exit that leaves the source reverted is worse
    than no gate: the next run starts on the wrong tree."""
    r = subprocess.run(
        ["git", "-C", str(worktree), "status", "--short"],
        capture_output=True, text=True, check=True)
    if r.stdout.strip():
        failures.append(
            f"{label}: worktree not clean after gate-prove: {r.stdout!r}")


def case_red_before_revert(failures: list) -> None:
    """Test asserts the BASE behavior. On the branch the source carries
    BRANCH_VALUE, so the assertion fails before anything is reverted.
    The gate must read that as unprovable: the branch is already red,
    and any post-run verdict would be measured against no baseline."""
    with tempfile.TemporaryDirectory() as t:
        tmpdir = Path(t)
        # Snippet reads src.sh, expects the BASE_VALUE the branch removed.
        snippet = (
            "[ -f src.sh ] || { echo 'src.sh missing'; exit 1; }\n"
            "grep -q '^BASE_VALUE=1$' src.sh "
            "|| { echo 'src.sh is not the base version'; exit 1; }\n"
        )
        base_sha = build_repo(tmpdir, test_post_condition=snippet)
        r = run_prove(tmpdir, base_sha)
        assert_exit("red before the revert", 3, r, failures)
        assert_clean_tree(tmpdir, "red before the revert", failures)


def case_passes_then_fails(failures: list) -> None:
    """Test asserts the BRANCH behavior. On the branch the source
    carries BRANCH_VALUE, so the assertion passes. After revert the
    source is back to BASE_VALUE and the assertion fails. The gate
    must say so: tests depend on the change."""
    with tempfile.TemporaryDirectory() as t:
        tmpdir = Path(t)
        snippet = (
            "[ -f src.sh ] || { echo 'src.sh missing'; exit 1; }\n"
            "grep -q '^BRANCH_VALUE=2$' src.sh "
            "|| { echo 'src.sh is not the branch version'; exit 1; }\n"
        )
        base_sha = build_repo(tmpdir, test_post_condition=snippet)
        r = run_prove(tmpdir, base_sha)
        assert_exit("passes then fails", 0, r, failures)
        assert_clean_tree(tmpdir, "passes then fails", failures)


def case_passes_then_passes(failures: list) -> None:
    """Test passes regardless of what src.sh contains. Both pre-run
    and post-run succeed, so the gate must say the tests do not
    depend on the change.

    The test command is built to not read src.sh at all, so the
    post-revert state of the working tree cannot affect it.
    """
    with tempfile.TemporaryDirectory() as t:
        tmpdir = Path(t)
        snippet = ": # always pass\n"  # `:` is bash's no-op, exit 0
        base_sha = build_repo(tmpdir, test_post_condition=snippet)
        r = run_prove(tmpdir, base_sha)
        assert_exit("passes then passes", 1, r, failures)
        assert_clean_tree(tmpdir, "passes then passes", failures)


def case_command_missing(failures: list) -> None:
    """A command the shell cannot find exits 127 on the pre-run. That is
    not a red branch: nothing was measured, and saying "already red"
    would be the gate asserting something it never established. The two
    unprovable answers must read differently."""
    with tempfile.TemporaryDirectory() as t:
        tmpdir = Path(t)
        base_sha = build_repo(tmpdir, test_post_condition=": # unused\n")
        (tmpdir / "gates.toml").write_text(
            "[prove]\n"
            "tests = '^tests/.*\\.sh$'\n"
            "sources = '^src\\.sh$'\n"
            "command = 'definitely-not-a-real-command'\n"
        )
        subprocess.run(["git", "-C", str(tmpdir), "commit", "-qam", "cmd"],
                       check=True, capture_output=True)
        r = run_prove(tmpdir, base_sha)
        assert_exit("command missing", 3, r, failures)
        out = (r.stdout + r.stderr).lower()
        if "did not run" not in out:
            failures.append(
                f"command missing: exit 3 reached, but the message does not "
                f"say the command did not run: {r.stderr.strip()!r}")
        if "already red" in out:
            failures.append(
                f"command missing: reported the branch as already red, which "
                f"the gate did not measure: {r.stderr.strip()!r}")


def case_nothing_to_revert(failures: list) -> None:
    """A branch whose change reached the base by another route has
    nothing to revert. The gate must say so, and must not spend a suite
    run to find out: on a real project that is a full build."""
    with tempfile.TemporaryDirectory() as t:
        tmpdir = Path(t)
        marker = tmpdir / "suite-ran"
        build_repo(tmpdir, test_post_condition=": # unused\n")
        (tmpdir / "gates.toml").write_text(
            "[prove]\n"
            "tests = '^tests/.*\\.sh$'\n"
            "sources = '^src\\.sh$'\n"
            f"command = 'touch {marker}'\n"
        )
        subprocess.run(["git", "-C", str(tmpdir), "commit", "-qam", "cmd"],
                       check=True, capture_output=True)

        def git(*args):
            return subprocess.run(["git", "-C", str(tmpdir), *args],
                                  check=True, capture_output=True, text=True)

        # The same source content reaches main by another route, so
        # reverting the branch's source against it is a no-op.
        git("checkout", "-q", "main")
        (tmpdir / "src.sh").write_text("BRANCH_VALUE=2\n")
        git("commit", "-qam", "same change, other route")
        base_sha = git("rev-parse", "HEAD").stdout.strip()
        git("checkout", "-q", "feat")

        r = run_prove(tmpdir, base_sha)
        assert_exit("nothing to revert", 3, r, failures)
        if "nothing to revert" not in (r.stdout + r.stderr).lower():
            failures.append(
                f"nothing to revert: message did not say so: "
                f"{r.stderr.strip()!r}")
        if marker.exists():
            failures.append(
                "nothing to revert: the suite ran before the gate worked out "
                "there was nothing to measure")


def main() -> int:
    failures: list = []

    # Sanity: a missing gate-prove would otherwise fail with
    # FileNotFoundError, which reads as "the gate does not work" rather
    # than "the test is broken".
    if not GATE_PROVE.is_file():
        failures.append(f"missing tool: {GATE_PROVE}")

    case_red_before_revert(failures)
    case_passes_then_fails(failures)
    case_passes_then_passes(failures)
    case_command_missing(failures)
    case_nothing_to_revert(failures)

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS gate-prove: red->3, pass-then-fail->0, pass-then-pass->1,\n     missing command->3, nothing to revert->3 without running the suite")
    return 0


if __name__ == "__main__":
    sys.exit(main())