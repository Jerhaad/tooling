#!/usr/bin/env python3
"""Pin that pipeline/triage.sh hands the prompt to the agent via --query-file
and surfaces a non-zero agent exit.

The script is the bridge between pipeline/review.sh (which writes
REVIEW_NOTES.md on a reviewer-* branch) and the next nightly run (which
files issues for findings the triage says are still real). The only
behaviour pin today is `bash -n` on the script, so a regression in the
helper invocation -- say the prompt going back to `-z` -- would pass
review on a parse alone. The two cases below cover what review round
#142 named as blockers and pin them end-to-end through the script.

Run directly: `python3 tests/test_triage_phase.py`.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "pipeline" / "triage.sh"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


# A shell stub shaped for the triage helper's contract. It records
# whether the prompt came through `--query-file` (and what the helper
# passed as its argv), so the test can pin both the file transport
# and the post-chat args the helper forwards. `exit_code` is the
# stub's exit status; the test runs the stub once with 0 (a happy
# path that drops findings) and once with 7 (the failure path the
# review comment named).
STUB_AGENT = textwrap.dedent("""\
    #!/usr/bin/env bash
    log="${FAKE_TRIAGE_AGENT_LOG:-/tmp/fake_triage_agent.log}"
    python3 - "$log" "$@" <<'PY'
import json, os, sys
log_path = sys.argv[1]
args = sys.argv[2:]
has_qf = "--query-file" in args
record = {
    "args": list(args),
    "has_query_file": has_qf,
    "query_file": (args[args.index("--query-file") + 1]
                   if has_qf else None),
    "exit_code": int(os.environ.get("FAKE_TRIAGE_AGENT_EXIT", "0")),
}
json.dump(record, open(log_path, "w"))
PY
    exit "${FAKE_TRIAGE_AGENT_EXIT:-0}"
