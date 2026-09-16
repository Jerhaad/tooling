#!/usr/bin/env python3
"""Pin that remote-task reaches the drift report after a successful run.

The report is advisory end to end: a task without [task.mirrors] must print
nothing extra, a failing task-mirror-check must not fail the run, and a
machine without the tool on PATH must see a named skip rather than a shell
error. The ssh and rsync transport is faked with no-op scripts; the manifest
reader, the drift report, and the wiring in remote-task are the real thing,
so a regression in any of them is caught here, not just in the tool's own
tests.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_TASK = REPO_ROOT / "bin" / "remote-task"
REPO_BIN = REPO_ROOT / "bin"
MANIFEST_PY = REPO_ROOT / "lib" / "manifest.py"

# The workflow runs one step more than the command, so a correct report names
# the missing step. The command is opaque (not a just recipe) so this test
# does not depend on `just` being on PATH: one-level expansion is pinned by
# tests/test_task_mirror_check.py.
WORKFLOW = textwrap.dedent("""\
    name: CI
    on: [push]
    jobs:
      rust-tests:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - run: cargo build --workspace
          - run: cargo test --workspace
    """)


def manifest_text(mirrors: bool) -> str:
    lines = [
        "[[task]]",
        'name = "bench"',
        'role = "builder"',
        'command = "cargo build --workspace"',
    ]
    if mirrors:
        lines += [
            "[task.mirrors]",
            'workflow = ".github/workflows/ci.yaml"',
            'job = "rust-tests"',
        ]
    return "\n".join(lines) + "\n"


def manifest_with_migrator(sources: list[str] | None) -> str:
    """Build a gates.toml with [task.migrator.sources] when `sources` is set.

    The block is omitted when sources is None, so the existing test fixture
    can stay a one-liner and the new tests opt in.
    """
    lines = [
        "[[task]]",
        'name = "bench"',
        'role = "builder"',
        'command = "cargo build --workspace"',
    ]
    if sources is not None:
        quoted = ", ".join(f'"{s}"' for s in sources)
        lines += ["[task.migrator]", f"sources = [{quoted}]"]
    return "\n".join(lines) + "\n"


def setup_project(tmpdir: Path, mirrors: bool) -> Path:
    """Materialise a fake project and git-init it so repo_root works."""
    wf_dir = tmpdir / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "ci.yaml").write_text(WORKFLOW)
    (tmpdir / "gates.toml").write_text(manifest_text(mirrors))
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=tmpdir, check=True,
                       capture_output=True)
    return tmpdir.resolve()


def setup_project_migrator(tmpdir: Path, sources: list[str] | None,
                            migrations: list[tuple[str, str]] | None) -> Path:
    """Materialise a fake project with optional [task.migrator] and `migrations/`.

    `migrations` is a list of (name, body) pairs that get written into
    `tmpdir/migrations/`. Passing None for either argument skips the
    corresponding setup. The directory is created empty when migrations is
    `[]`.
    """
    tmpdir.mkdir(parents=True, exist_ok=True)
    (tmpdir / "gates.toml").write_text(manifest_with_migrator(sources))
    if migrations is not None:
        mig = tmpdir / "migrations"
        mig.mkdir(exist_ok=True)
        for name, body in migrations:
            (mig / name).write_text(body)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", *args], cwd=tmpdir, check=True,
                       capture_output=True)
    return tmpdir.resolve()


# The stateful fake ssh. Lives below the existing helpers so the older
# tests -- which use a no-op ssh -- keep their contract. The script
# maintains a fake remote filesystem rooted at $FAKE_REMOTE_ROOT; every
# `ssh HOST CMD...` invocation mutates that filesystem so the test can
# assert on what remote-task sent.
#
# Intentionally narrow: only the verbs remote-task actually sends. Any
# other invocation is a test bug or a tool bug, and silent no-ops hide
# both. We log every dispatch to stderr so the trace tells the next
# reader which verb we routed.
STATEFUL_FAKE_SSH = r"""#!/bin/bash
root="${FAKE_REMOTE_ROOT:?must be set}"
host="${1:?fake ssh needs a host}"
shift
mkdir -p "$root"
# remote-task passes the whole remote command as a single argument,
# so the verb and any flags arrive in one string. Strip it down to
# the verb and the first non-flag arg; everything past that goes
# straight to the verb implementation.
cmdline="$1"
# Verb: the first whitespace-separated word.
verb="${cmdline%% *}"
case "$verb" in
mkdir)
  # Extract the directory: skip `mkdir`, skip any `-X` flags, take the
  # first non-flag argument.
  d=""
  for a in $cmdline; do
    case "$a" in
      mkdir) ;;
      -*) ;;
      *) d="$a"; break ;;
    esac
  done
  [ -n "$d" ] || { echo "fake ssh: mkdir needs a path" >&2; exit 1; }
  mkdir -p "$root/$d"
  ;;
