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


def make_fakebin(tmpdir: Path, extra: dict | None = None) -> Path:
    """A directory of no-op stand-ins for the transport, plus any extras."""
    fake = tmpdir / "fakebin"
    fake.mkdir()
    scripts = {
        # The drift test must not touch a host or move a tree.
        "ssh": "#!/bin/sh\nexit 0\n",
        "rsync": "#!/bin/sh\nexit 0\n",
    }
    scripts.update(extra or {})
    for name, body in scripts.items():
        p = fake / name
        p.write_text(body)
        p.chmod(0o755)
    return fake


def run_remote_task(project: Path, fakebin: Path, repo_bin: bool) -> subprocess.CompletedProcess:
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


def main() -> int:
    tests = [
        test_reports_drift_after_pass,
        test_no_mirrors_is_unchanged,
        test_failing_tool_does_not_fail_run,
        test_missing_tool_is_named,
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