""")


def setup(tmp: Path, *, agent_exit: int,
          multiword_agent: bool = False) -> tuple[Path, Path, dict]:
    """Build a bare origin with one reviewer-<date> branch carrying
    REVIEW_NOTES.md, a work clone, and an env that points
    $PIPELINE_AGENT at the failing-or-succeeding stub. Returns the
    env the test will pass to the script.

    The stub writes FAKE_TRIAGE_AGENT_LOG to a path under tmp so the
    test can read what argv it received, and reads
    FAKE_TRIAGE_AGENT_EXIT to decide its own status.

    `multiword_agent=True` is the live-host PIPELINE_AGENT shape
    (`<hermes_python> -m hermes_cli.main`): one command string, more
    than one word. The helper has to word-split the string, and a
    regression that quotes it as one program name makes the agent
    refuse to start."""
    origin = tmp / "origin.git"
    git(tmp, "init", "-q", "--bare", "--initial-branch=main", str(origin))
    seed = tmp / "seed"
    git(tmp, "clone", "-q", str(origin), str(seed))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(seed, "config", k, v)
    git(seed, "checkout", "-q", "-b", "main")
    (seed / "a.txt").write_text("one\n")
    git(seed, "add", "a.txt")
    git(seed, "commit", "-qm", "first")
    git(seed, "push", "-q", "origin", "main")
    # Reviewer branch the script will pick up -- oldest untriaged by
    # branch name, oldest first. The script exits 0 when nothing is
    # untriaged, so this branch is what makes the script reach the
    # hermes_query line at all.
    git(seed, "checkout", "-q", "-b", "reviewer-2000-01-01")
    (seed / "REVIEW_NOTES.md").write_text("### finding\ndetails")
    git(seed, "add", "REVIEW_NOTES.md")
    git(seed, "commit", "-qm", "review")
    git(seed, "push", "-q", "origin", "reviewer-2000-01-01")
    git(seed, "checkout", "-q", "main")

    work = tmp / "work"
    git(tmp, "clone", "-q", str(origin), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(work, "config", k, v)
    git(work, "remote", "set-head", "origin", "main")

    stub = tmp / "agent"
    stub.write_text(STUB_AGENT)
    stub.chmod(0o755)
    if multiword_agent:
        # Wrap the stub so the agent string passed as $PIPELINE_AGENT
        # has more than one word -- `bash <wrapper> -m stub_module`,
        # the shape of `<hermes_python> -m hermes_cli.main` on the live
        # host. The wrapper is a shell alias that execs the stub; the
        # helper has to word-split the agent string and pass `<stub>
        # <pre> chat <post>` for the stub to ever run.
        wrapper = tmp / "agent-wrapper"
        wrapper.write_text("#!/usr/bin/env bash\nexec \"" + str(stub) + "\" \"$@\"\n")
        wrapper.chmod(0o755)
        agent_cmd = f"bash {wrapper} -m stub_module"
    else:
        agent_cmd = str(stub)
    home = tmp / "home"
    home.mkdir()
    queue = tmp / "queue"
    state = tmp / "state"
    log_path = tmp / "agent.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PIPELINE_", "GPU_GROUP"))}
    env.update(HOME=str(home), TMPDIR=str(tmp),
               AGENT_PIPELINE_ENV=str(tmp / "no-env"),
               PIPELINE_REPO=str(work), PIPELINE_GH_REPO="owner/proj",
               PIPELINE_VERIFY="/bin/true",
               PIPELINE_AGENT=agent_cmd,
               PIPELINE_STATE_DIR=str(state),
               PIPELINE_QUEUE=str(queue),
               PIPELINE_LANE="triage-test",
               FAKE_TRIAGE_AGENT_LOG=str(log_path),
               FAKE_TRIAGE_AGENT_EXIT=str(agent_exit))
    return origin, work, env


def case_prompt_reaches_stub_via_query_file(failures: list) -> None:
    """End-to-end pin: pipeline/triage.sh hands the prompt to the
    agent via `--query-file PATH`, not as one argv string. A 200 KB
    prompt would otherwise hit MAX_ARG_STRLEN and the agent would
    never start; a 1 KB prompt hides that, so the assertion is on
    shape (`--query-file` present, `--query-file PATH` pointing at a
    file the test wrote) rather than on size. The stub records its
    argv; the helper has to leave `--query-file` there for the test
    to pass."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _, _, env = setup(tmp, agent_exit=0)
        r = subprocess.run(["bash", str(SCRIPT)], env=env,
                           capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(
                f"query-file: script exit {r.returncode}: "
                f"stdout={r.stdout!r} stderr={r.stderr!r}")
            return
        log_path = tmp / "agent.json"
        if not log_path.exists():
            failures.append(
                f"query-file: stub never ran; the script's helper "
                f"call did not reach the agent at all "
                f"(stdout={r.stdout!r} stderr={r.stderr!r})")
            return
        rec = json.loads(log_path.read_text())
        if not rec.get("has_query_file"):
            failures.append(
                "query-file: agent did not see --query-file in its "
                f"argv; the helper is back on -z or has dropped the "
                f"file transport. args={rec.get('args')!r}")
        qf = rec.get("query_file")
        if qf and not Path(qf).exists():
            failures.append(
                f"query-file: agent was pointed at {qf} which does "
                f"not exist; the helper is fabricating a path")
        if qf and not Path(qf).read_bytes():
            failures.append(
                f"query-file: agent was pointed at {qf} which is "
                f"empty; the helper wrote the prompt somewhere else")
        # The helper inserts `chat` between the caller's pre-chat args
        # and the post-chat args, with `--query-file` after `chat`.
        # If the helper's argv layout drifts, this is where it shows.
        args = rec.get("args") or []
        if "chat" not in args:
            failures.append(
                f"query-file: helper did not insert `chat` between "
                f"pre-chat and post-chat args; args={args!r}")
        # --skills and --yolo come from the script's post-chat args
        # and must reach the agent. A regression on the `--` sentinel
        # (review item 3) would land them before `chat` and the
        # assertion on `--skills after chat` would still pass; the
        # tighter assertion is that the helper *forwards* them at
        # all.
        if "--skills" not in args:
            failures.append(
                f"query-file: --skills missing from agent argv; "
                f"args={args!r}")
        if "--yolo" not in args:
            failures.append(
                f"query-file: --yolo missing from agent argv; "
                f"args={args!r}")
        # Tighten: the helper splits on the sentinel and forwards
        # post-chat args (`--skills`, `--yolo`) after `chat`. A
        # script that forgets the `--` lands the post-chat args in
        # pre-chat, where they reach the agent before `chat` and
        # `chat` parses them as its own arguments. The helper
        # still records them in argv either way, so this assertion
        # is the one that catches the review item 3 regression.
        if "chat" in args:
            chat_idx = args.index("chat")
            for post in ("--skills", "--yolo"):
                if post in args and args.index(post) < chat_idx:
                    failures.append(
                        f"query-file: {post} landed before `chat` "
                        f"in the agent's argv; the script forgot "
                        f"the `--` sentinel. args={args!r}")


def case_non_zero_agent_exit_is_reported(failures: list) -> None:
    """The script's `|| true` swallows the helper's exit code, but the
    helper surfaces it (item 1 in review round 1) and the script
    must recognise the failure on its way to writing the triage
    file. The agent exits 7, writes nothing to the triage queue,
    and the script's `[[ -s \"$out\" ]] || produced nothing` branch
    fires: the operator gets a line naming the run that failed and
    a work directory to inspect.

    A script that hides the failure (writes nothing, says nothing)
    is the regression the review names: the nightly triage would
    no-op and report no error. The stub exits 7 here; the helper
    surfaces it; the script reads the empty triage file as a
    failure and prints 'produced nothing' on stdout. The
    assertion is on the stdout line, not on the script's exit
    code (which is 0 either way because of `|| true`)."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _, _, env = setup(tmp, agent_exit=7)
        r = subprocess.run(["bash", str(SCRIPT)], env=env,
                           capture_output=True, text=True)
        log_path = tmp / "agent.json"
        if not log_path.exists():
            failures.append(
                f"non-zero: stub never ran; helper did not reach the "
                f"agent at all (stdout={r.stdout!r} "
                f"stderr={r.stderr!r})")
            return
        rec = json.loads(log_path.read_text())
        # The helper's contract: the agent's exit code surfaces in
        # the helper's return. The script wraps that call in
        # `|| true`, so the script's own exit code is not what to
        # assert on here. What the operator sees is the script's
        # stdout, and the script has to say the run produced
        # nothing when the helper returned non-zero AND the agent
        # did not write the triage file. A script that said
        # nothing would be the regression the review names.
        if "produced nothing" not in r.stdout:
            failures.append(
                f"non-zero: helper surfaced exit 7 but the script "
                f"did not say the run produced nothing; the agent "
                f"failure is hidden from the operator. "
                f"stdout={r.stdout!r} stderr={r.stderr!r}")
        # Belt-and-braces. The stub wrote a triage file (it wrote
        # nothing to the file pointed at by `$out`); the script
        # recognised that as 'produced nothing'. A regression
        # where the stub wrote *something* to `$out` would not
        # trigger the 'produced nothing' branch, so assert the
        # stub's contract was honoured (it wrote nothing) and the
        # branch fired on the right reason.
        state = tmp / "state"
        queue = tmp / "queue"
        triage_files = list(queue.glob("review-*.md"))
        if triage_files:
            failures.append(
                f"non-zero: stub was supposed to exit non-zero and "
                f"write nothing, but the script found "
                f"{[p.name for p in triage_files]} in the queue; "
                f"the test setup itself is wrong")


def case_multiword_pipeline_agent_starts_and_records_argv(failures: list) -> None:
    """End-to-end pin: a multi-word $PIPELINE_AGENT (e.g.
    `<hermes_python> -m hermes_cli.main`) starts and records the
    helper's argv. The helper has to word-split the agent string;
    without that word-split, the agent is run as one program name
    (`bash <wrapper> -m stub_module`), the shell exits 127 with
    "No such file or directory", the stub never runs, and the
    script no-ops while reporting nothing.

    The stub always writes `args`, so an `args` missing the
    helper's `chat` (or an empty record entirely) is the regression
    signature. The agent's argv starts with `-p <profile>` (the
    helper's first pre-chat arg) -- the helper must NOT have
    inserted anything between the agent command and the caller's
    pre-chat args, which is the failure mode review round 1 named
    in item 2."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        _, _, env = setup(tmp, agent_exit=0, multiword_agent=True)
        r = subprocess.run(["bash", str(SCRIPT)], env=env,
                           capture_output=True, text=True)
        log_path = tmp / "agent.json"
        if not log_path.exists():
            failures.append(
                f"multiword-agent: stub never ran; the wrapper did "
                f"not start (the helper did not word-split the "
                f"agent command). stdout={r.stdout!r} "
                f"stderr={r.stderr!r}")
            return
        rec = json.loads(log_path.read_text())
        args = rec.get("args") or []
        if "chat" not in args:
            failures.append(
                f"multiword-agent: agent argv is missing the "
                f"helper's `chat`; the wrapper's exec never ran "
                f"because the helper quoted the agent command as "
                f"one program name. args={args!r}")
        if not rec.get("has_query_file"):
            failures.append(
                f"multiword-agent: agent did not see --query-file "
                f"in its argv; the helper's word-split dropped the "
                f"prompt on the floor. args={args!r}")
        # The agent command is `bash <wrapper> -m stub_module`.
        # After word-split, the helper invokes `<wrapper> -m
        # stub_module -p triage-test chat --query-file ...`. The
        # wrapper does `exec <stub> "$@"`, so the stub's $@ is
        # `-m stub_module -p triage-test chat --query-file ...`
        # -- the wrapper consumed `bash <wrapper>`. The
        # assertion is on the position of the caller's words:
        # `-p` and the profile name must come immediately after
        # the agent command (`-m stub_module`), and the helper
        # must NOT have inserted anything between them.
        if args[:4] != ["-m", "stub_module", "-p", "triage-test"]:
            failures.append(
                f"multiword-agent: agent command + pre-chat args "
                f"should land as `'-m stub_module -p triage-test'` "
                f"in the stub's $@; the helper appears to have "
                f"inserted something between them or word-split "
                f"wrongly. args={args!r}")


def main() -> int:
    failures: list[str] = []
    if not SCRIPT.is_file():
        failures.append(f"missing script: {SCRIPT}")
        for f in failures:
            print(f"FAIL {f}")
        return 1
    case_prompt_reaches_stub_via_query_file(failures)
    case_non_zero_agent_exit_is_reported(failures)
    case_multiword_pipeline_agent_starts_and_records_argv(failures)
    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS triage.sh hands the prompt via --query-file and "
          "surfaces a non-zero agent exit")
    return 0


if __name__ == "__main__":
    sys.exit(main())