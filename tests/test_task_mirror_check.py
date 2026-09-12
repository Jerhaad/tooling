#!/usr/bin/env python3
"""Pin the drift detector's behaviour with tests.

Four acceptance criteria from the issue:
  1. A task naming a job reports which `run:` steps the command does not
     reach (RECOMMEND) and which reached lines the workflow does not run
     (informational).
  2. `except` entries are excluded from both directions and listed as
     deliberate.
  3. A project without `[task.mirrors]` is a no-op (silent exit 0).
  4. The report never fails a run on its own (exit 0 always).

Run directly: `python3 tests/test_task_mirror_check.py`. The harness has no
test runner dependency; stdlib subprocess + tmpdirs are enough. We invoke
both the bash wrapper (`bin/task-mirror-check`) and the python helper
(`lib/workflow_steps.py`) directly, since each carries part of the behaviour.
"""
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "bin" / "task-mirror-check"
HELPER = REPO_ROOT / "lib" / "workflow_steps.py"
MANIFEST = REPO_ROOT / "lib" / "manifest.py"
EXAMPLE = REPO_ROOT / "gates.toml.example"
# A multi-job workflow committed at tests/fixtures/, so the parser is
# exercised against a real on-disk file -- not a string in this test. Every
# fixture built inline so far held exactly one job named `rust-tests`,
# which is the only arrangement the bug cannot reach.
MULTIJOB_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "workflow-multijob.yaml"


def setup_worktree(tmpdir: Path, manifest_text: str, workflow_text: str | None,
                   justfile_text: str | None = None) -> Path:
    """Materialise a fake project: gates.toml, optional workflow, optional justfile.

    `tmpdir` becomes the project root (we run `git init` so repo_root works).
    Returns the absolute path to the project root.
    """
    if justfile_text is not None:
        (tmpdir / "justfile").write_text(justfile_text)
    if workflow_text is not None:
        wf_dir = tmpdir / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "ci.yaml").write_text(workflow_text)
    (tmpdir / "gates.toml").write_text(manifest_text)
    subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=tmpdir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=tmpdir, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmpdir, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=tmpdir, check=True, capture_output=True)
    return tmpdir.resolve()


def manifest_with_task(task_name: str, command: str, mirrors: dict | None) -> str:
    """Build a gates.toml with one [[task]] (no mirrors block if mirrors is None)."""
    task_block = [
        "[[task]]",
        f'name = "{task_name}"',
        'role = "builder"',
        f'command = "{command}"',
    ]
    if mirrors is not None:
        task_block.append("[task.mirrors]")
        task_block.append(f'workflow = "{mirrors["workflow"]}"')
        task_block.append(f'job = "{mirrors["job"]}"')
        if "except" in mirrors:
            except_list = ", ".join(f'"{e}"' for e in mirrors["except"])
            task_block.append(f"except = [{except_list}]")
    return "\n".join(task_block) + "\n"


def run_wrapper(worktree: Path, task_name: str = "bench",
                capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(WRAPPER), str(worktree), "--task", task_name],
        capture_output=capture, text=True,
    )


def run_helper(worktree: Path, command: str, workflow: str,
               job: str, except_subs: list[str] | None = None) -> subprocess.CompletedProcess:
    args = [sys.executable, str(HELPER),
            "--command", command,
            "--workflow", workflow,
            "--job", job,
            "--worktree", str(worktree)]
    for e in (except_subs or []):
        args.extend(["--except", e])
    return subprocess.run(args, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Test cases.
# ---------------------------------------------------------------------------

WORKFLOW_THREE_STEPS = textwrap.dedent("""\
    name: CI
    on: [push]
    jobs:
      rust-tests:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v4
          - name: Build and test
            run: |
              cargo build --workspace
              cargo test --workspace
              cargo clippy -- -D warnings
          - name: Lint
            run: cargo fmt --all -- --check
    """)


def assert_pass(msg: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{msg}: {detail}")


def test_recommends_missing_step():
    """A task naming a job lists the workflow steps the command does not reach."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "cargo build --workspace",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "rust-tests"}),
                            WORKFLOW_THREE_STEPS)
        r = run_helper(wt, "cargo build --workspace",
                       ".github/workflows/ci.yaml", "rust-tests")
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0,
                    f"stderr={r.stderr!r}")
        assert_pass("RECOMMEND block present",
                    "RECOMMEND: add these workflow steps" in out, out)
        # The command covers `cargo build --workspace` but not the others.
        for needle in ("cargo test --workspace",
                       "cargo clippy -- -D warnings",
                       "cargo fmt --all -- --check"):
            assert_pass(f"recommends {needle!r}", needle in out, out)
        # And it does NOT recommend the one the command does reach.
        assert_pass("does not falsely recommend cargo build",
                    out.count("+ cargo build --workspace") == 0,
                    out)
        # `uses:` steps are not runs and must not appear at all.
        assert_pass("uses: not surfaced as a run step",
                    "actions/checkout" not in out, out)
        # No `informational:` block when nothing reaches-but-extra.
        assert_pass("no informational block",
                    "informational:" not in out, out)
        print("PASS test_recommends_missing_step")


def test_informational_extra():
    """A reached line the workflow does not run shows under `informational:`."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        # Command does `cargo build`, workflow does `cargo test`. So:
        # - workflow's `cargo test --workspace` is missing (RECOMMEND)
        # - command's `cargo build --workspace` is extra (informational)
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "cargo build --workspace",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "rust-tests"}),
                            WORKFLOW_THREE_STEPS)
        r = run_helper(wt, "echo 'preflight'",  # opaque single item
                       ".github/workflows/ci.yaml", "rust-tests")
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        # `echo 'preflight'` is the single reached item; not in workflow -> informational.
        assert_pass("informational block present",
                    "informational:" in out, out)
        assert_pass("informational contains the reached line",
                    "echo 'preflight'" in out, out)
        # The whole step list still gets a RECOMMEND block.
        assert_pass("recommend block still present",
                    "RECOMMEND:" in out, out)
        print("PASS test_informational_extra")


