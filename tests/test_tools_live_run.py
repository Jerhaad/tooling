#!/usr/bin/env python3
"""Pin the live-checkout refresh behaviour against a throwaway repo pair.

The bug this covers is a refresh that rewrites a checkout someone is running
out of: a feature branch left on the live checkout, a dirty tree wiped to
origin/main, a refresher that ran in the background while a job was still
using the bytes. Each test stands up a real `origin` (bare) and a real `live`
(non-bare) clone, runs the wrapper, and reads the live checkout back. A
fixture that only ever sees input the test synthesised cannot catch a wrapper
that only handles synthesised input.

Run directly: `python3 tests/test_tools_live_run.py`. No runner dependency.
"""
import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_RUN = REPO_ROOT / "bin" / "tools-live-run"


def git(cwd: Path, *args: str, check: bool = True,
        env: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke `git` with explicit -C and a fresh env that does not leak the
    caller's GIT_DIR / GIT_WORK_TREE: a tmpdir clone picking them up is a
    clone pointing at the wrapper's checkout, not at itself."""
    full_env = {k: v for k, v in os.environ.items()
                if not k.startswith("GIT_")}
    full_env.update(env or {})
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True,
                          env=full_env, check=check)


def build_pair(tmp: Path) -> tuple[Path, Path, Path]:
    """Create a bare `origin`, clone it to `live`, return (origin, live, lock).

    The lock lives inside the tmpdir so two test runs cannot contend, and the
    test sets LIVE_LOCK to it before invoking the wrapper.
    """
    origin = tmp / "origin.git"
    live = tmp / "live"
    lock = tmp / "live.lock"

    git(tmp, "init", "--bare", str(origin), "--initial-branch=main")
    git(tmp, "clone", str(origin), str(live))
    git(live, "symbolic-ref", "HEAD", "refs/heads/main")
    git(live, "config", "user.email", "test@example.com")
    git(live, "config", "user.name", "Test")
    # An empty commit so origin/main exists; an unanchored fetch on a fresh
    # clone leaves HEAD unborn and `git reset --hard origin/main` errors out.
    git(live, "commit", "--allow-empty", "-m", "initial")
    git(live, "push", "origin", "main")
    # pipeline/ holds the test pipeline script, committed and pushed so the
    # wrapper can resolve $LIVE/pipeline/<name> to a file origin/main has.
    (live / "pipeline").mkdir()
    (live / "pipeline" / "marker.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo \"marker ran in $(pwd) with args: $@\"\n"
        f"echo \"live sha was $(git -C {live} rev-parse HEAD)\"\n"
        f"touch '{tmp}/marker.ran'\n"
    )
    (live / "pipeline" / "marker.sh").chmod(0o755)
    git(live, "add", "-A")
    git(live, "commit", "-m", "add marker pipeline")
    git(live, "push", "origin", "main")

    return origin, live, lock


def advance_origin(origin: Path, live: Path) -> tuple[str, str]:
    """Push a new commit to origin/main and return (old_sha, new_sha). The
    live checkout is left at old_sha, so the next run has work to do."""
    old = git(live, "rev-parse", "HEAD").stdout.strip()
    # Write a new file in the live checkout, push from there so origin grows
    # by one commit and live is still at the older sha.
    (live / "adv.txt").write_text("advanced\n")
    git(live, "add", "adv.txt")
    git(live, "commit", "-m", "advance")
    git(live, "push", "origin", "main")
    new = git(live, "rev-parse", "origin/main").stdout.strip()
    git(live, "reset", "--hard", old)
    return old, new


def run_wrapper(live: Path, lock: Path, *args: str,
               marker: Path | None = None) -> subprocess.CompletedProcess:
    """Invoke tools-live-run with LIVE_REPO/LIVE_LOCK set. marker is a file
    whose existence (or absence) tells us whether the pipeline ran -- created
    on entry by the wrapper so a failed run leaves it absent."""
    if marker is not None:
        marker.unlink(missing_ok=True)
    env = {**os.environ, "LIVE_REPO": str(live), "LIVE_LOCK": str(lock)}
    return subprocess.run(["bash", str(LIVE_RUN), *args],
                          capture_output=True, text=True, env=env)


def live_sha(live: Path) -> str:
    return git(live, "rev-parse", "HEAD").stdout.strip()


def assert_(cond: bool, msg: str, failures: list) -> None:
    if not cond:
        failures.append(msg)


