#!/usr/bin/env python3
"""Pin the hermes_query helper.

The helper takes the prompt as a file (`--query-file PATH`) rather than as
one argv string (`-z "$prompt"`), so a prompt over MAX_ARG_STRLEN (128 KiB)
can reach the agent intact. The tests below stub the agent with a Python
script that records its argv, reads the file the helper pointed it at, and
exits the way hermes's `chat -Q` does -- including the `session_id:` line
that callers downstream read.

Run directly: `python3 tests/test_hermes_query.py`.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "lib" / "common.sh"


# A shell-script stub that records every argv element and reads the
# file at --query-file (if any). A shell stub is the right shape: it
# runs as one program, so its $0 is the agent's argv[0] and $1, $2, ...
# are the words the helper passed after it. A python stub wrapped in
# `python3 script.py ...` would record only the words after the
# interpreter, hiding the helper's word-split.
STUB_AGENT = textwrap.dedent('''\
    #!/usr/bin/env bash
    # ARGS_JSON: a single line carrying the argv the helper passed
    # (in JSON). QUERY_FILE / QUERY_BYTES / QUERY_SHA follow on
    # separate lines so a missing --query-file is a missing line,
    # not a missing field inside one record.
    log="${FAKE_HERMES_LOG:-/tmp/fake_hermes_query.log}"
    python3 - "$log" "$0" "$@" <<'PY'
import hashlib, json, os, sys
log, argv0, *args = sys.argv[1:]
record = {
    "args": list(args),
    "argv0": argv0,
    "max_arg_bytes": max((len(a.encode("utf-8")) for a in args), default=0),
}
qf = None
for i, a in enumerate(args):
    if a == "--query-file" and i + 1 < len(args):
        qf = args[i + 1]
if qf and os.path.exists(qf):
    data = open(qf, "rb").read()
    record["query_file"] = qf
    record["query_bytes"] = len(data)
    record["query_sha"] = hashlib.sha256(data).hexdigest()
with open(log, "w") as fh:
    json.dump(record, fh)
    fh.write("\\n")
PY
    # Simulate `chat -Q`: a `session_id:` line on stdout before the answer.
    echo "session_id: stub-session-123"
    echo "STUB_ANSWER"
''')


def _fresh_stub(tmp: Path, body: str = STUB_AGENT) -> Path:
    stub = tmp / "fake_hermes"
    # The shebang comes from the body. Writing the body as-is keeps a
    # python stub and a shell stub interchangeable from the helper's
    # point of view.
    stub.write_text(body)
    stub.chmod(0o755)
    return stub


def _run_helper(tmp: Path, prompt: str, *helper_args: str,
                env: dict | None = None,
                stub_body: str | None = None,
                agent: str | None = None,
                timeout: int = 30) -> subprocess.CompletedProcess:
    """Run lib/common.sh's hermes_query against a stub agent. Returns
    the subprocess result so the test can read what was recorded.

    `agent` is the command string the helper is asked to run. It is
    word-split by the helper itself; pass `<stub>` for a single-word
    invocation or `bash <stub>` to verify the multi-word contract
    the live pipeline uses. The default is the single-word form
    because the shell stub has its own shebang."""
    work = tmp / "work"
    work.mkdir(exist_ok=True)
    prompt_file = work / "prompt.txt"
    prompt_file.write_bytes(prompt.encode("utf-8"))
    stdout_file = work / "out.stdout"
    stderr_file = work / "out.stderr"

    stub = _fresh_stub(tmp, stub_body or STUB_AGENT)

    runner = tmp / "runner.sh"
    runner.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        . "{HELPER}"
        # The fake stub is itself a python script; ./{stub.name} resolves
        # to the absolute stub path we passed as $1 below.
        hermes_query "$@"
    """))
    runner.chmod(0o755)

    full_env = {**os.environ,
                "FAKE_HERMES_LOG": str(tmp / "fake_hermes_query.log"),
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}"}
    if env:
        full_env.update(env)

    # If the caller did not include a `--` separator, append `--` so the
    # helper consumes everything before it as pre-chat. Tests that
    # intentionally exercise post-chat args must add `--` themselves.
    if helper_args and "--" not in helper_args:
        helper_args = (*helper_args, "--")

    # The agent is one command string the helper word-splits itself.
    # Default to the single-word form (the shell stub has its own
    # shebang); tests that exercise the multi-word contract the
    # live pipeline uses pass `agent=...` explicitly.
    if agent is None:
        agent = str(stub)

    return subprocess.run(
        [str(runner), agent, str(prompt_file),
         str(stdout_file), str(stderr_file), *helper_args],
        env=full_env, capture_output=True, text=True, timeout=timeout)


