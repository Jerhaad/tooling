#!/usr/bin/env python3
"""Pin that gate-prove runs the suite before reverting, and refuses to
prove a branch whose suite was already red.

The bug this covers is the gate reporting "tests depend on the change"
when no test ever ran: a build that fails before any test executes (a
regenerated offline cache the revert removes, a renamed recipe) exits
non-zero, which the gate used to read as proof. The fix is a pre-run on
the unmodified tree: if it does not pass, the gate must say so and
refuse to claim either answer.

The five cases the verdict must cover, plus the one that pins the kill
safety, are:

- red before the revert reaches exit 3 (unprovable)
- passes then fails reaches exit 0 (the tests depend on the change)
- passes then passes reaches exit 1 (the tests do not depend on the change)
- a missing test command reaches exit 3 (not "already red")
- a branch whose source already matches the base reaches exit 3 (no
  suite is run; "nothing to measure" is faster than a full build)
- a SIGTERM or SIGKILL mid-suite leaves the caller's tree byte-identical
  to HEAD, pinned because the prove runs in a scratch worktree the
  gate creates and removes; the caller's tree is never written
- a caller-supplied `-- test command` override runs against the scratch,
  not the caller's worktree, so the post-revert run sees the reverted
  source the manifest path already saw

Each case builds its own throwaway repo so the suite passes or fails on
demand, through `gates.toml`'s `prove.command` pointing at a script
under tests/. The script's exit code is what the gate reads; making
the script's verdict depend on a marker file the source change carries
is what makes "post" behave differently from "pre".

Run directly: `python3 tests/test_gate_prove.py`. No runner, no
dependency.
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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
    pointed at the base commit. PROVE_BASE is set so the gate's base is
    the test's base, not origin/main; TMPDIR is forwarded so the gate's
    scratch sits in the lane's private temp dir rather than /tmp."""
    env = {"PATH": "/usr/bin:/bin",
           "HOME": str(Path.home()),
           "PROVE_BASE": base_sha}
    tmp = os.environ.get("TMPDIR")
    if tmp:
        env["TMPDIR"] = tmp
    return subprocess.run(
        [str(GATE_PROVE), str(worktree)],
        capture_output=True, text=True, env=env)


def run_prove_with_override(worktree: Path, base_sha: str,
                            override: list[str]) -> subprocess.CompletedProcess:
    """Invoke gate-prove with a caller-supplied `-- test command` override."""
    env = {"PATH": "/usr/bin:/bin",
           "HOME": str(Path.home()),
           "PROVE_BASE": base_sha}
    tmp = os.environ.get("TMPDIR")
    if tmp:
        env["TMPDIR"] = tmp
    return subprocess.run(
        [str(GATE_PROVE), str(worktree), "--", *override],
        capture_output=True, text=True, env=env)


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
    """The gate never writes to the caller's worktree (the prove runs in a scratch worktree it creates and removes), so the caller's tree must be byte-identical to HEAD on every exit path."""
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


def _tree_name(path: str) -> str:
    """Mirror lib/common.sh's tree_name so the test can compare what remote-task would derive from a given path."""
    base = os.path.basename(path)
    sub = re.sub(r"[^A-Za-z0-9_]", "_", base)
    sub = re.sub(r"_+", "_", sub)
    return sub.rstrip("_")


