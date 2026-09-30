#!/usr/bin/env python3
"""Pin the Gates workflow, whose check pr-ready promotes on, to the verdict
a local gate-verify gives."""
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "gates.yaml"
HELPER = REPO_ROOT / "lib" / "workflow_steps.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("_wfs", HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    text = WORKFLOW.read_text()
    steps, err = load_helper().extract_run_steps(text, "gates")
    failures = []
    if steps is None:
        failures.append(f"job 'gates' not readable: {err}")
    elif not any(s.strip() == "bin/gate-verify ." for s in steps):
        failures.append(f"no step runs `bin/gate-verify .`: {steps!r}")
    # gate-verify diffs HEAD against the base, so it needs the PR head and
    # the base's history in the checkout.
    for needle in ("ref: ${{ github.event.pull_request.head.sha }}",
                   "fetch-depth: 0",
                   "VERIFY_BASE: origin/${{ github.event.pull_request.base.ref }}"):
        if needle not in text:
            failures.append(f"missing `{needle}`")
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS gates workflow: runs gate-verify on the PR head, full history, "
          "against the PR's base")
    return 0


if __name__ == "__main__":
    sys.exit(main())