def _run_helper_direct(tmp: Path, prompt: str, *helper_args: str,
                       env: dict | None = None,
                       stub_body: str | None = None,
                       agent: str | None = None) -> subprocess.CompletedProcess:
    """Run the helper from a fresh bash that sources common.sh. Use when
    the caller needs fd 8 or fd 9 to be inherited (the timeout wrapper
    case)."""
    work = tmp / "work"
    work.mkdir(exist_ok=True)
    prompt_file = work / "prompt.txt"
    prompt_file.write_bytes(prompt.encode("utf-8"))
    stdout_file = work / "out.stdout"
    stderr_file = work / "out.stderr"
    if stub_body is None:
        stub_body = STUB_AGENT
    stub = _fresh_stub(tmp, stub_body)

    runner = tmp / "runner2.sh"
    runner.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        # Open fd 8 before sourcing -- the helper must not have closed
        # it on the child path (the lockdown belongs to the caller).
        exec 8>/tmp/lock_$$.lock
        . "{HELPER}"
        hermes_query "$@"
    """))
    runner.chmod(0o755)

    full_env = {**os.environ,
                "FAKE_HERMES_LOG": str(tmp / "fake_hermes_query.log"),
                "PATH": f"{tmp}:{os.environ.get('PATH', '')}"}
    if env:
        full_env.update(env)

    if agent is None:
        agent = str(stub)

    return subprocess.run(
        [str(runner), agent, str(prompt_file),
         str(stdout_file), str(stderr_file), *helper_args],
        env=full_env, capture_output=True, text=True, timeout=30)


def test_large_prompt_with_quotes_and_subs_reaches_stub_byte_for_byte():
    """A prompt over 200 KiB with `$()`, backticks and unbalanced quotes
    must reach the stub byte-for-byte. Every argv element must stay under
    4096 bytes -- anything larger hits environment limits or execve
    restrictions that the helper is supposed to avoid."""
    chunk = "alpha \"$(date)\" `whoami` 'unbal {brkt}'\n"
    prompt = (chunk * 6000)[:220_000]
    assert len(prompt) > 200_000, (
        f"fixture shorter than 200K bytes ({len(prompt)}); bump count")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        r = _run_helper(tmp, prompt, "--skills", "swe-test", "--yolo")
        assert r.returncode == 0, (
            f"helper exited {r.returncode}: stdout={r.stdout!r} "
            f"stderr={r.stderr!r}")

        import json
        rec = json.loads((tmp / "fake_hermes_query.log").read_text())
        assert "args" in rec, f"stub never recorded: {rec}"
        # The helper takes the agent as one command string and runs it
        # verbatim -- the words are exactly the caller's, no -m
        # hermes_cli.main appended, no chat inserted by the helper.
        # The default test agent is the shell stub itself (a single
        # word), so argv[0] is the stub path and the helper's
        # `chat --query-file …` line comes immediately after.
        # argv0 is what the stub recorded in its $0, which is the
        # path the helper actually executed.
        assert rec.get("argv0") and Path(rec["argv0"]).name == "fake_hermes", (
            f"helper should have invoked the stub by its path; got "
            f"argv0={rec.get('argv0')!r}, args={rec.get('args')!r}")
        assert "chat" in rec["args"], (
            f"helper should pass `chat` as the subcommand, got "
            f"{rec['args']!r}")
        assert "--query-file" in rec["args"], (
            f"helper should pass --query-file, got {rec['args']!r}")
        assert "--oneshot" in rec["args"], (
            f"helper should pass --oneshot, got {rec['args']!r}")
        assert "-Q" in rec["args"], (
            f"helper should pass -Q, got {rec['args']!r}")

        # The prompt file the helper pointed the agent at must contain
        # exactly what we wrote -- no shell interpretation, no truncation.
        assert rec["query_bytes"] == len(prompt.encode()), (
            f"stub received {rec['query_bytes']} bytes; expected "
            f"{len(prompt.encode())}")
        import hashlib
        want = hashlib.sha256(prompt.encode()).hexdigest()
        assert rec["query_sha"] == want, (
            "stub received different bytes than the test wrote (sha mismatch)")

        # No argv element must exceed 4096 bytes -- the test is over a
        # 200K prompt; if the helper put it on argv the entire chunk
        # would land in $1 of the child.
        assert rec["max_arg_bytes"] < 4096, (
            f"an argv element was {rec['max_arg_bytes']} bytes; the "
            f"helper should be carrying the prompt by --query-file, not "
            f"as an argv string")


def test_session_id_line_is_dropped_from_agents_stdout():
    """`chat -Q` writes `session_id: <id>` on stdout before the answer.
    The helper strips that line so callers reading stdout do not see it."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        r = _run_helper(tmp, "hello world")
        assert r.returncode == 0, r.stderr
        out = (tmp / "work" / "out.stdout").read_text()
        # The helper dropped the leading session_id line but the answer
        # ('STUB_ANSWER') survives exactly as the agent wrote it.
        assert out == "STUB_ANSWER\n", (
            f"unexpected helper stdout: {out!r}\n"
            f"the `session_id:` line should have been dropped")
        assert "session_id" not in out, (
            f"helper left a session_id reference in stdout: {out!r}")


