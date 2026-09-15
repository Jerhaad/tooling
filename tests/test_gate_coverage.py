#!/usr/bin/env python3
"""Pin that every test suite in tests/ is reachable from gates.toml.

gate-verify reports the gates it selected, never the ones that do not exist, so
an unwired suite reads as a short run rather than a hole.

Checked both directions: a suite no `when` selects, and a gate whose `run` names
a file that is gone.

Run directly: `python3 tests/test_gate_coverage.py`.
"""
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATES = REPO_ROOT / "gates.toml"
TESTS = REPO_ROOT / "tests"

# Pins the manifest, not a tool, so no `when` should select it.
EXEMPT = {"test_gate_coverage.py"}


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

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print(f"PASS gates.toml covers {len(suites)} suite(s) and every bin/ tool")
    return 0


if __name__ == "__main__":
    sys.exit(main())
