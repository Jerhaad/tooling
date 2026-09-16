#!/usr/bin/env python3
"""Pin the gap-progress behaviour triage-on-merge must give.

A run killed by the 3600s cap never advanced state, so the next run faced the
same gap and died the same way. State now advances only on the triager's
completion sentinel, and the ledger carries what a stopped run finished.

Each case runs the real script and the real triager with $PIPELINE_TRIAGER
swapped for a stub that can abort mid-gap.
"""
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "pipeline" / "triage-on-merge.sh"
TRIAGER = REPO_ROOT / "bin" / "hermes-triage"


def git(cwd: Path, *args: str, check: bool = True,
        ) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True, text=True, check=check)


def scenario_key(repo: Path, since: str) -> str:
    """Stable string the stub uses to look up its scenario. Joins with a
    separator a path or sha cannot contain, so two scenarios keyed by
    different inputs cannot collide."""
    return f"{repo}|{since}"


def make_origin(tmpdir: Path) -> tuple[Path, Path, str, str]:
    """Build a bare repo with two commits on main and a work tree whose
    `origin` remote points at it. Returns (origin_path, work_path, old_sha,
    new_sha) so the test can set PIPELINE_REPO to the work tree (what the
    real pipeline drives) and have the script fetch from origin (the bare
    repo, where the trunk lives)."""
    tmpdir.mkdir(parents=True, exist_ok=True)
    origin = tmpdir / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--quiet", "--initial-branch=main")
    work = tmpdir / "seed"
    work.mkdir()
    git(work, "init", "--quiet", "-b", "main")
    for k, v in [("user.email", "t@t"), ("user.name", "t")]:
        git(work, "config", k, v)
    (work / "README").write_text("seed\n")
    git(work, "add", "README")
    git(work, "commit", "--quiet", "-m", "first")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "--quiet", "origin", "main")
    old_sha = git(work, "rev-parse", "HEAD").stdout.strip()
    (work / "README").write_text("seed\nsecond\n")
    git(work, "commit", "--quiet", "-am", "second")
    git(work, "push", "--quiet", "origin", "main")
    new_sha = git(work, "rev-parse", "HEAD").stdout.strip()
    return origin, work, old_sha, new_sha


def write_config(env_file: Path, repo: Path, triager: Path,
                 state_dir: Path) -> None:
    """Write the AGENT_PIPELINE_ENV file common.sh sources. Every variable
    common.sh reads must be present and non-empty: the script uses `set -u`
    and refuses with `:?` on missing values.

    HERMES_TRIAGE_QUEUE is read by bin/hermes-triage (not common.sh), so the
    script's common.sh-derived QUEUE and the triager's HERMES_TRIAGE_QUEUE
    both have to point at the same place or the triage files would land
    somewhere neither side looks at.
    """
    queue = state_dir / "queue"
    env_file.write_text(
        f"export PIPELINE_REPO={shlex.quote(str(repo))}\n"
        f"export PIPELINE_GH_REPO=owner/repo\n"
        f"export PIPELINE_AGENT=/bin/true\n"
        f"export PIPELINE_VERIFY=/bin/true\n"
        f"export PIPELINE_IMPLEMENTER=/bin/true\n"
        f"export PIPELINE_TRIAGER={shlex.quote(str(triager))}\n"
        f"export PIPELINE_BRANCH_SWEEP=/bin/true\n"
        f"export PIPELINE_STATE_DIR={shlex.quote(str(state_dir))}\n"
        f"export PIPELINE_QUEUE={shlex.quote(str(queue))}\n"
        f"export HERMES_TRIAGE_QUEUE={shlex.quote(str(queue))}\n"
    )


