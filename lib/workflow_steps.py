#!/usr/bin/env python3
"""Advisory drift report between a [[task]]'s command and the CI job it mirrors.

A `run:` step the command cannot reach is the dangerous case -- CI grew, the
gate did not -- and gets the recommendation block. A reached line the workflow
does not run is the local-extras case and is listed as informational. Entries
in the manifest's `except` list are excluded from both directions and listed
as deliberate, so a `cargo run -p migrate` that the workflow carries but the
local gate rightly omits does not fire.

Parsing is conservative:
  * The workflow's `run:` values are extracted by indentation, not by a YAML
    parser. PyYAML is not installed in this environment and a library pull
    is out of scope; do not "fix" this by replacing the hand-rolled parser
    with PyYAML without first installing it everywhere the tool runs. A
    permissive parser that missed a real `run:` would stay green, which is
    the failure the comparison is supposed to surface.
  * `just --show <recipe>` is one level deep. `just` answers for its own
    recipes and only the workflow needs reading.

Exit is always 0. A project without `[task.mirrors]` reaches bash with empty
TASK_MIRRORS_* and the bash wrapper exits silently before this script runs.
"""
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


def extract_run_steps(workflow_text: str, job_name: str) -> tuple[list[str] | None, str | None]:
    """Return the list of `run:` strings for the named job, or (None, msg).

    Each step is a single string. For a `run: |` literal block the string is
    the joined body lines; for a `run: >` folded block it is the body lines
    joined with spaces (matching how the YAML interpreter folds them).
    """
    lines = workflow_text.splitlines()
    n = len(lines)
    steps: list[str] = []
    jobs_idx: int | None = None
    for i, ln in enumerate(lines):
        if re.match(r"^jobs\s*:\s*$", ln):
            jobs_idx = i
            break
    if jobs_idx is None:
        return None, "workflow has no `jobs:` key"

    # Walk down until the named job's line. A sibling job header at the same
    # indent is not a reason to stop -- the target may be a later sibling. We
    # only stop when the indent drops below 2 spaces, which means we have
    # left the `jobs:` section. A line at 4-or-more spaces of indent is
    # inside some job's body and cannot itself be a job header.
    job_re = re.compile(r"^  ([A-Za-z0-9_.-]+)\s*:\s*(.*)$")
    job_line = None
    for i in range(jobs_idx + 1, n):
        ln = lines[i]
        if not ln.strip():
            continue
        # Anything indented less than 2 spaces is back at the top level --
        # we have walked past every job. A sibling job header IS a line at
        # 2 spaces, not less, so this check does not abort the search
        # prematurely.
        if not ln.startswith("  "):
            break
        # Inside a job body (4+ spaces). Not a candidate header.
        if ln.startswith("   "):
            continue
        m = job_re.match(ln)
        if not m:
            continue
        if m.group(1) == job_name:
            job_line = i
            break
    if job_line is None:
        return None, f"workflow has no job named {job_name!r}"

    steps_line = None
    for j in range(job_line + 1, n):
        ln = lines[j]
        # Another job header at exactly 2 spaces of indent ends this job's
        # body -- we have walked into the next sibling. A `steps:` block
        # cannot appear at that indent (it lives under the job name, so at
        # 4 spaces), so this check is safe to run first.
        if ln.startswith("  ") and not ln.startswith("   "):
            break
        if re.match(r"^    steps\s*:\s*$", ln):
            steps_line = j
            break
    if steps_line is None:
        return None, f"job {job_name!r} has no `steps:` list"

    i = steps_line + 1
    while i < n:
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        # Anything indented less than 6 spaces ends the steps list: either a
        # sibling job header (2 spaces) or a top-level key (0 spaces). A line
        # at 6 spaces is a step start.
        if not ln.startswith("      "):
            break
        m = re.match(r"^      - \s*(\w+)\s*:\s*(.*)$", ln)
        if not m:
            i += 1
            continue
        key, rest = m.group(1), m.group(2)
        # Walk forward to the next step start or to the end of the steps list.
        j = i + 1
        while j < n:
            ln2 = lines[j]
            if re.match(r"^      - ", ln2):
                break
            if ln2.strip() and not ln2.startswith("     "):
                break
            j += 1
        block = lines[i + 1:j]

        run_value = _extract_run_from_step(key, rest, block)
        if run_value is not None:
            steps.append(run_value)
        i = j

    return steps, None