def test_clean_refreshes_and_runs(tmp: Path, failures: list) -> None:
    origin, live, lock = build_pair(tmp)
    old, new = advance_origin(origin, live)
    assert_(old != new, "advance did not move origin/main", failures)

    marker = tmp / "marker.ran"
    proc = run_wrapper(live, lock, "marker.sh", "x", "y", marker=marker)

    assert_(proc.returncode == 0, f"wrapper exited {proc.returncode}: {proc.stderr}", failures)
    assert_("refreshed" in proc.stdout,
            f"expected 'refreshed' in stdout, got: {proc.stdout!r}", failures)
    assert_(live_sha(live) == new,
            f"live did not advance to origin/main: live={live_sha(live)[:12]} new={new[:12]}",
            failures)
    assert_(marker.exists(), "pipeline script did not run (marker missing)", failures)
    # The wrapper's refresh message must name both shas -- "a job that silently
    # ran different code than the last one is the whole complaint in the issue".
    assert_(old[:12] in proc.stdout and new[:12] in proc.stdout,
            f"refresh line missing old/new sha: {proc.stdout!r}", failures)


def test_dirty_refused_and_nothing_reset(tmp: Path, failures: list) -> None:
    origin, live, lock = build_pair(tmp)
    advance_origin(origin, live)
    pre = live_sha(live)
    (live / "untracked.txt").write_text("oops\n")
    marker = tmp / "marker.ran"

    proc = run_wrapper(live, lock, "marker.sh", marker=marker)

    assert_(proc.returncode == 3,
            f"dirty live did not refuse (exit {proc.returncode}): {proc.stderr}",
            failures)
    # The complaint must name what is wrong -- the issue says "refuse by name,
    # and say which" -- otherwise the operator chasing a missed refresh cannot
    # tell whether they should commit or clean.
    assert_("dirty" in (proc.stderr + proc.stdout).lower(),
            f"refusal did not name 'dirty': {proc.stderr!r}", failures)
    assert_(live_sha(live) == pre, "live sha changed despite dirty refusal", failures)
    assert_(not marker.exists(), "pipeline ran despite dirty refusal", failures)


def test_on_other_branch_refused(tmp: Path, failures: list) -> None:
    origin, live, lock = build_pair(tmp)
    advance_origin(origin, live)
    pre = live_sha(live)
    git(live, "checkout", "-q", "-b", "feature")
    marker = tmp / "marker.ran"

    proc = run_wrapper(live, lock, "marker.sh", marker=marker)

    git(live, "checkout", "-q", "main")
    assert_(proc.returncode == 3,
            f"off-main live did not refuse (exit {proc.returncode}): {proc.stderr}",
            failures)
    assert_("main" in (proc.stderr + proc.stdout).lower(),
            f"refusal did not name the branch fault: {proc.stderr!r}", failures)
    assert_(live_sha(live) == pre, "live sha changed despite off-main refusal", failures)
    assert_(not marker.exists(), "pipeline ran despite off-main refusal", failures)