def case_scratch_named_after_caller(failures: list) -> None:
    """The scratch worktree's basename is `<caller>-prove`, not the generic `scratch` that every branch used to share."""
    with tempfile.TemporaryDirectory() as t:
        tmpdir = Path(t)
        # The sentinel must live outside the caller's repo: an untracked
        # file inside it would read as a dirty tree, and we are checking
        # the tree is clean at the end. The scratch runs in a sibling
        # tempdir under TMPDIR, which is the natural place.
        sentinel = tmpdir.parent / f"{tmpdir.name}-scratch-path"
        # Print PWD and exit 0 unconditionally. The first run is the
        # baseline: on the unmodified tree, the gate expects a pass (exit
        # 0 from the snippet), runs the suite, reverts, runs again. Both
        # runs exit 0 -- which the gate reads as "tests do not depend on
        # the change" (exit 1 from gate-prove). Either way the sentinel
        # is written by both runs, and the second run's PWD is the one
        # the gate will revert against. We just need any successful run
        # to leave the sentinel.
        snippet = (
            f"pwd > {sentinel}\n"
            f"exit 0\n"
        )
        base_sha = build_repo(tmpdir, test_post_condition=snippet)
        r = run_prove(tmpdir, base_sha)
        # Either verdict is fine -- the gate ran, that's what we need to
        # have observed the scratch. Exit 1 (suite passes both pre and
        # post) is the expected one for an unconditional exit 0.
        if r.returncode not in (0, 1):
            failures.append(
                f"scratch name: gate exited unexpectedly rc={r.returncode} "
                f"stdout={r.stdout.strip()[:200]!r} "
                f"stderr={r.stderr.strip()[:200]!r}")
        if not sentinel.exists():
            failures.append(
                f"scratch name: sentinel not written; the test command "
                f"never ran: rc={r.returncode} stdout={r.stdout!r}")
        else:
            scratch = sentinel.read_text().strip()
            caller = str(tmpdir)
            caller_basename = os.path.basename(caller.rstrip("/"))
            # The scratch must derive from the caller plus a fixed
            # suffix. The exact suffix is `-prove`, and the rest of the
            # basename is the caller's directory name -- not the generic
            # `scratch` every branch used to share, and not the caller's
            # own basename (which would route remote-task to the
            # caller's bench directory).
            want_basename = f"{caller_basename}-prove"
            got_basename = os.path.basename(scratch)
            if got_basename != want_basename:
                failures.append(
                    f"scratch name: scratch basename {got_basename!r} "
                    f"does not match the caller-derived pattern "
                    f"{want_basename!r} (caller basename "
                    f"{caller_basename!r}, scratch path {scratch!r})")
            # The derived tree_name must differ from the caller's. Two
            # branches whose scratches produce the same tree_name share
            # one remote bench directory and one database, which is the
            # collision this test pins.
            if _tree_name(scratch) == _tree_name(caller):
                failures.append(
                    f"scratch name: scratch's tree_name "
                    f"{_tree_name(scratch)!r} equals the caller's "
                    f"{_tree_name(caller)!r}; remote-task would route "
                    f"this run to the caller's bench directory")
            # The scratch must not collide with the caller's name; that
            # is what makes a subsequent git-worktree-registering
            # consumer see the scratch as a different tree.
            if got_basename == caller_basename:
                failures.append(
                    f"scratch name: scratch basename equals caller "
                    f"basename; the gate's prove would route through "
                    f"the caller's remote cache")
        assert_clean_tree(tmpdir, "scratch name", failures)


def case_prune_stale_registration(failures: list) -> None:
    """A worktree registration left by a SIGKILL'd run must be pruned before the gate adds its scratch."""
    with tempfile.TemporaryDirectory() as t:
        tmpdir = Path(t)

        def git(*args, **kwargs):
            return subprocess.run(["git", "-C", str(tmpdir), *args],
                                  capture_output=True, text=True, **kwargs)

        # Build the repo first so the orphan branch has a commit to point
        # at. The gate does not care about the orphan; it is a setup
        # artifact that survives only until `git worktree prune` runs.
        snippet = ": # always pass\n"
        base_sha = build_repo(tmpdir, test_post_condition=snippet)
        # Add an orphan registration on top of the base. `git worktree
        # add` needs a real ref, so make one and add the worktree against
        # it.
        r = git("branch", "orphan-branch")
        if r.returncode != 0:
            failures.append(
                f"prune stale: setup failed creating orphan branch: "
                f"rc={r.returncode} stderr={r.stderr.strip()!r}")
            return
        orphan = tmpdir.parent / f"{tmpdir.name}-orphan"
        r = git("worktree", "add", "--detach", str(orphan), "orphan-branch")
        if r.returncode != 0:
            failures.append(
                f"prune stale: setup failed adding orphan worktree: "
                f"rc={r.returncode} stderr={r.stderr.strip()!r}")
            return
        # Simulate the SIGKILL: the directory is gone, the registration
        # is not. `git worktree list` shows it as a ghost until something
        # runs `git worktree prune`.
        shutil.rmtree(orphan)
        before = git("worktree", "list", "--porcelain").stdout
        if "orphan" not in before:
            failures.append(
                "prune stale: setup failed; orphan registration was not "
                "visible to `git worktree list` before the gate ran")
            return

        # The prove itself need not succeed -- we are checking that the
        # orphan registration is gone afterwards, not the verdict.
        r = run_prove(tmpdir, base_sha)

        after = git("worktree", "list", "--porcelain").stdout
        if "orphan" in after:
            failures.append(
                f"prune stale: orphan registration survived the gate: "
                f"`git worktree list --porcelain` after the run still "
                f"names it:\n{after}\n(gate rc={r.returncode} "
                f"stderr={r.stderr.strip()[:200]!r})")
        assert_clean_tree(tmpdir, "prune stale", failures)