def write_stub_triager(stub: Path, scenarios: dict) -> None:
    """Write a stub $PIPELINE_TRIAGER that replays a recorded scenario.

    `scenarios` is a dict keyed by the issue-list invocation; each value is
    a dict describing what to write and when to abort. The stub writes the
    same `$QUEUE/issue-N.md` files the real triager would, prints their
    paths on stdout, appends each to the ledger, and touches the sentinel at
    the end -- exactly the contract the script now depends on.

    A scenario with `kill_after: K` aborts the run (exits 137) after K
    issues have been "triaged", so the test can simulate the timeout case
    without a 3600s wait.
    """
    import json
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"export SCENARIOS={shlex.quote(json.dumps(scenarios))}\n"
        "python3 - \"$@\" <<'PYEOF'\n"
        "import json, os, sys, signal\n"
        "scenarios = json.loads(os.environ['SCENARIOS'])\n"
        "args = sys.argv[1:]\n"
        "repo = None\n"
        "since = None\n"
        "ledger = None\n"
        "i = 0\n"
        "while i < len(args):\n"
        "    if args[i] == '--repo':\n"
        "        repo = args[i+1]; i += 2\n"
        "    elif args[i] == '--since-merge':\n"
        "        since = args[i+1]; i += 2\n"
        "    elif args[i] == '--ledger':\n"
        "        ledger = args[i+1]; i += 2\n"
        "    else:\n"
        "        i += 1\n"
        "key = f'{repo}|{since}'\n"
        "if key not in scenarios:\n"
        "    sys.exit(f'no scenario for {key}')\n"
        "s = scenarios[key]\n"
        "queue = os.environ['HERMES_TRIAGE_QUEUE']\n"
        "os.makedirs(queue, exist_ok=True)\n"
        "# Line-buffered: SIGTERM between prints would otherwise leave the\n"
        "# command-substitution pipe holding unflushed bytes, and the\n"
        "# script would read `$written` as empty.\n"
        "sys.stdout.reconfigure(line_buffering=True)\n"
        "already = set()\n"
        "if ledger and os.path.exists(ledger):\n"
        "    with open(ledger) as f:\n"
        "        already = {ln.strip() for ln in f if ln.strip()}\n"
        "for idx, n in enumerate(s['issues']):\n"
        "    if str(n) in already:\n"
        "        continue\n"
        "    out = f'{queue}/issue-{n}.md'\n"
        "    with open(out, 'w') as f:\n"
        "        f.write(f'**Verdict:** triage of {n}\\n')\n"
        "    print(out, flush=True)\n"
        "    if ledger:\n"
        "        with open(ledger, 'a') as f:\n"
        "            f.write(f'{n}\\n')\n"
        "    if s.get('kill_after') is not None and idx + 1 >= s['kill_after']:\n"
        "        # SIGTERM is what timeout sends; 128+15=143 is the exit\n"
        "        # the wrapper would see if `|| true` weren't masking it.\n"
        "        os.kill(os.getpid(), signal.SIGTERM)\n"
        "        # SIGTERM is the default action -- the script exits 143\n"
        "        # before reaching the sentinel touch.\n"
        "if ledger:\n"
        "    open(ledger + '.done', 'w').close()\n"
        "PYEOF\n"
    )
    stub.chmod(0o755)


def run_script(tmpdir: Path, env_file: Path) -> subprocess.CompletedProcess:
    """Invoke pipeline/triage-on-merge.sh with AGENT_PIPELINE_ENV pointed
    at the test's env file. Home is overridden so common.sh's defaults do
    not reach a real state directory."""
    env = os.environ.copy()
    env["AGENT_PIPELINE_ENV"] = str(env_file)
    env["HOME"] = str(tmpdir / "home")
    env["PATH"] = "/usr/bin:/bin"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True, text=True, env=env)


def state_files(state_dir: Path) -> dict:
    """Return the on-disk state after a run: the SHA file, the ledger, and
    the done sentinel, all as their textual or None form. Lets a test
    assert what was kept and what was cleared in one expression."""
    state = state_dir / "triage-last-trunk"
    ledger = state_dir / "triage-issues"
    done = state_dir / "triage-issues.done"
    return {
        "state": state.read_text().strip() if state.exists() else None,
        "ledger": ledger.read_text().splitlines()
                 if ledger.exists() else None,
        "done": done.exists(),
    }


def assert_eq(label: str, want, got, failures: list) -> None:
    if want != got:
        failures.append(f"{label}: want {want!r}, got {got!r}")