def test_in_sync():
    """An exact match prints `in sync` and no RECOMMEND/informational block."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        # Build a workflow with a single run that exactly matches the command.
        wf = textwrap.dedent("""\
            name: CI
            jobs:
              rust-tests:
                runs-on: ubuntu-latest
                steps:
                  - run: cargo build --workspace
            """)
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "cargo build --workspace",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "rust-tests"}),
                            wf)
        r = run_helper(wt, "cargo build --workspace",
                       ".github/workflows/ci.yaml", "rust-tests")
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("status in sync",
                    "status: in sync" in out, out)
        assert_pass("no RECOMMEND block",
                    "RECOMMEND:" not in out, out)
        assert_pass("no informational block",
                    "informational:" not in out, out)
        print("PASS test_in_sync")


def test_except_excluded_and_listed():
    """An `except` substring pulls the matching step out and lists it as deliberate.

    The except match is per-step: if any line of a multi-line step contains the
    except substring, the whole step goes to `deliberate` -- the user named a
    substring, not a line. Lines inside the step are still listed individually
    under deliberate so the report says which commands are inside the block.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        wt = setup_worktree(Path(t),
                            manifest_with_task(
                                "bench",
                                "cargo fmt --all -- --check",
                                {"workflow": ".github/workflows/ci.yaml",
                                 "job": "rust-tests",
                                 "except": ["cargo clippy"]}),
                            WORKFLOW_THREE_STEPS)
        r = run_helper(wt, "cargo fmt --all -- --check",
                       ".github/workflows/ci.yaml", "rust-tests",
                       except_subs=["cargo clippy"])
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        # The clippy line is NOT in the RECOMMEND list (the multi-line step
        # containing it was excluded by `except`).
        assert_pass("except line excluded from RECOMMEND",
                    out.count("+ cargo clippy") == 0, out)
        assert_pass("no false RECOMMEND for cargo test --workspace "
                    "(inside the excluded step)",
                    out.count("+ cargo test --workspace") == 0, out)
        # The deliberate block lists every line of the excluded step.
        assert_pass("deliberate block present",
                    "deliberate" in out, out)
        assert_pass("clippy line listed as deliberate",
                    "[workflow only] cargo clippy -- -D warnings" in out, out)
        assert_pass("cargo build line listed as deliberate (it was in the same step)",
                    "[workflow only] cargo build --workspace" in out, out)
        # cargo fmt is still in the workflow AND the command -> in sync for it.
        # No RECOMMEND/informational block for fmt.
        assert_pass("no RECOMMEND for cargo fmt",
                    out.count("+ cargo fmt") == 0, out)
        # The whole thing reports drift because other lines of the excluded
        # step (cargo build/test) ARE workflow steps the command does not reach
        # as such -- they were deliberately excluded, so the report says so.
        # In sync is the right outcome because every workflow step (after
        # except) is either reached by the command or deliberately excluded.
        assert_pass("status in sync (after except)",
                    "in sync" in out, out)
        print("PASS test_except_excluded_and_listed")