def case_override_runs_against_scratch(failures: list) -> None:
    """A caller-supplied `-- test command` override must run inside the scratch, not the caller's worktree, exactly the way the manifest path does."""
    with tempfile.TemporaryDirectory() as t:
        tmpdir = Path(t)
        # A `grep` against src.sh from cwd: this is what the manifest
        # path already does, but the override runs it from wherever the
        # gate decides. Pinning cwd matters because the bug is exactly
        # that the gate decided "the caller's tree".
        snippet = (
            "grep -q '^BRANCH_VALUE=2$' src.sh "
            "|| { echo 'src.sh is not the branch version'; exit 1; }\n"
        )
        base_sha = build_repo(tmpdir, test_post_condition=snippet)
        r = run_prove_with_override(
            tmpdir, base_sha,
            override=["sh", "-c",
                      "grep -q '^BRANCH_VALUE=2$' src.sh"])
        # The verdict is the same case as case_passes_then_fails
        # (exit 0 -- tests depend on the change). What makes this test
        # distinct is the command path: an override command the gate
        # ran as `"$@"` in the current directory. With the fix the
        # override runs in the scratch and exits 1 on the reverted
        # source, so the gate returns 0; without the fix the override
        # runs in the caller and the gate returns 1.
        assert_exit("override runs against scratch", 0, r, failures)
        if "tests depend on the change" not in (
                r.stderr + r.stdout).lower():
            failures.append(
                f"override runs against scratch: exit 0 reached, but "
                f"verdict line missing: stdout={r.stdout.strip()!r}")
        assert_clean_tree(tmpdir, "override runs against scratch",
                          failures)