def case_first_run(failures: list, tmpdir: Path) -> None:
    """No state file: the script records $HEAD_NOW and exits silently.
    Triaging the whole queue from a standing start is hours of model time
    nobody asked for, so the first run is a no-op beyond seeding the state.
    A stale ledger from a previous repository must not survive: the next
    gap would otherwise skip issues it never triaged in this one."""
    origin, _, _, new_sha = make_origin(tmpdir / "first")
    work = tmpdir / "first" / "seed"
    state_dir = tmpdir / "first" / "state"
    state_dir.mkdir()
    queue = state_dir / "queue"
    queue.mkdir()
    # Plant a stale ledger so the test can see it cleared.
    (state_dir / "triage-issues").write_text("5\n7\n12\n")
    (state_dir / "triage-issues.done").write_text("")
    env_file = tmpdir / "first" / "env"
    triager = tmpdir / "first" / "triager"
    write_config(env_file, work, triager, state_dir)
    write_stub_triager(triager, {})  # must not be invoked

    r = run_script(tmpdir / "first", env_file)
    assert_eq("first run: exit code", 0, r.returncode, failures)
    assert_eq("first run: stdout silent", "", r.stdout.strip(), failures)
    s = state_files(state_dir)
    assert_eq("first run: state records HEAD_NOW", new_sha, s["state"],
              failures)
    assert_eq("first run: stale ledger cleared", None, s["ledger"], failures)
    assert_eq("first run: stale sentinel cleared", False, s["done"], failures)


def case_trunk_unchanged(failures: list, tmpdir: Path) -> None:
    """State file names the current HEAD_NOW. Nothing changed, nothing to
    say. A script that prints here would page the operator every cron tick."""
    origin, work, old_sha, new_sha = make_origin(tmpdir / "still")
    state_dir = tmpdir / "still" / "state"
    state_dir.mkdir()
    queue = state_dir / "queue"
    queue.mkdir()
    (state_dir / "triage-last-trunk").write_text(new_sha + "\n")
    env_file = tmpdir / "still" / "env"
    triager = tmpdir / "still" / "triager"
    write_config(env_file, work, triager, state_dir)
    write_stub_triager(triager, {})  # must not be invoked

    r = run_script(tmpdir / "still", env_file)
    assert_eq("trunk unchanged: exit code", 0, r.returncode, failures)
    assert_eq("trunk unchanged: stdout silent", "", r.stdout.strip(),
              failures)
    s = state_files(state_dir)
    assert_eq("trunk unchanged: state untouched", new_sha, s["state"],
              failures)


def case_complete_run(failures: list, tmpdir: Path) -> None:
    """Trunk moved, candidates exist, run completes naturally. State must
    advance and the ledger must be cleared -- the next run starts a fresh
    gap. The re-triaged summary appears on stdout; the verdict line of
    each file is read so an operator can scan it."""
    origin, work, old_sha, new_sha = make_origin(tmpdir / "full")
    state_dir = tmpdir / "full" / "state"
    state_dir.mkdir()
    queue = tmpdir / "full" / "queue"
    queue.mkdir()
    (state_dir / "triage-last-trunk").write_text(old_sha + "\n")
    env_file = tmpdir / "full" / "env"
    triager = tmpdir / "full" / "triager"
    write_config(env_file, work, triager, state_dir)

    # The script fetches into $REPO (work) from origin (the bare repo),
    # so the stub's scenario key is keyed by the path the script passes as
    # --repo, which is $REPO.
    scenarios = {
        scenario_key(work, old_sha): {"issues": [5, 7, 12]},
    }
    write_stub_triager(triager, scenarios)

    r = run_script(tmpdir / "full", env_file)
    assert_eq("complete run: exit code", 0, r.returncode, failures)
    if "origin/main moved" not in r.stdout:
        failures.append(
            f"complete run: missing summary header: stdout={r.stdout!r}")
    for n in (5, 7, 12):
        if f"issue-{n}.md" not in r.stdout:
            failures.append(
                f"complete run: summary missing issue {n}: stdout={r.stdout!r}")
    s = state_files(state_dir)
    assert_eq("complete run: state advances to HEAD_NOW",
              new_sha, s["state"], failures)
    assert_eq("complete run: ledger cleared", None, s["ledger"], failures)
    assert_eq("complete run: sentinel cleared", False, s["done"], failures)


