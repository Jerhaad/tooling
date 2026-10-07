#!/usr/bin/env python3
"""The lane wrapper refuses a compile under a login shell, and hermes-implement
exports it only when the manifest declares a builder.

Run directly: `python3 tests/test_lane_rustc.py`. The cargo case needs cargo.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "lib" / "lane-rustc"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_hermes_implement_tmp import (  # noqa: E402
    HERMES_IMPLEMENT, git, make_worktree, write_stub_gh)

STUB_HERMES = """#!/usr/bin/env python3
import os, subprocess
print('RUSTC_WRAPPER=' + os.environ.get('RUSTC_WRAPPER', ''))
open(os.path.join(os.environ['TMPDIR'], 'HANDOVER.md'), 'w').write('stub\\n')
subprocess.check_call(['git', 'commit', '--quiet', '--allow-empty', '-m', 'stub'],
    env={**os.environ, 'GIT_AUTHOR_NAME': 's', 'GIT_AUTHOR_EMAIL': 's@s',
         'GIT_COMMITTER_NAME': 's', 'GIT_COMMITTER_EMAIL': 's@s'})
"""


def wrap(tmp: Path, *args: str) -> tuple[subprocess.CompletedProcess, bool]:
    """Run the wrapper over a stub rustc; report whether the stub ran."""
    marker = tmp / "ran"
    marker.unlink(missing_ok=True)
    rustc = tmp / "rustc"
    rustc.write_text(f"#!/bin/sh\ntouch {marker}\n")
    rustc.chmod(0o755)
    proc = subprocess.run([str(WRAPPER), str(rustc), *args],
                          capture_output=True, text=True)
    return proc, marker.exists()


def lane_wrapper(root: Path, role: str, stub: Path, gh_dir: Path) -> str:
    """Run hermes-implement on a project whose one task has `role`; return the
    RUSTC_WRAPPER its agent saw."""
    wt = make_worktree(root)
    (wt / "gates.toml").write_text(
        f'[[task]]\nname = "bench"\nrole = "{role}"\ncommand = "true"\n')
    git(wt, "add", "gates.toml")
    git(wt, "commit", "--quiet", "-m", "manifest")
    state = root / "state"
    state.mkdir()
    proc = subprocess.run(
        [str(HERMES_IMPLEMENT), "--issue", "1", "--worktree", str(wt)],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "HERMES_PYTHON": str(stub),
             "AGENT_STATE_DIR": str(state),
             "PATH": f"{gh_dir}:{os.environ.get('PATH', '')}",
             "HERMES_IMPLEMENT_ATTEMPTS": "1",
             "HERMES_IMPLEMENT_TIMEOUT": "30"})
    out = proc.stdout.strip().splitlines()
    report = Path(out[-1]) / "attempt1.stdout" if out else None
    if not report or not report.exists():
        raise RuntimeError(f"no lane report: rc={proc.returncode} stderr={proc.stderr[-400:]!r}")
    for line in report.read_text().splitlines():
        if line.startswith("RUSTC_WRAPPER="):
            return line.split("=", 1)[1]
    return ""


def main() -> int:
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        proc, ran = wrap(tmp, "--crate-name", "t", "src/main.rs")
        if proc.returncode == 0 or ran:
            failures.append(f"a compile went through: rc={proc.returncode} ran={ran}")
        if "remote-task" not in proc.stderr:
            failures.append(f"the refusal does not name remote-task: {proc.stderr!r}")

        for args in (["-", "--crate-name", "___", "--print=cfg"], ["-vV"]):
            proc, ran = wrap(tmp, *args)
            if proc.returncode != 0 or not ran:
                failures.append(f"a probe was refused: {args} rc={proc.returncode}")

        if shutil.which("cargo"):
            crate = tmp / "crate"
            (crate / "src").mkdir(parents=True)
            (crate / "Cargo.toml").write_text(
                '[package]\nname = "t"\nversion = "0.1.0"\nedition = "2021"\n')
            (crate / "src" / "main.rs").write_text("fn main() {}\n")
            env = {**os.environ, "RUSTC_WRAPPER": str(WRAPPER)}
            for cmd, want_ok in (("cargo build", False),
                                 ("cargo metadata --format-version 1", True),
                                 ("cargo fmt --check", True)):
                proc = subprocess.run(["bash", "-l", "-c", cmd], cwd=crate, env=env,
                                      capture_output=True, text=True, timeout=300)
                if (proc.returncode == 0) != want_ok:
                    failures.append(f"`{cmd}` under bash -l: rc={proc.returncode} "
                                    f"stderr={proc.stderr[-400:]!r}")
            if list((crate / "target").glob("debug/t")):
                failures.append("cargo build produced a binary")
        else:
            print("SKIP cargo under a login shell: cargo not installed")

        stub = tmp / "hermes_stub.py"
        stub.write_text(STUB_HERMES)
        stub.chmod(0o755)
        gh_dir = tmp / "ghbin"
        gh_dir.mkdir()
        write_stub_gh(gh_dir)
        for name, role, want in (("builder", "builder", WRAPPER.resolve()),
                                 ("local", "cluster", None)):
            seen = lane_wrapper(tmp / name, role, stub, gh_dir)
            got = Path(seen).resolve() if seen else None
            if got != want:
                failures.append(f"a project whose task role is {role} gave the lane "
                                f"RUSTC_WRAPPER={seen!r}")

    for f in failures:
        print(f"FAIL {f}")
    if failures:
        return 1
    print("PASS a project with a builder keeps lanes from compiling Rust locally")
    return 0


if __name__ == "__main__":
    sys.exit(main())
