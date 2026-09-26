#!/usr/bin/env python3
"""Pin gate-verify's decision between three cases.

  docs-only      -> exit 0, names the files
  gated code     -> exit 0, runs the gates the diff selected
  ungated code   -> exit 1, names the code path, not the whole diff

`--list` selects without running, so the gates' own suites never stand up. The
real gates.toml is copied into each tmpdir rather than a synthetic one.

Run directly: `python3 tests/test_gate_verify.py`.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_VERIFY = REPO_ROOT / "bin" / "gate-verify"
GATES_TOML = REPO_ROOT / "gates.toml"


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True, text=True, check=True)


def make_repo(tmpdir: Path, with_gates: bool = True) -> Path:
    """Stand up a throwaway repo with one commit on a branch off main.

    The project's gates.toml is copied in (when `with_gates`) so the
    test exercises the same manifest shape gate-verify sees in
    production. Initial files give `git diff --name-only` something to
    show; the diff under test is what each case appends on top.
    """
    repo = tmpdir / "wt"
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n")
    git(repo, "add", "README.md")
    if with_gates:
        shutil.copy(GATES_TOML, repo / "gates.toml")
    else:
        (repo / "gates.toml").write_text("")
    git(repo, "add", "gates.toml")
    git(repo, "commit", "-q", "-m", "init")
    # Set up a remote so `origin/main` resolves; gate-verify's default
    # VERIFY_BASE is origin/main and the test passes the repo as a path,
    # not a branch name.
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def run_gate_verify(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke bin/gate-verify against the repo with the given flags."""
    return subprocess.run(
        [str(GATE_VERIFY), str(repo), *args],
        capture_output=True, text=True,
        # common.sh reads $HOME for the optional env file; setting it
        # here keeps `set -u` from aborting before the test can read
        # the verdict. The other vars match what gate-verify expects
        # in production (a default VERIFY_BASE so the test does not
        # need to point at a real origin/main).
        env={"HOME": "/tmp", "PATH": "/usr/bin:/bin",
             "VERIFY_BASE": "origin/main"})


def selected_names(out: str) -> list[str]:
    """Parse `gate-verify --list` output. The format is two leading
    spaces, the gate name left-padded to 18 characters (and truncated
    if longer), then the run command -- so the name is the bytes from
    column 2 up to column 20, not a regex on whitespace."""
    names = []
    for ln in out.splitlines():
        if not ln.startswith("  "):
            continue
        if len(ln) < 20:
            continue
        names.append(ln[2:20].rstrip())
    return names


def selected_names_match(out: str, expected: list[str]) -> set[str]:
    """--list pads names to 18 chars and truncates anything longer; this
    matches each output name to the manifest's names by prefix so the
    test does not break on a name wider than the column."""
    seen = selected_names(out)
    matched = set()
    for got in seen:
        for want in expected:
            if got == want or want.startswith(got):
                matched.add(want)
    return matched


def case_docs_only(tmp: Path) -> list[str]:
    """A diff that is only docs, a licence, and a config file: nothing
    to gate, exit 0, message names the paths."""
    repo = make_repo(tmp / "docs", with_gates=False)
    # No gate, but the repo must still be a valid git repo for the
    # diff to be readable. The decision under test does not depend on
    # what gates are present.
    (repo / "LICENSE").write_text("MIT\n")
    (repo / "README.md").write_text("now with docs\n")
    (repo / ".editorconfig").write_text("root = true\n")
    git(repo, "add", "LICENSE", "README.md", ".editorconfig")
    git(repo, "commit", "-q", "-m", "docs-only")
    r = run_gate_verify(repo)
    failures = []
    if r.returncode != 0:
        failures.append(
            f"docs-only diff refused: rc={r.returncode} stderr={r.stderr.strip()}")
        return failures
    if "nothing to gate" not in r.stderr:
        failures.append(
            f"docs-only diff did not report 'nothing to gate': stderr={r.stderr.strip()}")
    for path in ("LICENSE", "README.md", ".editorconfig"):
        if path not in r.stderr:
            failures.append(
                f"docs-only diff did not name {path}: stderr={r.stderr.strip()}")
    return failures


def case_ungated_code(tmp: Path) -> list[str]:
    """A diff that adds an unwired .py file under lib/ -- code reaching
    main unverified. Exits 1 and names the file, not any docs alongside."""
    repo = make_repo(tmp / "ungated")
    # lib/, not bin/: every bin/ path matches a catch-all gate.
    (repo / "lib").mkdir()
    (repo / "lib" / "ungated_thing.py").write_text("# nothing\n")
    (repo / "LICENSE").write_text("MIT\n")
    git(repo, "add", "lib/ungated_thing.py", "LICENSE")
    git(repo, "commit", "-q", "-m", "ungated")
    r = run_gate_verify(repo)
    failures = []
    if r.returncode != 1:
        failures.append(
            f"ungated code did not refuse: rc={r.returncode} stderr={r.stderr.strip()}")
    if "lib/ungated_thing.py" not in r.stderr:
        failures.append(
            f"ungated refusal did not name the file: stderr={r.stderr.strip()}")
    if "LICENSE" in r.stderr:
        failures.append(
            f"ungated refusal named the licence: stderr={r.stderr.strip()}")
    return failures