def test_session_id_in_answer_after_first_line_is_kept():
    """An answer whose own body mentions `session_id: ...` after the
    first line must survive -- the helper strips the marker by *position*,
    not by pattern, so subsequent occurrences pass through untouched."""
    stub_body = textwrap.dedent('''\
        #!/usr/bin/env bash
        # First line is the -Q marker; the answer body follows on later
        # lines and includes another `session_id:` mention that must NOT
        # be dropped by the helper.
        echo "session_id: stub-session-123"
        echo "the answer mentions a token-shaped string \\`session_id: foo\\` here"
        echo "which is part of the answer body, not the marker"
    ''')
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        r = _run_helper(tmp, "test", stub_body=stub_body)
        assert r.returncode == 0, r.stderr
        out = (tmp / "work" / "out.stdout").read_text()
        # The first line (the marker) is dropped; subsequent occurrences
        # in the body remain.
        assert out.startswith("the answer mentions"), (
            f"helper dropped answer body, kept only the marker: {out!r}")
        assert "session_id: foo" in out, (
            f"helper stripped a mid-line `session_id:` mention from the "
            f"answer body -- it should only drop the leading marker: {out!r}")


def test_helper_does_not_close_fd_8_for_the_child():
    """The helper does not close fd 8 (GPU group lock). Callers that
    need it closed in the child add `8>&-` to their own invocation --
    hermes-implement keeps it open so the GPU lock spans retries, and
    the review scripts close it so the child cannot outlive its slot.

    fd 9 (review slot) is the same shape, but the test cannot easily
    stand up `acquire_review_slot` -- the slot lives in
    $AGENT_STATE_DIR/hermes-review-<profile>-<n>.lock. Cover fd 9 at
    the caller level in tests/test_hermes_scrutinize.py instead."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        body = textwrap.dedent('''\
            #!/usr/bin/env bash
            if [ -e /proc/self/fd/8 ]; then
                fd8=true
            else
                fd8=false
            fi
            log="${FAKE_HERMES_LOG:-/tmp/f.log}"
            printf '{"fd8": %s}\\n' "$fd8" >"$log"
            echo "session_id: abc"
            echo "OK"
        ''')
        r = _run_helper_direct(tmp, "test", stub_body=body)
        assert r.returncode == 0, r.stderr
        import json
        rec = json.loads((tmp / "fake_hermes_query.log").read_text())
        # fd 8 should be open by default. The helper does not close it:
        # callers that want it closed (the review scripts) add `8>&-`
        # themselves.
        assert rec["fd8"] is True, (
            "helper closed fd 8 in the child; callers cannot pass a "
            "held lock through to the next agent call")


def test_pre_chat_args_go_before_chat_post_chat_args_after():
    """The argv layout is `AGENT [pre-chat] chat [post-chat]` --
    profile/model flags go before, --skills/--yolo/--continue after.
    The agent command is what the caller passed (the helper adds
    nothing); here it's the shell stub. This is what the
    bin/hermes-implement retry loop relies on."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        # Use the helper's `--` convention: pre-chat before, post-chat
        # after. The runner helper above auto-appends `--` only when the
        # caller has none, so pass `--` explicitly here.
        r = _run_helper(tmp, "test",
                        "-p", "reviewer", "--",
                        "--skills", "swe-reviewer",
                        "--yolo")
        assert r.returncode == 0, r.stderr
        import json
        rec = json.loads((tmp / "fake_hermes_query.log").read_text())
        args = rec["args"]
        # The agent itself is argv[0] (the stub path); -p 'reviewer' must
        # come after it but before 'chat' (pre-chat). --skills and
        # --yolo must come after 'chat' (post-chat).
        chat_idx = args.index("chat")
        pi = args.index("-p")
        assert args[pi + 1] == "reviewer", args
        assert pi < chat_idx, f"-p must be before chat, got {args!r}"
        si = args.index("--skills")
        assert args[si + 1] == "swe-reviewer"
        assert si > chat_idx, f"--skills must be after chat, got {args!r}"
        yi = args.index("--yolo")
        assert yi > chat_idx, f"--yolo must be after chat, got {args!r}"