def test_lock_held_skips_refresh(tmp: Path, failures: list) -> None:
    origin, live, lock = build_pair(tmp)
    old, new = advance_origin(origin, live)
    pre = live_sha(live)
    assert_(pre == old and old != new,
            f"setup wrong: pre={pre[:12]} old={old[:12]} new={new[:12]}", failures)

    # Holder takes the same lock the wrapper does and holds it for the
    # duration of the wrapper's run. A separate process is the only honest
    # test: an in-process flock lets the wrapper's fork see its own lock
    # depending on fd inheritance.
    holder_src = textwrap.dedent(f"""
             import fcntl, sys, time
             with open({str(lock)!r}, 'w') as f:
                 fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                 print('locked', flush=True)
                 time.sleep(5)
         """)
    holder = subprocess.Popen(
        ["python3", "-c", holder_src],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # Wait for the holder to report it has the lock before we race it.
    holder_stdout = holder.stdout
    assert holder_stdout is not None
    line = holder_stdout.readline()
    # Build the failure message WITHOUT evaluating holder.stderr.read() unless
    # the holder failed to print 'locked' -- that read would block until the
    # holder exits, releasing the lock it is supposed to be holding.
    if "locked" not in line:
        holder_stderr = holder.stderr
        assert holder_stderr is not None
        failures.append(
            f"holder never took the lock: stdout={line!r} "
            f"stderr={holder_stderr.read()!r}")

    marker = tmp / "marker.ran"
    proc = run_wrapper(live, lock, "marker.sh", "x", marker=marker)

    holder.wait(timeout=10)

    assert_(proc.returncode == 0,
            f"wrapper exited {proc.returncode} on held lock: stderr={proc.stderr!r}",
            failures)
    assert_("refresh skipped" in proc.stdout or "skipped" in proc.stdout.lower(),
            f"wrapper did not announce the skip: stdout={proc.stdout!r}", failures)
    assert_(live_sha(live) == pre,
            f"live moved despite held lock: was {pre[:12]} now {live_sha(live)[:12]}",
            failures)
    # The pipeline must still run -- "carries on with what is there".
    assert_(marker.exists(), "pipeline did not run despite shared lock acquired", failures)


def test_unreachable_origin_still_runs(tmp: Path, failures: list) -> None:
    """A fetch that fails degrades to the tree as it stands. A scheduled
    job running one commit stale is a far better outcome than a scheduled
    job that does not run, and the wrapper has to say which it did."""
    origin, live, lock = build_pair(tmp)
    marker = tmp / "marker.ran"
    # Move the remote out from under the clone: fetch now fails, and the
    # refresh has nothing to reset to.
    origin.rename(tmp / "origin.gone")

    proc = run_wrapper(live, lock, "marker.sh", marker=marker)

    assert_(proc.returncode == 0,
            f"unreachable origin: wrapper exited {proc.returncode}: {proc.stdout}{proc.stderr}",
            failures)
    assert_(marker.exists(),
            "unreachable origin: pipeline did not run; a fetch failure must "
            "not stop the job", failures)
    assert_("refresh skipped" in proc.stdout,
            f"unreachable origin: did not say the refresh was skipped: {proc.stdout!r}",
            failures)


def test_bare_name_resolves(tmp: Path, failures: list) -> None:
    """Cron entries name the script without its suffix. Every path that
    reaches the pipeline has to resolve it the same way, including the
    one taken when the refresh could not happen."""
    origin, live, lock = build_pair(tmp)
    marker = tmp / "marker.ran"

    proc = run_wrapper(live, lock, "marker", marker=marker)
    assert_(proc.returncode == 0,
            f"bare name: wrapper exited {proc.returncode}: {proc.stdout}{proc.stderr}",
            failures)
    assert_(marker.exists(), "bare name: pipeline did not run", failures)

    # The same bare name on the degraded path.
    origin.rename(tmp / "origin.gone")
    proc = run_wrapper(live, lock, "marker", marker=marker)
    assert_(proc.returncode == 0,
            f"bare name, fetch failed: wrapper exited {proc.returncode}: "
            f"{proc.stdout}{proc.stderr}", failures)
    assert_(marker.exists(),
            "bare name, fetch failed: pipeline did not run", failures)


def test_refusal_reaches_stdout(tmp: Path, failures: list) -> None:
    """cron delivers stdout and drops the rest, so a refusal printed on
    stderr is six scheduled jobs going quiet with no signal."""
    origin, live, lock = build_pair(tmp)
    (live / "pipeline" / "marker.sh").write_text("#!/usr/bin/env bash\ntrue\n")

    proc = run_wrapper(live, lock, "marker.sh")
    assert_(proc.returncode == 3,
            f"dirty tree: expected exit 3, got {proc.returncode}", failures)
    assert_("dirty" in proc.stdout,
            f"dirty tree: refusal not on stdout, where cron can deliver it: "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}", failures)


def main() -> int:
    failures: list = []
    cases = [
        ("clean refreshes and runs", test_clean_refreshes_and_runs),
        ("dirty refused, nothing reset", test_dirty_refused_and_nothing_reset),
        ("off-main branch refused", test_on_other_branch_refused),
        ("lock held skips refresh, pipeline runs", test_lock_held_skips_refresh),
        ("unreachable origin still runs the job", test_unreachable_origin_still_runs),
        ("bare script name resolves on every path", test_bare_name_resolves),
        ("refusal reaches stdout", test_refusal_reaches_stdout),
    ]

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for label, fn in cases:
            try:
                case_dir = tmp / label.replace(" ", "_")
                case_dir.mkdir()
                fn(case_dir, failures)
            except Exception as exc:                       # noqa: BLE001
                failures.append(f"{label}: {type(exc).__name__}: {exc}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASS ({len(cases)} case{'s' if len(cases) != 1 else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())