def case_gated_code_selects(tmp: Path) -> list[str]:
    """A diff that touches only a tool with a per-file gate must select
    exactly the gates that gate prescribes, nothing more. bin/pr-ready
    is the per-file case that pins the selection contract."""
    repo = make_repo(tmp / "gated")
    # Touch pr-ready on a fresh branch; the gate `when`
    # `^(bin/pr-ready|tests/test_pr_ready\.py|tests/fixtures/gh_.*\.json)$`
    # selects only the pr-ready tests gate.
    git(repo, "checkout", "-q", "-b", "pr-ready-change")
    # Copy the actual bin/pr-ready from the repo so the file exists
    # for `git diff` to see.
    (repo / "bin").mkdir()
    shutil.copy(REPO_ROOT / "bin" / "pr-ready", repo / "bin" / "pr-ready")
    git(repo, "add", "bin/pr-ready")
    git(repo, "commit", "-q", "-m", "edit pr-ready")
    # VERIFY_BASE=origin/main selects against the same base gate-verify
    # uses in production; --list skips running the gates so the test
    # exercises selection without needing the test suites.
    r = subprocess.run(
        [str(GATE_VERIFY), str(repo), "--list"],
        env={"HOME": "/tmp", "PATH": "/usr/bin:/bin",
             "VERIFY_BASE": "origin/main"},
        capture_output=True, text=True)
    failures = []
    if r.returncode != 0:
        failures.append(
            f"gated code did not select cleanly: rc={r.returncode} "
            f"stderr={r.stderr.strip()} stdout={r.stdout.strip()}")
        return failures
    names = selected_names(r.stdout)
    # Selection is unchanged. Expected names come from the manifest, so the
    # suite list can grow without breaking this.
    if "pr-ready tests" not in names:
        failures.append(
            f"pr-ready diff did not select 'pr-ready tests': got {names!r}")
    # No gate whose `when` does not match bin/pr-ready should fire.
    import tomllib
    manifest_gates = tomllib.loads((repo / "gates.toml").read_text())["gates"]
    import re as _re
    expected = [g["name"] for g in manifest_gates
                if g.get("when") and _re.search(g["when"], "bin/pr-ready")]
    matched = selected_names_match(r.stdout, expected)
    if matched != set(expected):
        failures.append(
            f"pr-ready diff selected {names!r}, expected {expected!r}")
    return failures


def case_mixed_gateable_ungateable(tmp: Path) -> list[str]:
    """A diff with both gated code and a licence runs the gates and
    exits 0. The licence is not the reason the branch is gated, and it
    must not block selection."""
    repo = make_repo(tmp / "mixed")
    (repo / "bin").mkdir()
    shutil.copy(REPO_ROOT / "bin" / "pr-ready", repo / "bin" / "pr-ready")
    (repo / "LICENSE").write_text("MIT\n")
    git(repo, "add", "bin/pr-ready", "LICENSE")
    git(repo, "commit", "-q", "-m", "mixed")
    r = subprocess.run(
        [str(GATE_VERIFY), str(repo), "--list"],
        env={"HOME": "/tmp", "PATH": "/usr/bin:/bin",
             "VERIFY_BASE": "origin/main"},
        capture_output=True, text=True)
    failures = []
    if r.returncode != 0:
        failures.append(
            f"mixed diff refused: rc={r.returncode} stderr={r.stderr.strip()}")
        return failures
    names = selected_names(r.stdout)
    if "pr-ready tests" not in names:
        failures.append(
            f"mixed diff lost pr-ready selection: got {names!r}")
    return failures


def case_bin_doc_is_not_code(tmp: Path) -> list[str]:
    """A file under bin/ with a suffix that is not a source one is a
    document, not a script. The carve-out that makes no-suffix bin/ scripts
    gateable must not swallow bin/README.md, or a diff adding one is refused
    for having no gate to run on prose."""
    repo = make_repo(tmp / "bindoc", with_gates=False)
    (repo / "bin").mkdir(exist_ok=True)
    (repo / "bin" / "README.md").write_text("what lives here\n")
    git(repo, "add", "bin/README.md")
    git(repo, "commit", "-q", "-m", "a readme under bin")
    r = run_gate_verify(repo)
    failures = []
    if r.returncode != 0:
        failures.append(
            f"bin/README.md refused as ungated code: rc={r.returncode} "
            f"stderr={r.stderr.strip()}")
        return failures
    if "nothing to gate" not in r.stderr:
        failures.append(
            f"bin/README.md was classified as code: stderr={r.stderr.strip()}")
    return failures