# A stub shaped for the multi-word-agent contract: it is a plain
# text file with a shebang, and the agent string the helper is
# asked to run is `bash <path>`. Word-splitting the string the
# caller composed has to produce exactly those two words, with
# nothing appended. The pipeline's $PIPELINE_AGENT is one such
# multi-word command (`<hermes_python> -m hermes_cli.main`).
MULTIWORD_AGENT_BODY = textwrap.dedent('''\
    #!/usr/bin/env bash
    # $0: this script's path (the second word of the agent command).
    # $1, $2, ...: what the helper's word-split gave the agent after
    # the program name -- nothing the helper added, just the caller's
    # pre/post-chat args. The stub records the full argv so the test
    # can assert the helper ran the agent command verbatim.
    log="${FAKE_HERMES_LOG:-/tmp/fake_hermes_query.log}"
    python3 - "$log" "$@" <<'PY'
import json, sys
log, *args = sys.argv[1:]
record = {"args": list(args), "argv0": sys.argv[0]}
with open(log, "w") as fh:
    json.dump(record, fh)
    fh.write("\\n")
PY
    echo "session_id: stub-session-123"
    echo "STUB_ANSWER"
''')


def test_helper_runs_multi_word_agent_command_verbatim():
    """The agent argument is a complete command line: the helper
    word-splits it and runs the result with nothing added. This
    is the contract the live pipeline relies on -- on this host
    PIPELINE_AGENT is `…/venv/bin/python -m hermes_cli.main`,
    and a previous shape tried to run that as a single program
    name (then appended `-m hermes_cli.main` a second time, with
    `-m` also being hermes's own model flag). With the new
    contract, a caller passes the whole command and the helper
    word-splits it; the agent sees those words in its argv and
    nothing more.

    The test uses `bash <stub>` as a stand-in for the pipeline's
    `python -m hermes_cli.main` shape: two words, a program and
    an argument. The stub records its full argv so we can assert
    the helper passed exactly those two words and nothing else
    (no extra `-m`, no extra module, no second `chat`)."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        stub = _fresh_stub(tmp, MULTIWORD_AGENT_BODY)
        r = _run_helper(
            tmp, "test",
            "-p", "reviewer", "--",
            "--skills", "swe-test", "--yolo",
            agent=f"bash {stub}")
        assert r.returncode == 0, (
            f"helper exited {r.returncode}; stdout={r.stdout!r} "
            f"stderr={r.stderr!r}")
        import json
        rec = json.loads((tmp / "fake_hermes_query.log").read_text())
        args = rec["args"]
        # The whole point: the helper did NOT add anything to the
        # agent command. The pipeline's $PIPELINE_AGENT is
        # `python -m hermes_cli.main`; if the helper still
        # appended `-m hermes_cli.main` like the old shape, the
        # agent would see `-m hermes_cli.main -m hermes_cli.main
        # chat --query-file ...`, and the second `-m` is
        # hermes's own model flag.
        # The script is invoked as `bash <stub> <pre> chat
        # <post>`. The script's $0 is the stub path; what the
        # helper added (the pre/chat/post words) is in args.
        # The script does not see `bash` itself (the shell
        # consumed it to start the script). The assertion is on
        # the relative position of the caller's words: the
        # agent's `-m hermes_cli.main` slot (here `-p reviewer`)
        # must come right after the stub, and the helper must
        # have inserted nothing extra in between.
        # argv0 (the script's $0) is the stub path.
        assert rec.get("argv0") == str(stub), (
            f"helper should have invoked the agent by its "
            f"interpreter; got argv0={rec.get('argv0')!r}, "
            f"expected {str(stub)!r}")
        # args[0] and args[1] are `-p reviewer` (the first pre-chat
        # args the helper passed). The script's $0 (the stub path)
        # is the argv0 above, not in args.
        assert args[0:2] == ["-p", "reviewer"], (
            f"pre-chat args should follow the agent command "
            f"unmodified; got args[0:2]={args[0:2]!r}, full={args!r}")
        # And the helper still inserted `chat` and the post-chat
        # args after `--`.
        assert "chat" in args, f"`chat` subcommand missing: {args!r}"
        assert "--query-file" in args, f"--query-file missing: {args!r}"
        assert "--skills" in args, f"--skills missing: {args!r}"
        assert "--yolo" in args, f"--yolo missing: {args!r}"
        # Critically: the helper must NOT have inserted its own
        # `-m hermes_cli.main` between the agent command and the
        # caller's pre-chat args. The script's $@ starts
        # directly with `-p`, not with anything the helper added.
        assert not any("hermes_cli" in a for a in args), (
            f"helper added its own module flag to the agent "
            f"command; got {args!r}")


# A shell-script stub that exits with a non-zero status and a
# distinctive stdout, so the test can assert (a) the helper propagated
# the exit code, and (b) the session_id-stripping still happened on
# the way out. The exit code is the value the review comment asked
# the helper to surface; a hermes_query that returns the status of
# its final `case` would return 0 instead.
FAILING_AGENT = textwrap.dedent('''\
    #!/usr/bin/env bash
    # Distinguishing answer so we can assert the helper still stripped
    # the session_id line and wrote the rest to the stdout file.
    echo "session_id: stub-session-123"
    echo "FAILING_ANSWER"
    exit 7
''')


def test_helper_propagates_agent_exit_status():
    """A failed agent (or one that `timeout` killed with status 124)
    must surface as hermes_query's exit status, not as 0. The previous
    shape returned the status of its final `case` (always 0) and let
    every caller think the agent had succeeded; bin/hermes-implement's
    retry loop and 124-handling depend on this code coming back
    unchanged, and bin/hermes-review-pr and hermes-scrutinize.sh use
    `RC=$?` to decide whether to fail. Pin it here with a stub that
    exits 7, and assert the helper returned 7 too.

    Uses a runner without `set -e`: a failed agent is the point of
    this test, and `set -e` would kill the runner before the helper
    can capture the exit code, which would mask the bug rather than
    pin it."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        work = tmp / "work"
        work.mkdir(exist_ok=True)
        prompt_file = work / "prompt.txt"
        prompt_file.write_bytes(b"test")
        stdout_file = work / "out.stdout"
        stderr_file = work / "out.stderr"

        stub = _fresh_stub(tmp, FAILING_AGENT)

        runner = tmp / "rc_runner.sh"
        # No `set -e`: the helper's job is to surface the agent's
        # non-zero status, and `set -e` here would kill the script
        # at the failing agent before the helper ever ran.
        runner.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            . "{HELPER}"
            hermes_query "$@"
        """))
        runner.chmod(0o755)

        env = {**os.environ,
               "FAKE_HERMES_LOG": str(tmp / "fake_hermes_query.log"),
               "PATH": f"{tmp}:{os.environ.get('PATH', '')}"}

        r = subprocess.run(
            [str(runner), str(stub), str(prompt_file),
             str(stdout_file), str(stderr_file), "--"],
            env=env, capture_output=True, text=True, timeout=30)
        # Without the fix, hermes_query would return 0; the agent's
        # `sys.exit(7)` is what the test expects to see propagated.
        assert r.returncode == 7, (
            f"hermes_query hid the agent's exit code (got "
            f"returncode={r.returncode}; expected 7). "
            f"stdout={r.stdout!r} stderr={r.stderr!r}")
        # The session_id-stripping is a separate path: a helper that
        # surfaces the exit code but stops stripping would still be
        # broken, so assert the answer body reached the stdout file.
        out = (tmp / "work" / "out.stdout").read_text()
        assert out == "FAILING_ANSWER\n", (
            f"helper returned {r.returncode} but the session_id line "
            f"was not stripped on the way out: {out!r}")


def main() -> int:
    failures: list[tuple[str, str]] = []
    cases = [
        ("hermes_query carries a 200 KB prompt with quotes/$()/backticks "
         "byte-for-byte through --query-file, no argv element over 4096 bytes",
         test_large_prompt_with_quotes_and_subs_reaches_stub_byte_for_byte),
        ("hermes_query drops the session_id: line that chat -Q writes on stdout",
         test_session_id_line_is_dropped_from_agents_stdout),
        ("hermes_query only drops the *first* session_id: line, not any "
         "session_id: mention that appears later in the answer",
         test_session_id_in_answer_after_first_line_is_kept),
        ("hermes_query leaves fd 8 (GPU lock) open for the child; callers "
         "close it themselves",
         test_helper_does_not_close_fd_8_for_the_child),
        ("hermes_query places -p PROFILE before `chat`, --skills/--yolo after",
         test_pre_chat_args_go_before_chat_post_chat_args_after),
        ("hermes_query propagates the agent's exit status, so a failing "
         "agent or one killed by timeout (124) looks like a failure to "
         "callers' `local rc=$?` and `RC=$?` checks",
         test_helper_propagates_agent_exit_status),
        ("hermes_query word-splits a multi-word agent command and runs it "
         "verbatim, with nothing added (the contract $PIPELINE_AGENT relies "
         "on -- e.g. `python -m hermes_cli.main`)",
         test_helper_runs_multi_word_agent_command_verbatim),
    ]
    for name, fn in cases:
        try:
            fn()
        except AssertionError as e:
            failures.append((name, str(e)))
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures.append((name, repr(e)))
            print(f"ERROR {name}: {e!r}")
        else:
            print(f"ok   {name}")
    if failures:
        return 1
    print("PASS hermes_query helper pins prompt-by-file transport")
    return 0


if __name__ == "__main__":
    sys.exit(main())