cat)
  # `cat PATH 2>/dev/null`: skip cat, take the first non-flag non-redir.
  f=""
  for a in $cmdline; do
    case "$a" in
      cat) ;;
      2*) ;;
      ">"*) ;;
      *) f="$a"; break ;;
    esac
  done
  cat "$root/$f" 2>/dev/null || true
  ;;
touch)
  # `touch FILE1 FILE2 ...`: take every argument that isn't a flag.
  for a in $cmdline; do
    case "$a" in
      touch|-*) ;;
      *) mkdir -p "$root/$(dirname "$a")"; touch "$root/$a" ;;
    esac
  done
  ;;
printf)
  # printf '%s\n' CONTENT > FILE -- the only form remote-task uses.
  # Parse: skip `printf`, skip the format string ('%s\n'), take the body,
  # skip `>`, take the target. Bash passes the body wrapped in single
  # quotes from the command line; strip those so the file content
  # matches what remote-task thought it sent.
  body=""
  target=""
  state=verb
  for a in $cmdline; do
    case "$state:$a" in
      verb:printf) state=format ;;
      format:'%s\n') state=body ;;
      format:*) state=body; body="$a" ;;
      body:'>') state=target ;;
      body:*) body="${a#\'}"; body="${body%\'}" ;;
      target:*) target="$a" ;;
    esac
  done
  [ -n "$target" ] || { echo "fake ssh: printf needs a target" >&2; exit 1; }
  mkdir -p "$root/$(dirname "$target")"
  printf '%s\n' "$body" > "$root/$target"
  ;;
flock)
  # The build's heredoc form: consume stdin, do nothing.
  cat > /dev/null
  ;;
docker)
  # Container reset is inert to the staleness check.
  ;;
*)
  echo "fake ssh: unhandled verb: $verb" >&2
  cat > /dev/null
  exit 0
  ;;