def case_declared_gates_are_authoritative(tmp: Path) -> list[str]:
    failures = []
    for label, path, when in [
        ("rust", "src/lib.rs", r"\.rs$"),
        ("fixture", "tests/fixtures/gh_head.json", r"\.json$"),
        ("documentation", "README.md", r"\.md$"),
        ("unconditional", "LICENSE", ""),
    ]:
        repo = make_repo(tmp / label, with_gates=False)
        (repo / "gates.toml").write_text(
            f"[[gates]]\nname = 'sentinel'\nwhen = '{when}'\n"
            "run = 'sh -c \"echo GATE-RAN; exit 7\"'\n")
        git(repo, "add", "gates.toml")
        git(repo, "commit", "-q", "-m", "manifest")
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n")
        git(repo, "add", path)
        git(repo, "commit", "-q", "-m", "change")
        listed = run_gate_verify(repo, "--list")
        ran = run_gate_verify(repo)
        if listed.returncode or "sentinel" not in listed.stdout:
            failures.append(f"{label}: explicit gate was not selected: {listed}")
        if ran.returncode != 1 or "GATE-RAN" not in ran.stdout:
            failures.append(f"{label}: explicit gate failure was skipped: {ran}")
    return failures


def case_unknown_source_and_invalid_manifest_refuse(tmp: Path) -> list[str]:
    failures = []
    for label, manifest, path in [
        ("unknown-source", "", "src/lib.rs"),
        ("invalid-manifest", "invalid = [", "README.md"),
        ("missing-manifest", None, "README.md"),
    ]:
        repo = make_repo(tmp / label, with_gates=False)
        if manifest is None:
            (repo / "gates.toml").unlink()
        else:
            (repo / "gates.toml").write_text(manifest)
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "change")
        r = run_gate_verify(repo)
        if r.returncode == 0 or "nothing to gate" in r.stderr:
            failures.append(f"{label}: incorrectly accepted: {r}")
        if label in ("invalid-manifest", "missing-manifest"):
            # The refusal below exits non-zero even when the manifest cannot
            # be read, so the exit code alone cannot tell a reader failure
            # from a gate-selection verdict. The advice for an unwired
            # script must not appear over a manifest that never reached the
            # selection step.
            for phrase in ("no gate matches", "Add one to gates.toml"):
                if phrase in r.stderr:
                    failures.append(
                        f"{label}: manifest failure read as gate refusal: "
                        f"stderr={r.stderr.strip()}")
    return failures


def case_gated_code_with_ungated(tmp: Path) -> list[str]:
    """A diff with a gated bin/ tool and an ungated code path still runs
    the gate. The ungated path is not the reason to refuse; refusal is
    reserved for the case where no gate covers any code path."""
    repo = make_repo(tmp / "gated_with_ungated")
    (repo / "bin").mkdir()
    shutil.copy(REPO_ROOT / "bin" / "pr-ready", repo / "bin" / "pr-ready")
    (repo / "lib").mkdir()
    (repo / "lib" / "ungated_thing.py").write_text("# nothing\n")
    git(repo, "add", "bin/pr-ready", "lib/ungated_thing.py")
    git(repo, "commit", "-q", "-m", "gated plus ungated")
    r = subprocess.run(
        [str(GATE_VERIFY), str(repo), "--list"],
        env={"HOME": "/tmp", "PATH": "/usr/bin:/bin",
             "VERIFY_BASE": "origin/main"},
        capture_output=True, text=True)
    failures = []
    if r.returncode != 0:
        failures.append(
            f"gated+ungated refused: rc={r.returncode} stderr={r.stderr.strip()}")
        return failures
    names = selected_names(r.stdout)
    if "pr-ready tests" not in names:
        failures.append(
            f"gated+ungated lost pr-ready selection: got {names!r}")
    return failures


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for label, fn in [
            ("declared gates remain authoritative", case_declared_gates_are_authoritative),
            ("unknown source and invalid manifests refuse", case_unknown_source_and_invalid_manifest_refuse),
            ("docs-only diff", case_docs_only),
            ("ungated code refuses with the script named",
             case_ungated_code),
            ("gated code selects the matching gates",
             case_gated_code_selects),
            ("mixed gated code + licence still gates",
             case_mixed_gateable_ungateable),
            ("a doc under bin/ is not code", case_bin_doc_is_not_code),
            ("gated code plus ungated non-doc path still passes",
             case_gated_code_with_ungated),
        ]:
            try:
                failures.extend(fn(tmp))
            except subprocess.CalledProcessError as e:
                failures.append(f"{label}: git exited {e.returncode}: {e.stderr}")
    if failures:
        print("FAIL")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS gate-verify distinguishes docs-only, ungated code, "
          "and gated code")
    return 0


if __name__ == "__main__":
    sys.exit(main())