def case_complete_run_no_candidates(failures: list, tmpdir: Path) -> None:
    """Trunk moved but no issue body names a path in the diff. The gap
    closes silently and state advances: there is nothing the next run
    could add by waiting, so leaving it open would only delay the next
    legitimate re-triage by one cron tick per silent day.

    The stub completes naturally with no work and touches the sentinel,
    because the real triager's empty-paths branch does the same: the
    candidate loop is just skipped, but the natural-completion signal
    still fires."""
    origin, work, old_sha, new_sha = make_origin(tmpdir / "none")
    state_dir = tmpdir / "none" / "state"
    state_dir.mkdir()
    queue = tmpdir / "none" / "queue"
    queue.mkdir()
    (state_dir / "triage-last-trunk").write_text(old_sha + "\n")
    env_file = tmpdir / "none" / "env"
    triager = tmpdir / "none" / "triager"
    write_config(env_file, work, triager, state_dir)
    # Empty issue list -- the triager runs but does nothing, and reaches
    # the natural-completion touch.
    write_stub_triager(triager,
                       {scenario_key(work, old_sha): {"issues": []}})

    r = run_script(tmpdir / "none", env_file)
    assert_eq("no candidates: exit code", 0, r.returncode, failures)
    assert_eq("no candidates: stdout silent", "", r.stdout.strip(),
              failures)
    s = state_files(state_dir)
    assert_eq("no candidates: state advances to HEAD_NOW",
              new_sha, s["state"], failures)


def case_killed_mid_run(failures: list, tmpdir: Path) -> None:
    """The bug the issue names: a run killed after 2 of 3 issues leaves
    the ledger with those two, state unchanged, and stdout showing what
    finished. The next run must NOT re-triage 5 and 7, and must finish 12.
    This is the case the old design got wrong: state advanced, so the
    next run started over."""
    origin, work, old_sha, new_sha = make_origin(tmpdir / "kill")
    state_dir = tmpdir / "kill" / "state"
    state_dir.mkdir()
    queue = tmpdir / "kill" / "queue"
    queue.mkdir()
    (state_dir / "triage-last-trunk").write_text(old_sha + "\n")
    env_file = tmpdir / "kill" / "env"
    triager = tmpdir / "kill" / "triager"
    write_config(env_file, work, triager, state_dir)

    # First run: kills after 2 of 3.
    scenarios1 = {
        scenario_key(work, old_sha): {"issues": [5, 7, 12], "kill_after": 2},
    }
    write_stub_triager(triager, scenarios1)

    r1 = run_script(tmpdir / "kill", env_file)
    assert_eq("killed: first-run exit code", 0, r1.returncode, failures)
    s1 = state_files(state_dir)
    assert_eq("killed: state unchanged", old_sha, s1["state"], failures)
    if s1["ledger"] is None or sorted(s1["ledger"]) != ["5", "7"]:
        failures.append(
            f"killed: ledger should name the two completed issues: "
            f"got {s1['ledger']!r}")
    assert_eq("killed: sentinel absent", False, s1["done"], failures)
    # Stdout should show the two completed, not the third.
    if "issue-5.md" not in r1.stdout or "issue-7.md" not in r1.stdout:
        failures.append(
            f"killed: stdout missing completed issues: {r1.stdout!r}")
    if "issue-12.md" in r1.stdout:
        failures.append(
            f"killed: stdout mentions an issue that was never triaged: "
            f"{r1.stdout!r}")

    # Resume: the stub now sees the ledger already has them. The triager
    # must skip them, finish 12, touch the sentinel, and the script must
    # advance state and clear the ledger.
    scenarios2 = {
        scenario_key(work, old_sha): {"issues": [5, 7, 12]},
    }
    write_stub_triager(triager, scenarios2)

    r2 = run_script(tmpdir / "kill", env_file)
    assert_eq("resume: exit code", 0, r2.returncode, failures)
    s2 = state_files(state_dir)
    assert_eq("resume: state advances to HEAD_NOW", new_sha, s2["state"],
              failures)
    assert_eq("resume: ledger cleared", None, s2["ledger"], failures)
    assert_eq("resume: sentinel cleared", False, s2["done"], failures)
    # The resume run triaged only 12 (5 and 7 were skipped). The summary
    # must show 12, not the ones the ledger skipped.
    if "issue-12.md" not in r2.stdout:
        failures.append(
            f"resume: summary missing the only newly-triaged issue: "
            f"{r2.stdout!r}")
    if "issue-5.md" in r2.stdout or "issue-7.md" in r2.stdout:
        failures.append(
            f"resume: summary re-listed issues the ledger had: "
            f"{r2.stdout!r}")


