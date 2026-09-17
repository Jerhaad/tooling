#!/usr/bin/env python3
"""Pin that every test suite in tests/ is reachable from gates.toml.

gate-verify reports the gates it selected, never the ones that do not exist, so
an unwired suite reads as a short run rather than a hole.

Checked both directions: a suite no `when` selects, and a gate whose `run` names
a file that is gone.

Run directly: `python3 tests/test_gate_coverage.py`.
"""
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATES = REPO_ROOT / "gates.toml"
TESTS = REPO_ROOT / "tests"

# Pins the manifest, not a tool, so no `when` should select it.
EXEMPT = {"test_gate_coverage.py"}

# bin/ and tests/ have per-file checks above, which the directory rule would
# only paper over. skills/ is content the installer symlinks, not code this
# repository has committed to gating. Hidden directories are VCS state.
DIR_EXEMPT = {"bin", "skills", "tests"} | {p.name for p in REPO_ROOT.iterdir()
                                          if p.name.startswith(".")}
SOURCE_EXTS = (".sh", ".py")


def main() -> int:
    failures = []
    doc = tomllib.loads(GATES.read_text())
    gates = doc.get("gates", [])
    if not gates:
        print("FAIL gates.toml declares no gates")
        return 1

    suites = sorted(p.name for p in TESTS.glob("test_*.py")
                    if p.name not in EXEMPT)

    # Matched against the repo-relative path `git diff --name-only` emits,
    # which is what gate-verify feeds these patterns.
    for suite in suites:
        rel = f"tests/{suite}"
        if not any(g.get("when") and re.search(g["when"], rel) for g in gates):
            failures.append(
                f"{rel} is named by no gate, so editing it runs nothing")

    # A gate naming a suite that is gone passes green having run nothing.
    for g in gates:
        for named in re.findall(r"tests/test_\w+\.py", g.get("run", "")):
            if not (REPO_ROOT / named).is_file():
                failures.append(
                    f"gate {g.get('name', '?')!r} runs {named}, which does not exist")

    # A bin/ tool no `when` matches is one gate-verify refuses to gate at all.
    for tool in sorted(p.name for p in (REPO_ROOT / "bin").iterdir() if p.is_file()):
        rel = f"bin/{tool}"
        if not any(g.get("when") and re.search(g["when"], rel) for g in gates):
            failures.append(f"{rel} matches no gate's `when`")

    # A top-level directory shipping source must be matched by some `when`, or
    # gate-verify refuses a diff that touches only it. The directory is the unit:
    # one matching `when` lifts the refusal, and per-file coverage here would be
    # a stricter rule this suite has not committed to.
    for entry in sorted(p for p in REPO_ROOT.iterdir()
                        if p.is_dir() and p.name not in DIR_EXEMPT):
        files = [str(f.relative_to(REPO_ROOT))
                 for f in entry.rglob("*")
                 if f.is_file() and f.suffix in SOURCE_EXTS]
        if not files:
            continue
        if not any(g.get("when") and any(re.search(g["when"], f) for f in files)
                   for g in gates):
            failures.append(
                f"{entry.name}/ ships source but no gate's `when` matches a file inside it")

    # A syntax gate that cannot fail is worse than none. Two forms do that, and
    # both passed review here before this check existed:
    #   `bash -n a.sh b.sh`   parses a.sh, takes the rest as $1, $2...
    #   `find -exec cmd +/\;` does not propagate cmd's exit status
    #
    # Named as well as probed: whether `-exec ... +` fails depends on which file
    # find lists first, so the behavioural run below cannot catch it reliably.
    for g in gates:
        run = g.get("run", "")
        if "bash -n" not in run:
            continue
        if re.search(r"bash -n [^|]*\*", run):
            failures.append(
                f"gate {g.get('name', '?')!r} passes a glob to `bash -n`, which "
                f"parses only the first file")
        if "-exec" in run:
            failures.append(
                f"gate {g.get('name', '?')!r} uses `find -exec`, which does not "
                f"propagate the exit status of what it runs")
        target = next((d for d in ("install", "pipeline")
                       if f" {d} " in run or f"'{d}'" in run or run.startswith(d)
                       or f" {d}/" in run), None)
        if target is None or not (REPO_ROOT / target).is_dir():
            continue
        with tempfile.TemporaryDirectory() as td:
            sandbox = Path(td)
            shutil.copytree(REPO_ROOT / target, sandbox / target)
            broken = sandbox / target / "_coverage_probe.sh"
            broken.write_text("#!/usr/bin/env bash\nif [ 1 -eq 1 ]; then\n")
            r = subprocess.run(["bash", "-c", run], cwd=sandbox,
                               capture_output=True, text=True)
            if r.returncode == 0:
                failures.append(
                    f"gate {g.get('name', '?')!r} passed with an unparseable "
                    f"script in {target}/; its command checks fewer files than "
                    f"its `when` selects")

    # The manifest itself. Every other gate is read from this file, so a `when`
    # broken here breaks gate selection everywhere -- and a change to it selected
    # nothing, including this check. It has to gate itself.
    for rel in ("gates.toml", "tests/test_gate_coverage.py"):
        if not any(g.get("when") and re.search(g["when"], rel) for g in gates):
            failures.append(
                f"{rel} is named by no gate, so breaking gate selection runs nothing")

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print(f"PASS gates.toml covers {len(suites)} suite(s) and every bin/ tool")
    return 0


if __name__ == "__main__":
    sys.exit(main())