def case_kill_preserves_tree(failures: list) -> None:
    """A killed gate-prove must leave the caller's worktree byte-identical to HEAD: no source reverted, no diff staged, no scratch worktree polluting the working tree."""
    for label, sig in [("SIGTERM mid-suite", signal.SIGTERM),
                       ("SIGKILL mid-suite", signal.SIGKILL)]:
        with tempfile.TemporaryDirectory() as t:
            tmpdir = Path(t)
            # The sentinel must live outside the caller's repo: the
            # caller's tree is the post-condition under test, and an
            # untracked file inside it would read as a dirty tree.
            # A sibling directory of tmpdir is the natural choice --
            # the test command runs from a scratch worktree under
            # /tmp, and absolute paths reach it the same way.
            sentinel = tmpdir.parent / f"{tmpdir.name}-suite-started"
            # Only the post-revert run sleeps; the pre-run exits 0
            # immediately, which the gate needs for its baseline
            # check. Detecting "src.sh reads BASE_VALUE" tells the
            # test the gate is past the revert, in the window where
            # a kill would leave the caller's tree half-reverted
            # under the broken design.
            snippet = (
                f"if grep -q '^BASE_VALUE=1$' src.sh; then\n"
                f"  touch {sentinel}\n"
                f"  sleep 30\n"
                f"fi\n"
                f"exit 0\n"
            )
            base_sha = build_repo(tmpdir, test_post_condition=snippet)
            # TMPDIR is forwarded alongside PROVE_BASE: the gate's
            # scratch has to land in the same private temp dir as the
            # sentinel, both for the safety rule that forbids /tmp and
            # because the scratch path the gate derives for any
            # downstream consumer (remote-task, say) needs to be the
            # one under TMPDIR, not the global /tmp.
            env = {"PATH": "/usr/bin:/bin",
                   "HOME": str(Path.home()),
                   "PROVE_BASE": base_sha}
            tmp = os.environ.get("TMPDIR")
            if tmp:
                env["TMPDIR"] = tmp
            proc = subprocess.Popen(
                [str(GATE_PROVE), str(tmpdir)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
                # New session so the test can SIGKILL the gate's
                # whole process group (bash + foreground sleep +
                # any children) without taking the test runner with
                # it. A bare subprocess shares its parent's group,
                # and killpg on that group would kill us.
                preexec_fn=os.setsid,
                env=env)
            try:
                # The sentinel is the only synchronisation point that
                # does not depend on the gate's internal timing. A
                # 30s deadline is generous: the test command starts
                # inside the first suite run, well before the
                # post-revert one.
                deadline = time.time() + 30
                while not sentinel.exists():
                    if proc.poll() is not None:
                        out, err = proc.communicate()
                        failures.append(
                            f"{label}: gate exited before the suite "
                            f"started: rc={proc.returncode} "
                            f"stdout={out!r} stderr={err!r}")
                        break
                    if time.time() > deadline:
                        proc.kill()
                        proc.communicate()
                        failures.append(
                            f"{label}: sentinel never appeared: the "
                            f"suite was never reached")
                        break
                    time.sleep(0.05)
                else:
                    # Signal sent: the caller's tree must be untouched
                    # right now and must remain untouched after the
                    # gate dies. Both checks pin the property the
                    # issue names -- byte-identical to HEAD.
                    proc.send_signal(sig)
                    # Give the signal time to land and any handler in
                    # bash a chance to fire. The trap the gate set on
                    # TERM cannot run while the foreground command is
                    # sleeping (bash defers it), so the SIGTERM case
                    # reads the tree before bash has acted on the
                    # signal at all. The scratch worktree is what
                    # protects the caller in that window.
                    time.sleep(0.5)
                    failures.extend(
                        _assert_caller_tree_untouched(tmpdir, label))
                    # Now drain the subprocess. SIGKILL exits
                    # immediately; SIGTERM's trap runs only after the
                    # foreground command finishes, so the gate stays
                    # alive until the 30s sleep ends or until we
                    # clean it up. Either way, the post-condition
                    # already checked above is what the test pins.
                    if proc.poll() is None:
                        # SIGKILL the gate's own process group so
                        # bash, its foreground sleep, and any other
                        # children die together. The gate is started
                        # in a new session (preexec_fn=os.setsid) so
                        # the test's own group is unaffected.
                        try:
                            os.killpg(os.getpgid(proc.pid),
                                      signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            proc.kill()
                        try:
                            proc.communicate(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.communicate()
                    # Re-check the tree after the gate is gone, in
                    # case anything the SIGTERM trap ran (or did not
                    # run, since the foreground command was blocking)
                    # wrote to the caller's tree.
                    failures.extend(
                        _assert_caller_tree_untouched(
                            tmpdir, label + " (after exit)"))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()


def _assert_caller_tree_untouched(worktree: Path, label: str) -> list:
    """The byte-identical-to-HEAD check the issue asks for."""
    failures = []
    r = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True, text=True, check=True)
    if r.stdout.strip():
        failures.append(
            f"{label}: worktree not clean: {r.stdout!r}")
    r = subprocess.run(
        ["git", "-C", str(worktree), "diff", "HEAD", "--stat"],
        capture_output=True, text=True, check=True)
    if r.stdout.strip():
        failures.append(
            f"{label}: worktree differs from HEAD: {r.stdout!r}")
    return failures


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
    case_scratch_named_after_caller(failures)
    case_prune_stale_registration(failures)
    case_override_runs_against_scratch(failures)
    case_kill_preserves_tree(failures)

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS gate-prove: red->3, pass-then-fail->0, pass-then-pass->1,\n     missing command->3, nothing to revert->3 without running the suite\n     --override->0 against the scratch")
    return 0


if __name__ == "__main__":
    sys.exit(main())