esac
"""


def make_fakebin(tmpdir: Path, extra: dict | None = None,
                  stateful: bool = False) -> Path:
    """A directory of no-op stand-ins for the transport, plus any extras."""
    fake = tmpdir / "fakebin"
    fake.mkdir()
    scripts = {
        # The drift test must not touch a host or move a tree.
        "ssh": "#!/bin/sh\nexit 0\n",
        "rsync": "#!/bin/sh\nexit 0\n",
    }
    if stateful:
        scripts["ssh"] = STATEFUL_FAKE_SSH
    scripts.update(extra or {})
    for name, body in scripts.items():
        p = fake / name
        p.write_text(body)
        p.chmod(0o755)
    return fake


def run_remote_task(project: Path, fakebin: Path, repo_bin: bool,
                     fake_remote_root: Path | None = None
                     ) -> subprocess.CompletedProcess:
    """Run the real remote-task against the fake project, hermetically."""
    env_file = project / "tools.env"
    env_file.write_text("")
    lock = project / "build.lock"
    path_parts = [str(fakebin)]
    if repo_bin:
        # The real task-mirror-check, as it would sit on PATH after install.
        path_parts.append(str(REPO_BIN))
    env = {
        "PATH": os.pathsep.join(path_parts + ["/usr/bin", "/bin"]),
        # Named role, fake target: host_for must not reach a real host.
        "HOST_BUILDER": "fakehost",
        # A scratch env file so this machine's operator env is never sourced.
        "TOOLS_ENV": str(env_file),
        # Keep the local build lock out of $HOME.
        "REMOTE_TASK_LOCK": str(lock),
        # $HOME is only read for defaults both of which are overridden; point
        # it at the project so nothing can reach the real one.
        "HOME": str(project),
    }
    if fake_remote_root is not None:
        env["FAKE_REMOTE_ROOT"] = str(fake_remote_root)
    return subprocess.run([str(REMOTE_TASK), str(project)],
                          capture_output=True, text=True, env=env)


def assert_pass(msg: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{msg}: {detail}")


def test_reports_drift_after_pass():
    """A successful run with a mirrors block ends in the drift report."""
    with tempfile.TemporaryDirectory() as t:
        project = setup_project(Path(t) / "proj", True)
        fakebin = make_fakebin(Path(t))
        r = run_remote_task(project, fakebin, repo_bin=True)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0,
                    f"stderr={r.stderr!r}")
        assert_pass("build reported PASS", "==> PASS (bench" in out, out)
        assert_pass("drift report present", "drift report" in out, out)
        assert_pass("missing step recommended",
                    "+ cargo test --workspace" in out, out)
        # The report is an observation after the run, not before or instead
        # of it: the PASS line comes first.
        assert_pass("report after the PASS line",
                    out.index("==> PASS") < out.index("drift report"), out)


def test_no_mirrors_is_unchanged():
    """A task without [task.mirrors] prints exactly what it always did."""
    with tempfile.TemporaryDirectory() as t:
        project = setup_project(Path(t) / "proj", False)
        fakebin = make_fakebin(Path(t))
        r = run_remote_task(project, fakebin, repo_bin=True)
        lines = r.stdout.splitlines()
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("no drift output of any kind",
                    "drift" not in r.stdout, r.stdout)
        assert_pass("no stderr", r.stderr == "", r.stderr)
        # The run's own two lines and nothing else: the wiring must be
        # invisible to a project that has not opted in.
        assert_pass("exactly the pre-existing output lines",
                    len(lines) == 2
                    and lines[0].startswith("==> syncing")
                    and lines[1].startswith("==> PASS (bench"),
                    r.stdout)


def test_failing_tool_does_not_fail_run():
    """A task-mirror-check that exits non-zero cannot fail the build."""
    with tempfile.TemporaryDirectory() as t:
        project = setup_project(Path(t) / "proj", True)
        fakebin = make_fakebin(Path(t), extra={
            # A tool that is on PATH but broken: the run must survive it.
            "task-mirror-check":
                "#!/bin/sh\necho marker-broken-mirror\nexit 1\n",
        })
        r = run_remote_task(project, fakebin, repo_bin=False)
        out = r.stdout
        assert_pass("exit 0 with a failing tool", r.returncode == 0,
                    f"stderr={r.stderr!r}")
        assert_pass("the tool was reached",
                    "marker-broken-mirror" in out, out)
        assert_pass("build still reported PASS",
                    "==> PASS (bench" in out, out)


def test_missing_tool_is_named():
    """Without the tool on PATH the run prints a named skip, not an error."""
    with tempfile.TemporaryDirectory() as t:
        project = setup_project(Path(t) / "proj", True)
        fakebin = make_fakebin(Path(t))
        r = run_remote_task(project, fakebin, repo_bin=False)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("named skip printed",
                    "task-mirror-check not on PATH" in out, out)
        assert_pass("no drift report attempted",
                    "drift report for" not in out, out)
        assert_pass("build still reported PASS",
                    "==> PASS (bench" in out, out)


# ---------------------------------------------------------------------------
# Migration staleness (issue #50). Each test below exercises one branch of the
# check: no migrations dir, [task.migrator] absent, first sync, matching
# fingerprint, changed fingerprint, and the touch surviving a real rsync.

MIGRATOR_SOURCES = ["crates/db/src/lib.rs", "crates/api-rest/src/lib.rs",
                    "src/main.rs"]
MIGRATIONS_V1 = [("0001_init.sql", "CREATE TABLE init (id INT);\n")]
MIGRATIONS_V2 = [("0001_init.sql", "CREATE TABLE init (id INT);\n"),
                 ("0002_added.sql", "CREATE TABLE added (id INT);\n")]


def local_fingerprint(migrations: list[tuple[str, str]]) -> str:
    """The same fingerprint remote-task computes locally.

    The bash side runs `find | sort -z | xargs sha256sum | sha256sum`.
    That hashes each file, joins `<hash>  \n` lines in sorted
    order, and hashes the joined blob. We mirror that with Python so the
    test can pre-populate a matching fingerprint and verify the
    no-touch branch.
    """
    import hashlib
    lines = []
    for name, body in sorted(migrations):
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        lines.append(f"{body_hash}  migrations/{name}\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def test_no_migrations_dir_is_unchanged():
    """[task.migrator] configured but no migrations/ on disk: silent.

    A project that has neither an embedded set nor a [task.migrator]
    block must look identical to one that has neither. Without a
    migrations/ directory the fingerprint is undefined and we do not
    write one.
    """
    with tempfile.TemporaryDirectory() as t:
        project = setup_project_migrator(
            Path(t) / "proj", sources=MIGRATOR_SOURCES, migrations=None)
        fakebin = make_fakebin(Path(t), stateful=True)
        fake_root = Path(t) / "remote"
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("no migration touch line",
                    "migrations changed" not in out, out)
        assert_pass("no fingerprint written on remote",
                    not (fake_root / "bench" / "proj"
                         / ".migrations-fingerprint").exists(),
                    "fingerprint should not exist without migrations/")
        assert_pass("build still reported PASS",
                    "==> PASS (bench" in out, out)


def test_migrator_unconfigured_is_unchanged():
    """Project has migrations/ but no [task.migrator] block: silent.

    Without a sources list the tool has nothing to touch and a rebuild
    is outside its reach. The fingerprint is also skipped, so the next
    run that does opt in starts from a clean slate.
    """
    with tempfile.TemporaryDirectory() as t:
        project = setup_project_migrator(
            Path(t) / "proj", sources=None, migrations=MIGRATIONS_V1)
        fakebin = make_fakebin(Path(t), stateful=True)
        fake_root = Path(t) / "remote"
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("no migration touch line",
                    "migrations changed" not in out, out)
        assert_pass("no fingerprint written when not configured",
                    not (fake_root / "bench" / "proj"
                         / ".migrations-fingerprint").exists(),
                    "fingerprint should not exist when [task.migrator] absent")
        assert_pass("build still reported PASS",
                    "==> PASS (bench" in out, out)


def test_first_run_writes_fingerprint_no_touch():
    """A cold cache records the current set; no touch on first sync.

    The first compile is going to embed the migrations from scratch
    regardless, so a touch adds nothing. Writing the fingerprint
    afterwards means the *next* run has a baseline to compare against.
    """
    with tempfile.TemporaryDirectory() as t:
        project = setup_project_migrator(
            Path(t) / "proj", sources=MIGRATOR_SOURCES, migrations=MIGRATIONS_V1)
        fakebin = make_fakebin(Path(t), stateful=True)
        fake_root = Path(t) / "remote"
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("no migration touch line on first run",
                    "migrations changed" not in out, out)
        fp_path = fake_root / "bench" / "proj" / ".migrations-fingerprint"
        assert_pass("fingerprint recorded on first sync",
                    fp_path.is_file(), "expected fingerprint to exist")
        assert_pass("fingerprint matches local migration set",
                    fp_path.read_text().strip() == local_fingerprint(MIGRATIONS_V1),
                    fp_path.read_text())
        assert_pass("no source files were touched on the remote",
                    not any((fake_root / "bench" / "proj" / s).exists()
                            for s in MIGRATOR_SOURCES),
                    "expected no sources to be touched")


def test_matching_fingerprint_no_touch():
    """When the local set matches what the remote last built, do nothing.

    This is the warm-cache case the issue is specifically trying to
    preserve: an unchanged tree does not trigger a rebuild. We
    pre-populate the fingerprint the local side would compute, so the
    comparison succeeds and the touch branch is skipped.
    """
    with tempfile.TemporaryDirectory() as t:
        project = setup_project_migrator(
            Path(t) / "proj", sources=MIGRATOR_SOURCES, migrations=MIGRATIONS_V1)
        fake_root = Path(t) / "remote"
        # Pre-populate the fingerprint that local_fingerprint(MIGRATIONS_V1)
        # would produce. mkdir the dest first so the fake ssh can land the
        # file. We do this by hand here rather than by invoking the tool.
        remote_dest = fake_root / "bench" / "proj"
        remote_dest.mkdir(parents=True, exist_ok=True)
        (remote_dest / ".migrations-fingerprint").write_text(
            local_fingerprint(MIGRATIONS_V1) + "\n")
        fakebin = make_fakebin(Path(t), stateful=True)
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("no migration touch line on a warm cache",
                    "migrations changed" not in out, out)
        assert_pass("no source files were touched",
                    not any((fake_root / "bench" / "proj" / s).exists()
                            for s in MIGRATOR_SOURCES),
                    "expected no sources to be touched")
        assert_pass("fingerprint left unchanged",
                    (fake_root / "bench" / "proj"
                     / ".migrations-fingerprint").read_text().strip()
                    == local_fingerprint(MIGRATIONS_V1),
                    "fingerprint should not change when nothing changed")


def test_changed_migrations_touch_sources():
    """A migrations set that diverges from what the remote cached triggers a touch.

    The pre-populated fingerprint hashes the V1 set; the local tree now
    has V2. The tool names the rebuild it performs -- silent touch,
    silent rebuild is the failure mode this test exists to catch.
    """
    with tempfile.TemporaryDirectory() as t:
        project = setup_project_migrator(
            Path(t) / "proj", sources=MIGRATOR_SOURCES, migrations=MIGRATIONS_V2)
        fake_root = Path(t) / "remote"
        remote_dest = fake_root / "bench" / "proj"
        remote_dest.mkdir(parents=True, exist_ok=True)
        (remote_dest / ".migrations-fingerprint").write_text(
            local_fingerprint(MIGRATIONS_V1) + "\n")
        fakebin = make_fakebin(Path(t), stateful=True)
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("announcement line printed",
                    "migrations changed since last sync" in out, out)
        # Names the number of files, so a reviewer can see how broad the
        # rebuild will be without grepping the manifest.
        assert_pass("announcement names the count",
                    f"touching {len(MIGRATOR_SOURCES)} source(s)" in out, out)
        for src in MIGRATOR_SOURCES:
            assert_pass(f"touched {src}",
                        (fake_root / "bench" / "proj" / src).exists(),
                        f"missing touched file: {src}")
        # Fingerprint advanced to the new set, so a subsequent run with
        # the same local tree does not re-touch.
        new_fp = (fake_root / "bench" / "proj"
                  / ".migrations-fingerprint").read_text().strip()
        assert_pass("fingerprint updated to the new set",
                    new_fp == local_fingerprint(MIGRATIONS_V2),
                    f"got {new_fp!r}")
        # The announcement comes *before* the build, not after, so a
        # reviewer reading top-to-bottom sees what is about to happen.
        assert_pass("announcement precedes the build PASS",
                    out.index("migrations changed") < out.index("==> PASS"),
                    out)


# The transport is a no-op in every test above, which is what let the touch sit
# on the wrong side of the sync: a stub rsync cannot revert an mtime. This one
# runs the real rsync against a local directory standing in for the builder.
REAL_RSYNC_STUB = """#!/bin/sh
# Rewrite host:path into the fake root, then hand over to the real rsync so
# the archive flag's mtime preservation is actually exercised.
set -e
args=""
for a in "$@"; do
  case "$a" in
    *:*) a="$FAKE_REMOTE_ROOT/${a#*:}" ;;
  esac
  args="$args $a"