def _extract_run_from_step(start_key: str, start_rest: str, block: list) -> str | None:
    """Return the `run:` value from one step's lines, or None if absent.

    A step's first key may itself be `run` (`      - run: <value>` or block),
    in which case `start_key == 'run'` and `start_rest` is the inline value (or
    empty). Otherwise the first key is something else (`name:`, `uses:`, ...) and
    a continuation `        run:` may follow.
    """
    if start_key == "run":
        # Step is `      - run: ...` (or `      - run: |` style; the block
        # indicator lives in `start_rest`).
        if start_rest.strip() in ("|", ">"):
            return _read_block(block, indicator=start_rest.strip(), indent=8)
        if start_rest:
            return start_rest
        # Empty inline value but no `|` / `>` -- unusual. Treat as a single
        # empty step so the report still names it.
        return ""

    # Look for `        run:` within the step's continuation lines.
    run_idx: int | None = None
    run_text: str | None = None
    for k, bl in enumerate(block):
        m = re.match(r"^        run\s*:\s*(.*)$", bl)
        if m:
            run_idx = k
            run_text = m.group(1).strip()
            break
    if run_idx is None:
        return None

    if run_text in ("|", ">"):
        return _read_block(block[run_idx + 1:], indicator=run_text, indent=10)
    if run_text == "":
        return None
    return run_text


def _read_block(lines: list, indicator: str, indent: int) -> str:
    """Read a `|` or `>` block scalar body.

    YAML literal (`|`) preserves newlines as `\n`; folded (`>`) folds to a
    single space. Body lines are everything indented at `indent` spaces; a
    blank line ends the block. Each body line is stripped of its leading
    `indent` spaces.
    """
    body = []
    for ln in lines:
        if not ln.strip():
            break
        m = re.match(rf"^ {{{indent}}}(.*)$", ln)
        if m:
            body.append(m.group(1))
    if indicator == ">":
        return " ".join(body)
    return "\n".join(body)


# ---------------------------------------------------------------------------
# Reaching the command: expand one level of `just <recipe>` if applicable.
# ---------------------------------------------------------------------------

JUST_RECIPE_RE = re.compile(r"^just\s+([A-Za-z0-9_.-]+)\b")