def case_ledger_substring_does_not_skip(failures: list, tmpdir: Path) -> None:
    """The skip is a per-line exact match. A ledger naming issue 120 must
    not cause issue 12 to be skipped: the grep uses `-x` so the substring
    never matches. Pin it, because the obvious implementation uses a
    substring test and silently drops an issue per matching prefix."""
    origin, work, old_sha, new_sha = make_origin(tmpdir / "substr")
    state_dir = tmpdir / "substr" / "state"
    state_dir.mkdir()
    queue = tmpdir / "substr" / "queue"
    queue.mkdir()
    (state_dir / "triage-last-trunk").write_text(old_sha + "\n")
    (state_dir / "triage-issues").write_text("120\n")
    env_file = tmpdir / "substr" / "env"
    triager = tmpdir / "substr" / "triager"
    write_config(env_file, work, triager, state_dir)

    scenarios = {
        scenario_key(work, old_sha): {"issues": [12, 120]},
    }
    write_stub_triager(triager, scenarios)

    r = run_script(tmpdir / "substr", env_file)
    assert_eq("substring: exit code", 0, r.returncode, failures)
    s = state_files(state_dir)
    # Issue 12 must appear: the ledger's "120" must not have caused 12 to
    # be skipped. Whether 120 itself appears depends on whether the stub
    # decided to re-triage it; pinning only the substring here is the
    # contract -- the ledger's skip is exact-line, not prefix.
    if "issue-12.md" not in r.stdout:
        failures.append(
            f"substring: ledger's '120' caused 12 to be skipped: "
            f"{r.stdout!r}")
    assert_eq("substring: state advances to HEAD_NOW",
              new_sha, s["state"], failures)


def advance_trunk(work: Path) -> str:
    """Push one more commit to the origin the test built, so a second
    script invocation sees a moved trunk. Returns the new HEAD sha."""
    readme = work / "README"
    readme.write_text(readme.read_text() + "more\n")
    git(work, "commit", "--quiet", "-am", "advance")
    git(work, "push", "--quiet", "origin", "main")
    return git(work, "rev-parse", "HEAD").stdout.strip()