done
exec /usr/bin/rsync $args
"""


def test_touch_survives_a_real_rsync():
    """`rsync -a` preserves the source's mtimes, so a touch applied to the
    remote copy before the transfer is reverted by it: the file lands with its
    original timestamp, older than the artifacts in the warm target/, and cargo
    rebuilds nothing. The touch has to happen after the sync, and only a real
    rsync can show the difference."""
    if not Path("/usr/bin/rsync").exists():
        print("SKIP touch survives a real rsync: no /usr/bin/rsync")
        return
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        project = setup_project_migrator(
            tmp / "proj", sources=MIGRATOR_SOURCES, migrations=MIGRATIONS_V1)
        # The sources have to exist for rsync to deliver them, and to be older
        # than the run so a surviving touch is visible as a newer mtime.
        old_time = 1756700000  # a fixed point well before the test runs
        for s in MIGRATOR_SOURCES:
            f = project / s
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("// holds a Migrator\n")
            os.utime(f, (old_time, old_time))
        fakebin = make_fakebin(tmp, stateful=True,
                              extra={"rsync": REAL_RSYNC_STUB})
        fake_root = tmp / "remote"
        # First sync records the baseline fingerprint.
        run_remote_task(project, fakebin, repo_bin=False,
                        fake_remote_root=fake_root)
        # The migration set moves, as a rebase would move it.
        for name, body in MIGRATIONS_V2:
            (project / "migrations" / name).write_text(body)
        r = run_remote_task(project, fakebin, repo_bin=False,
                            fake_remote_root=fake_root)
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("announced the touch",
                    "migrations changed" in r.stdout, r.stdout)
        for s in MIGRATOR_SOURCES:
            remote = fake_root / "bench" / "proj" / s
            local = project / s
            assert_pass(f"{s} present on the remote", remote.is_file(), str(remote))
            assert_pass(
                f"{s} is newer on the remote than in the source tree",
                remote.stat().st_mtime > local.stat().st_mtime,
                f"remote={remote.stat().st_mtime} local={local.stat().st_mtime} "
                "-- a touch before the sync is reverted by rsync -a")


def main() -> int:
    tests = [
        test_reports_drift_after_pass,
        test_no_mirrors_is_unchanged,
        test_failing_tool_does_not_fail_run,
        test_missing_tool_is_named,
        test_no_migrations_dir_is_unchanged,
        test_migrator_unconfigured_is_unchanged,
        test_first_run_writes_fingerprint_no_touch,
        test_matching_fingerprint_no_touch,
        test_changed_migrations_touch_sources,
        test_touch_survives_a_real_rsync,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures.append(f"{t.__name__}: {e}")
        except Exception as e:
            failures.append(f"{t.__name__}: {type(e).__name__}: {e}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f" - {f}")
        return 1
    print(f"\nPASS: all {len(tests)} tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