def test_no_mirrors_is_silent():
    """A [[task]] without [task.mirrors] exits 0 and prints nothing."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        # No mirrors block.
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "just ci", None),
                            WORKFLOW_THREE_STEPS)
        r = run_wrapper(wt, task_name="bench")
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("no stdout", r.stdout == "", f"stdout={r.stdout!r}")
        assert_pass("no stderr", r.stderr == "", f"stderr={r.stderr!r}")
        print("PASS test_no_mirrors_is_silent")


def test_always_exits_zero():
    """Even with drift detected, the report never fails a run."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "echo hi",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "rust-tests"}),
                            WORKFLOW_THREE_STEPS)
        r = run_wrapper(wt)
        assert_pass("exit 0 on drift", r.returncode == 0,
                    f"stderr={r.stderr!r}")
        assert_pass("drift surfaced", "drift" in r.stdout, r.stdout)
        print("PASS test_always_exits_zero")


def test_just_show_one_level():
    """`just ci` is expanded one level via `just --show` and reported by name.

    The expansion is exactly one level: a `just lint` inside the recipe body is
    a reached line the workflow may or may not run, but it is not expanded
    further -- `just` is the only one who knows what its recipes do.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        justfile = textwrap.dedent("""\
            ci:
                cargo build --workspace
                cargo test --workspace
                just lint
            """)
        # Workflow runs both `cargo build` and `cargo test`. `just lint` is a
        # recipe the local gate has but the CI workflow does not mirror -- it
        # shows under `informational:`, not as drift to be fixed.
        wf = textwrap.dedent("""\
            name: CI
            jobs:
              rust-tests:
                runs-on: ubuntu-latest
                steps:
                  - run: cargo build --workspace
                  - run: cargo test --workspace
            """)
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "just ci",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "rust-tests"}),
                            wf, justfile_text=justfile)
        r = run_wrapper(wt)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        # No missing workflow steps -- cargo build and cargo test are reached.
        assert_pass("no RECOMMEND block",
                    "RECOMMEND:" not in out, out)
        # `just lint` is the recipe the local gate has but the CI doesn't mirror.
        assert_pass("just lint surfaced as informational",
                    "just lint" in out and "informational:" in out, out)
        print("PASS test_just_show_one_level")


def test_just_show_joins_continuations():
    """A recipe body with line continuations is one logical reached line."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        # A continuation: `cargo test \` then `\t--workspace \` then
        # `\t--all-features` is one shell command after just --show joins it.
        justfile = textwrap.dedent("""\
            ci:
                cargo test \\
                    --workspace \\
                    --all-features
            """)
        wf = textwrap.dedent("""\
            name: CI
            jobs:
              rust-tests:
                runs-on: ubuntu-latest
                steps:
                  - run: cargo test --workspace --all-features
            """)
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "just ci",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "rust-tests"}),
                            wf, justfile_text=justfile)
        r = run_wrapper(wt)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("continuation joined matches workflow step",
                    "in sync" in out, out)
        print("PASS test_just_show_joins_continuations")


def test_unable_to_compare_when_no_just():
    """When `just` is missing on PATH, the report prints `unable to compare`."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        justfile = "ci:\n    cargo build --workspace\n"
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "just ci",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "rust-tests"}),
                            WORKFLOW_THREE_STEPS,
                            justfile_text=justfile)
        # Strip `just` from PATH but keep bash and python3 reachable. The
        # wrapper shells out to bash; the helper shells out to `just`.
        env = dict(os.environ)
        # Locate bash and python3 before we cut PATH down.
        bash_bin = shutil.which("bash")
        py_bin = shutil.which("python3")
        assert bash_bin and py_bin, "test environment must have bash and python3"
        env["PATH"] = os.path.dirname(bash_bin) + ":" + os.path.dirname(py_bin)
        r = subprocess.run([str(WRAPPER), str(wt)], capture_output=True,
                           text=True, env=env)
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0,
                    f"stderr={r.stderr!r}")
        assert_pass("unable to compare surfaced",
                    "unable to compare" in out, out)
        assert_pass("workflow steps listed for the human to read",
                    "workflow `run:` steps:" in out, out)
        print("PASS test_unable_to_compare_when_no_just")


def test_workflow_with_folded_block():
    """A `run: >` folded block is one logical command (folded to spaces)."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        wf = textwrap.dedent("""\
            name: CI
            jobs:
              rust-tests:
                runs-on: ubuntu-latest
                steps:
                  - run: >
                      cargo
                      build
                      --workspace
            """)
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench",
                                               "cargo build --workspace",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "rust-tests"}),
                            wf)
        r = run_helper(wt, "cargo build --workspace",
                       ".github/workflows/ci.yaml", "rust-tests")
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("folded step matches command",
                    "in sync" in out, out)
        print("PASS test_workflow_with_folded_block")


def test_workflow_with_inline_run():
    """A step `      - run: cargo build` (no `name:`) is still extracted."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        wf = textwrap.dedent("""\
            name: CI
            jobs:
              rust-tests:
                runs-on: ubuntu-latest
                steps:
                  - run: cargo build --workspace
            """)
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "echo hi",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "rust-tests"}),
                            wf)
        r = run_helper(wt, "echo hi",
                       ".github/workflows/ci.yaml", "rust-tests")
        out = r.stdout
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("RECOMMEND surfaces inline run step",
                    "cargo build --workspace" in out, out)
        print("PASS test_workflow_with_inline_run")


def test_unknown_job_prints_unable():
    """A job name that doesn't exist yields `unable to compare`, exit 0."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "echo hi",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "no-such-job"}),
                            WORKFLOW_THREE_STEPS)
        r = run_helper(wt, "echo hi",
                       ".github/workflows/ci.yaml", "no-such-job")
        out = r.stdout + r.stderr
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        assert_pass("unable to compare surfaced",
                    "unable to compare" in out, out)
        print("PASS test_unknown_job_prints_unable")