def reached_items(command: str, worktree: Path):
    """Return (items, status) where status is one of 'matched', 'unparsed'.

    `matched` means we resolved the command to a list of reached shell lines.
    `unparsed` means the command is not a `just` recipe and we cannot decompose
    it further, so the command itself is reported as a single opaque item --
    we can name the workflow's steps the command does not reach, but not the
    other way around.

    A `just <recipe>` command is expanded one level deep via `just --show`. The
    output strips the recipe's leading TAB from each line; continuation lines
    ending with `\\` are joined, comment lines (`#`) and blank lines are dropped.
    """
    m = JUST_RECIPE_RE.match(command.strip())
    if not m:
        return [command], "matched"  # opaque single item; we still name it
    recipe = m.group(1)
    just_bin = shutil.which("just")
    if not just_bin:
        return None, "no-just"
    try:
        r = subprocess.run(
            [just_bin, "--show", recipe],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        return None, f"just-show-failed:{recipe}"
    except subprocess.TimeoutExpired:
        return None, "just-show-timeout"
    return _parse_just_show(r.stdout), "matched"


def _parse_just_show(body: str) -> list[str]:
    """Parse `just --show` output into one reached-item per logical line.

    `just --show` re-indents each recipe body line with 4 leading spaces. We
    strip those, then join lines ending with `\\` (line continuation), then
    drop blank lines and lines whose first non-whitespace character is `#`.
    The first line of the output is the recipe header (`<recipe>:`) and is
    dropped.
    """
    raw_lines = body.splitlines()
    items = []
    pending = None
    for ln in raw_lines:
        # `just --show` prefixes every body line with 4 spaces. Strip them.
        if ln.startswith("    "):
            content = ln[4:]
        elif ln.startswith(" "):
            content = ln.lstrip()
        else:
            content = ln
        if pending is not None:
            # The previous line ended with `\` -- append.
            pending = pending + " " + content.strip()
        else:
            pending = content.strip()
        if pending.endswith("\\"):
            # Drop the backslash and continue collecting.
            pending = pending[:-1].rstrip()
            continue
        # Finalise this item.
        if pending.strip() and not pending.lstrip().startswith("#"):
            items.append(pending.strip())
        pending = None
    if pending is not None and pending.strip() and not pending.lstrip().startswith("#"):
        items.append(pending.strip())
    # Drop the recipe header (first non-empty line, ends with `:`).
    if items and items[0].endswith(":"):
        items = items[1:]
    return items


# ---------------------------------------------------------------------------
# Diff and report.
# ---------------------------------------------------------------------------

def normalise(step: str) -> str:
    """Collapse internal whitespace so `cargo test  --workspace` matches `cargo test --workspace`.

    We do not tokenise shell -- `just` is the only one who knows what the
    command means. We only fold runs of whitespace so trivial re-formatting
    does not look like drift.
    """
    return re.sub(r"\s+", " ", step.strip())


def split_step(step: str) -> list[str]:
    """Break a multi-line step into its individual shell-command lines.

    A `run: |` literal block is one string with `\n`-separated commands; each
    is a distinct shell invocation the comparison should treat as its own
    item. A `run: >` folded block has already been joined with spaces and is
    one logical command. Single-line steps return themselves.
    """
    if "\n" not in step:
        return [step]
    return [ln for ln in step.split("\n") if ln.strip()]


def apply_except(items: list[str], except_subs: list[str]) -> tuple[list[str], list[str]]:
    """Return (kept, deliberate). An item containing any `except` substring is
    pulled into `deliberate` and listed under that label."""
    kept, deliberate = [], []
    for it in items:
        if any(sub in it for sub in except_subs):
            deliberate.append(it)
        else:
            kept.append(it)
    return kept, deliberate


def report(workflow_steps: list[str], reached: list[str],
           except_subs: list[str], workflow_path: str, job_name: str,
           command: str, status: str) -> int:
    """Print the advisory report. Always returns 0."""
    print(f"==> drift report for {job_name!r} in {workflow_path}")
    print(f"    task command: {command}")
    if status != "matched":
        # When we cannot compare, show the workflow steps the user must read
        # themselves and a line that says so explicitly. This is its own
        # status -- not "no drift", not "drift", but "I could not answer".
        print(f"    status: unable to compare ({status})")
        if workflow_steps:
            print("    workflow `run:` steps:")
            for s in workflow_steps:
                for ln in split_step(s):
                    print(f"      - {ln}")
        print("    re-run with `just` on PATH or with a command the tool can parse")
        return 0

    wf_kept, wf_delib = apply_except(workflow_steps, except_subs)
    rd_kept, rd_delib = apply_except(reached, except_subs)

    # Split multi-line `|` literal blocks into per-command lines so a single
    # shell invocation in the workflow can match a single reached line.
    # Deliberate steps stay as one item each so the report's `deliberate`
    # block names the step the user wrote, not each of its commands.
    wf_lines = [ln for s in wf_kept for ln in split_step(s)]
    rd_lines = [ln for s in rd_kept for ln in split_step(s)]

    wf_set = {normalise(s) for s in wf_lines}
    rd_set = {normalise(s) for s in rd_lines}

    # "Workflow runs but command does not reach" -- the dangerous direction.
    missing = [s for s in wf_lines if normalise(s) not in rd_set]
    # "Command reaches but workflow does not run" -- local extras, informational.
    extra = [s for s in rd_lines if normalise(s) not in wf_set]

    # Deliberate omissions are always listed -- the user named them and
    # wants to see them -- even when there is no real drift left to report.
    if wf_delib or rd_delib:
        print("    deliberate (excluded by `except`):")
        for s in wf_delib:
            for ln in split_step(s):
                print(f"      [workflow only] {ln}")
        for s in rd_delib:
            for ln in split_step(s):
                print(f"      [command only] {ln}")

    if not missing and not extra:
        if wf_delib or rd_delib:
            print("    status: in sync (drift accounted for by `except`)")
        else:
            print("    status: in sync")
        return 0

    if missing:
        print("    RECOMMEND: add these workflow steps to the task command")
        for s in missing:
            print(f"      + {s}")
    if extra:
        print("    informational: command reaches these but the workflow does not run them")
        for s in extra:
            print(f"      - {s}")

    print("    status: drift")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--command", required=True)
    p.add_argument("--workflow", required=True,
                   help="path to the workflow file, relative to the worktree")
    p.add_argument("--job", required=True)
    p.add_argument("--worktree", required=True,
                   help="absolute path to the worktree (used as `just`'s cwd)")
    p.add_argument("--except", dest="except_subs", action="append", default=[])
    args = p.parse_args()

    worktree = Path(args.worktree).resolve()
    wf_path = worktree / args.workflow
    if not wf_path.is_file():
        print(f"==> drift report: workflow file not found: {wf_path}",
              file=sys.stderr)
        return 0

    try:
        wf_text = wf_path.read_text()
    except OSError as e:
        print(f"==> drift report: cannot read workflow: {e}", file=sys.stderr)
        return 0

    steps_or_none, err = extract_run_steps(wf_text, args.job)
    if err is not None or steps_or_none is None:
        msg = err or "workflow parse returned no steps"
        print(f"==> drift report: {msg}", file=sys.stderr)
        print("    status: unable to compare (workflow-parse-failed)",
              file=sys.stderr)
        return 0
    steps_list = steps_or_none

    reached_raw, status = reached_items(args.command, worktree)
    reached: list[str] = reached_raw if reached_raw is not None else []
    if not reached:
        reached = [args.command]

    return report(
        workflow_steps=steps_list,
        reached=reached,
        except_subs=args.except_subs,
        workflow_path=args.workflow,
        job_name=args.job,
        command=args.command,
        status=status,
    )


if __name__ == "__main__":
    sys.exit(main())