def case_first_run_then_real_run(failures: list, tmpdir: Path) -> None:
    """End-to-end: first run seeds state; the next run, against a moved
    trunk, sees the seeded state and runs the triager. The first run is
    the rejection of "triage the whole queue from a standing start"; the
    second run is where the real work happens. Pinning them together
    catches a regression that re-triages everything on the first run, or
    that refuses to run because the ledger exists from a previous gap."""
    origin_dir = tmpdir / "e2e"
    origin, work, old_sha, new_sha = make_origin(origin_dir)
    state_dir = origin_dir / "state"
    state_dir.mkdir()
    queue = origin_dir / "queue"
    queue.mkdir()
    env_file = origin_dir / "env"
    triager = origin_dir / "triager"
    write_config(env_file, work, triager, state_dir)
    write_stub_triager(triager, {})

    r1 = run_script(origin_dir, env_file)
    assert_eq("e2e first: exit code", 0, r1.returncode, failures)
    assert_eq("e2e first: stdout silent", "", r1.stdout.strip(), failures)
    s1 = state_files(state_dir)
    assert_eq("e2e first: state records HEAD_NOW",
              new_sha, s1["state"], failures)

    # Plant a ledger that points at issues nobody actually triaged in this
    # gap -- the script must not skip them just because the file exists.
    # The stub's candidate list contains both 42 and 9999; 42 is the one
    # that the stub will actually triage (9999 simulates "left over from a
    # previous run"), and the test pins that 42 still appears in the
    # summary despite the file's existence.
    (state_dir / "triage-issues").write_text("9999\n")

    # Advance the trunk so the second run has a real gap to close.
    final_sha = advance_trunk(work)

    scenarios = {scenario_key(work, new_sha): {"issues": [42, 9999]}}
    write_stub_triager(triager, scenarios)
    r2 = run_script(origin_dir, env_file)
    assert_eq("e2e second: exit code", 0, r2.returncode, failures)
    if "issue-42.md" not in r2.stdout:
        failures.append(
            f"e2e second: summary missing the issue the stub triaged: "
            f"{r2.stdout!r}")
    if "issue-9999.md" in r2.stdout:
        failures.append(
            f"e2e second: ledger's stale '9999' should not have been "
            f"re-triaged, but it appears: {r2.stdout!r}")
    s2 = state_files(state_dir)
    assert_eq("e2e second: state advances to new HEAD_NOW",
              final_sha, s2["state"], failures)
    assert_eq("e2e second: ledger cleared", None, s2["ledger"], failures)


def case_stale_sentinel_does_not_close_a_gap(failures: list, tmpdir: Path) -> None:
    """A sentinel left by a previous run must not credit this one.

    The triager touches $LEDGER.done as its last act. A run whose triager
    finished but whose script was killed before advancing state leaves that
    file on disk. If the next run reads it, it advances past a gap it never
    triaged and clears the ledger that recorded what was skipped -- the state
    the issue says must never be produced."""
    origin, work, old_sha, new_sha = make_origin(tmpdir / "stale")
    state_dir = tmpdir / "stale" / "state"
    state_dir.mkdir()
    (tmpdir / "stale" / "queue").mkdir()
    (state_dir / "triage-last-trunk").write_text(old_sha + "\n")
    (state_dir / "triage-issues.done").write_text("")
    env_file = tmpdir / "stale" / "env"
    triager = tmpdir / "stale" / "triager"
    write_config(env_file, work, triager, state_dir)
    # Killed after one of three: this run did not close the gap.
    write_stub_triager(triager, {
        scenario_key(work, old_sha): {"issues": [5, 7, 12], "kill_after": 1},
    })

    r = run_script(tmpdir / "stale", env_file)
    s = state_files(state_dir)
    assert_eq("stale sentinel: exit code", 0, r.returncode, failures)
    assert_eq("stale sentinel: state must not advance", old_sha, s["state"],
              failures)
    if s["ledger"] is None or "5" not in s["ledger"]:
        failures.append(
            f"stale sentinel: ledger should keep the issue that finished: "
            f"got {s['ledger']!r}")


def main() -> int:
    failures: list = []
    if not SCRIPT.is_file():
        failures.append(f"missing script: {SCRIPT}")
        for f in failures:
            print(f"FAIL {f}")
        return 1
    if not TRIAGER.is_file():
        failures.append(f"missing triager: {TRIAGER}")
        for f in failures:
            print(f"FAIL {f}")
        return 1

    with tempfile.TemporaryDirectory() as t:
        tmpdir = Path(t)
        case_first_run(failures, tmpdir)
        case_trunk_unchanged(failures, tmpdir)
        case_complete_run(failures, tmpdir)
        case_complete_run_no_candidates(failures, tmpdir)
        case_killed_mid_run(failures, tmpdir)
        case_stale_sentinel_does_not_close_a_gap(failures, tmpdir)
        case_ledger_substring_does_not_skip(failures, tmpdir)
        case_first_run_then_real_run(failures, tmpdir)

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS triage-on-merge: first run, no movement, complete run, "
          "no-candidates, killed mid-run, stale sentinel, ledger substring, "
          "first-then-real-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
