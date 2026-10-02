#!/usr/bin/env python3
"""Pin the Gates workflow, whose check pr-ready promotes on, to the verdict
a local gate-verify gives."""
import importlib.util
import re
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


def _step_blocks(text: str) -> list[tuple[int, list[str]]]:
    """Return (start_line, body_lines) for each `      - ` step in `text`.

    Used to inspect the `if:` guard on the step carrying `--all` -- the
    bare substring check below would also accept a stray `--all` mention
    in a comment, and the workflow had to actually pass `--all` only
    outside pull_request.
    """
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if re.match(r"^      - ", ln)]
    blocks = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        blocks.append((start, lines[start:end]))
    return blocks


def main() -> int:
    text = WORKFLOW.read_text()
    steps, err = load_helper().extract_run_steps(text, "gates")
    failures = []
    if steps is None:
        failures.append(f"job 'gates' not readable: {err}")
    else:
        if not any(s.strip() == "bin/gate-verify ." for s in steps):
            failures.append(f"no step runs `bin/gate-verify .`: {steps!r}")
        if not any(s.strip() == "bin/gate-verify . --all" for s in steps):
            failures.append(f"no step runs `bin/gate-verify . --all`: {steps!r}")

    # gate-verify diffs HEAD against the base, so it needs the PR head and
    # the base's history in the checkout. The ref string can be the bare
    # PR head sha (PR-only workflows) or a `||` form that falls back to
    # github.sha for push/schedule events (issue #108).
    for needle in ("github.event.pull_request.head.sha",
                   "fetch-depth: 0",
                   "VERIFY_BASE: origin/${{ github.event.pull_request.base.ref }}"):
        if needle not in text:
            failures.append(f"missing `{needle}`")

    # New triggers (issue #108): the workflow must also run on push to
    # main and on a weekly schedule, not only on pull_request.
    if "push:" not in text:
        failures.append("workflow has no `push:` trigger")
    if "schedule:" not in text:
        failures.append("workflow has no `schedule:` trigger")
    # The push trigger must scope to main so feature branches do not pay
    # the every-gate cost on every push.
    if re.search(r"branches:\s*\n\s*-\s*main", text) is None:
        failures.append("push trigger does not scope to `main`")

    # --all must only be passed when the event is not pull_request. The
    # step's own block must carry an `if:` guard that excludes
    # pull_request -- not a comment, not a docstring.
    has_all_step = steps is not None and any(
        s.strip() == "bin/gate-verify . --all" for s in steps)
    if not has_all_step:
        failures.append("no `bin/gate-verify . --all` step found")
    else:
        guarded = False
        for _start, body in _step_blocks(text):
            joined = "\n".join(body)
            if "bin/gate-verify . --all" not in joined:
                continue
            # An `if:` line on the step body that excludes pull_request.
            for ln in body[1:]:
                if not ln.startswith("        "):
                    continue
                stripped = ln.strip()
                if not stripped.startswith("if:"):
                    continue
                if "pull_request" not in stripped:
                    continue
                # Any mention of pull_request in an `if:` is a guard,
                # but only `!=` keeps --all away from PRs.
                if "!=" in stripped:
                    guarded = True
                break
        if not guarded:
            failures.append(
                "`bin/gate-verify . --all` step is not guarded to "
                "non-pull_request runs")

    # The plain `bin/gate-verify .` step (PR runs) must be the one that
    # uses the PR's base ref as VERIFY_BASE -- a push run that diffs
    # against the PR base would be a different verdict than the workflow
    # reports. We already asserted the substring above; this is the
    # cross-check that the same step carries it.
    pr_step_uses_pr_base = False
    for _start, body in _step_blocks(text):
        joined = "\n".join(body)
        if "bin/gate-verify ." in joined and "--all" not in joined:
            if "github.event.pull_request.base.ref" in joined:
                pr_step_uses_pr_base = True
                break
    if not pr_step_uses_pr_base:
        failures.append(
            "the PR `bin/gate-verify .` step does not use the PR base ref "
            "as VERIFY_BASE")

    # Concurrency: a single `gates-` group for every non-PR run would
    # collapse a push and a schedule tick into one queue, and an empty
    # PR number would do the same. The key must fall back to the ref
    # when no PR number is present.
    if "github.event.pull_request.number || github.ref" not in text:
        failures.append(
            "concurrency group does not fall back to github.ref when no "
            "pull_request number is present")

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS gates workflow: runs gate-verify on the PR head, full history, "
          "against the PR's base; --all only outside pull_request; "
          "push and schedule triggers wired")
    return 0


if __name__ == "__main__":
    sys.exit(main())