def test_missing_workflow_file_prints_unable():
    """A workflow path that doesn't exist yields `unable to compare`, exit 0."""
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        wt = setup_worktree(Path(t),
                            manifest_with_task("bench", "echo hi",
                                               {"workflow": ".github/workflows/ci.yaml",
                                                "job": "rust-tests"}),
                            None)  # no workflow file
        r = run_wrapper(wt)
        out = r.stdout + r.stderr
        assert_pass("exit 0", r.returncode == 0, f"stderr={r.stderr!r}")
        # Either the helper prints "workflow file not found" or "unable to
        # compare". Either way, the run is not failed.
        assert_pass("tool did not exit non-zero", r.returncode == 0,
                    f"stdout={r.stdout!r} stderr={r.stderr!r}")
        print("PASS test_missing_workflow_file_prints_unable")


def test_finds_job_among_siblings():
    """A job that is not first is found, and its siblings' steps are not leaked.

    The previous parser walked jobs and broke on the first sibling whose name
    did not match the target. Against the committed multi-job fixture, that
    left `rust-tests` -- the middle job -- unreached and surfaced
    `lint`'s and `web`'s `run:` lines as if they belonged to it.

    A test that passes against a single-job string cannot catch this: that
    string is the only arrangement the bug cannot reach. The fixture lives
    at tests/fixtures/workflow-multijob.yaml so the parser sees a real file.
    """
    assert_pass("fixture committed on disk",
                MULTIJOB_FIXTURE.is_file(),
                f"missing: {MULTIJOB_FIXTURE}")
    workflow_text = MULTIJOB_FIXTURE.read_text()

    # The helper module lives under lib/, not on sys.path. Load it by path
    # so the test does not have to know where the source lives.
    import importlib.util
    spec = importlib.util.spec_from_file_location("_wfs_under_test", HELPER)
    assert spec is not None and spec.loader is not None, \
        f"could not load {HELPER}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    steps, err = mod.extract_run_steps(workflow_text, "rust-tests")
    assert_pass("rust-tests is found",
                err is None, f"err={err!r}")
    assert_pass("steps list non-empty", bool(steps), f"steps={steps!r}")

    # Every extracted step is from rust-tests. Sibling jobs' commands must
    # NOT leak in -- that would mean the parser kept walking past the target.
    flat = "\n".join(s for s in (steps or []))
    assert_pass("rust-tests' cargo build present",
                "cargo build --workspace" in flat, flat)
    assert_pass("rust-tests' cargo test --workspace present",
                "cargo test --workspace" in flat, flat)
    assert_pass("rust-tests' cargo test --doc present",
                "cargo test --doc" in flat, flat)
    assert_pass("lint job's cargo fmt NOT leaked",
                "cargo fmt --all -- --check" not in flat, flat)
    assert_pass("lint job's cargo clippy NOT leaked",
                "cargo clippy" not in flat, flat)
    assert_pass("web job's npm ci NOT leaked",
                "npm ci" not in flat, flat)
    assert_pass("web job's npm test NOT leaked",
                "npm test" not in flat, flat)
    print("PASS test_finds_job_among_siblings")


# ---------------------------------------------------------------------------
# Runner.
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_recommends_missing_step,
        test_informational_extra,
        test_in_sync,
        test_except_excluded_and_listed,
        test_no_mirrors_is_silent,
        test_always_exits_zero,
        test_just_show_one_level,
        test_just_show_joins_continuations,
        test_unable_to_compare_when_no_just,
        test_workflow_with_folded_block,
        test_workflow_with_inline_run,
        test_unknown_job_prints_unable,
        test_missing_workflow_file_prints_unable,
        test_finds_job_among_siblings,
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
