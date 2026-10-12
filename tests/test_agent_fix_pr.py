#!/usr/bin/env python3
"""Pin what agent-fix-pr pushes, refuses, and records for one review round.

The contract: a round pushes only commits the lane added on top of the PR's
head, fast-forward, after the gate passes; replies are posted only after that
push, or alone when the lane pushed back on everything, one malformed entry
never costing the others; every round, pushed or not,
is one ledger line, and the ledger caps the rounds.

The PR lives in a local bare repository standing in for origin. `gh` is a fake
that answers from canned JSON through the real `jq`, the implementer is a fake
that does whatever the test's FAKE_LANE says, and the gate is a fake exiting
FAKE_GATE_RC.

Run directly: `python3 tests/test_agent_fix_pr.py`.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin" / "agent-fix-pr"
REPO = "owner/proj"
BRANCH = "feature"

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_GH_LOG"], "a") as f:
        f.write(json.dumps(args) + "\\n")
    canned = json.load(open(os.environ["FAKE_GH_DATA"]))
    def emit(doc):
        if "--jq" in args:
            flt = args[args.index("--jq") + 1]
            sys.stdout.write(subprocess.run(["jq", "-r", flt], input=json.dumps(doc),
                                            capture_output=True, text=True, check=True).stdout)
        else:
            print(json.dumps(doc))
    if args[:2] == ["pr", "view"]:
        emit(canned["pr"])
    elif args[0] == "api" and "-X" not in args:
        path = args[1]
        emit(canned["comments"] if path.endswith("/comments") else canned["review"])
    sys.exit(0)
""")

FAKE_IMPLEMENTER = textwrap.dedent("""\
    #!/usr/bin/env bash
    # --worktree is the tree the lane is handed.
    while [[ $# -gt 0 ]]; do [[ $1 == --worktree ]] && wt=$2; shift; done
    work=$(mktemp -d)
    echo "fixed it" >"$work/HANDOVER.md"
    cd "$wt"
    case "$FAKE_LANE" in
    commit)
        echo fix >>file.txt; git commit -qam "Fix the thing"
        cp "$FAKE_REPLIES" "$work/replies.json" ;;
    amend)
        echo fix >>file.txt; git commit -q --amend -am "Rewritten" ;;
    answer)
        cp "$FAKE_REPLIES" "$work/replies.json" ;;
    none) ;;
    esac
    echo "$work"
""")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def setup(tmp: Path, *, body="Closes #7.", lane="commit", gate_rc=0, replies=None):
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    wt = tmp / "wt"
    subprocess.run(["git", "clone", "-q", str(origin), str(wt)], check=True)
    git(wt, "config", "user.email", "t@example")
    git(wt, "config", "user.name", "t")
    git(wt, "checkout", "-q", "-b", BRANCH)
    (wt / "file.txt").write_text("start\n")
    git(wt, "add", "file.txt")
    git(wt, "commit", "-qm", "start")
    git(wt, "push", "-q", "-u", "origin", BRANCH)
    head = git(wt, "rev-parse", "HEAD")

    data = tmp / "gh.json"
    data.write_text(json.dumps({
        "pr": {"headRefName": BRANCH, "headRefOid": head, "body": body, "state": "OPEN"},
        "review": {"user": {"login": "reviewer"}, "commit_id": head, "body": "Please fix."},
        "comments": [
            {"id": 11, "path": "file.txt", "line": 1, "body": "[P1] wrong", "in_reply_to_id": None},
            {"id": 12, "path": "file.txt", "line": 1, "body": "an earlier reply", "in_reply_to_id": 11},
        ],
    }))
    fakebin = tmp / "fakebin"
    fakebin.mkdir()
    for name, text in (("gh", FAKE_GH), ("implementer", FAKE_IMPLEMENTER)):
        (fakebin / name).write_text(text)
        (fakebin / name).chmod(0o755)
    gate = fakebin / "gate"
    gate.write_text(f"#!/bin/sh\nexit {gate_rc}\n")
    gate.chmod(0o755)

    state = tmp / "state"
    ledger_dir = state / "fix-pr" / f"{REPO.replace('/', '_')}-5"
    replies_src = tmp / "replies.json"
    replies_src.write_text(json.dumps(replies if replies is not None else
                                      [{"comment_id": 11, "body": "Fixed."}]))
    env = dict(os.environ,
               PATH=f"{fakebin}:{os.environ['PATH']}",
               AGENT_STATE_DIR=str(state),
               TOOLS_ENV=str(tmp / "no-env"),
               PIPELINE_IMPLEMENTER=str(fakebin / "implementer"),
               AGENT_FIX_PR_VERIFY=str(gate),
               FAKE_GH_LOG=str(tmp / "gh.log"),
               FAKE_GH_DATA=str(data),
               FAKE_LANE=lane,
               FAKE_REPLIES=str(replies_src))
    return wt, origin, head, env, ledger_dir


def run(env, wt, *extra, review=True):
    args = ["--review", "99"] if review else []
    return subprocess.run([str(BIN), "--repo", REPO, "--pr", "5", *args,
                           "--worktree", str(wt), *extra],
                          env=env, capture_output=True, text=True)


def set_gate(tmp, script):
    gate = tmp / "fakebin" / "gate"
    gate.write_text(f"#!/bin/sh\n{script}\n")


def gh_calls(tmp):
    log = tmp / "gh.log"
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def ledger(ledger_dir):
    return [json.loads(l) for l in (ledger_dir / "rounds.jsonl").read_text().splitlines()]


def test_pushes_a_fast_forward_and_replies_after():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp)
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        new = git(origin, "rev-parse", BRANCH)
        assert new != head and git(wt, "merge-base", "--is-ancestor", head, new) == ""
        entry = ledger(ld)[-1]
        assert entry["verdict"] == "pushed" and entry["replied"] == 1, entry
        posts = [c for c in gh_calls(tmp) if "-X" in c]
        assert posts and posts[0][3].endswith("/comments/11/replies"), posts


def test_brief_carries_only_top_level_comments():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp)
        # The implementer sees the brief as its --extra argument.
        spy = tmp / "fakebin" / "implementer"
        spy.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >\"$SPY\"\nmktemp -d\n")
        env["SPY"] = str(tmp / "args")
        run(env, wt)
        args = (tmp / "args").read_text()
        assert "comment 11 at file.txt:1" in args, args
        assert "an earlier reply" not in args


def test_refuses_a_rewritten_history():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, lane="amend")
        r = run(env, wt)
        assert r.returncode == 1
        assert git(origin, "rev-parse", BRANCH) == head
        assert ledger(ld)[-1]["verdict"] == "history rewritten"
        assert not [c for c in gh_calls(tmp) if "-X" in c], "no reply without a push"


def test_failed_gate_is_recorded_and_not_pushed():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, gate_rc=1)
        assert run(env, wt).returncode == 1
        assert git(origin, "rev-parse", BRANCH) == head
        assert ledger(ld)[-1]["gate"] == "fail"


def test_round_cap_counts_every_round():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp, lane="none")
        for _ in range(2):
            assert run(env, wt, "--max-rounds", "2").returncode == 1
        r = run(env, wt, "--max-rounds", "2")
        assert r.returncode == 4, r.stderr
        assert len(ledger(ld)) == 2


def test_refuses_a_tree_not_at_the_pr_head():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp)
        (wt / "file.txt").write_text("local\n")
        git(wt, "commit", "-qam", "someone's work")
        assert run(env, wt).returncode == 3


def test_refuses_a_pr_naming_no_issue():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp, body="No reference here.")
        assert run(env, wt).returncode == 2


def test_one_malformed_reply_does_not_cost_the_rest():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp, replies=[
            {"comment_id": "not-a-number", "body": "x"},
            {"comment_id": 11},
            7,
            {"comment_id": 11, "body": "Fixed."},
        ])
        assert run(env, wt).returncode == 0
        assert ledger(ld)[-1]["replied"] == 1



def test_replies_alone_are_a_finished_round():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, lane="answer", replies=[
            {"comment_id": 11, "body": "Left as is: file.txt:1 already says start."}])
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        assert git(origin, "rev-parse", BRANCH) == head
        entry = ledger(ld)[-1]
        assert entry["verdict"] == "answered" and entry["replied"] == 1, entry
        assert entry["pushed"] is False and entry["gate"] == "skipped", entry
        posts = [c for c in gh_calls(tmp) if "-X" in c]
        assert posts and posts[0][3].endswith("/comments/11/replies"), posts


def test_a_round_with_neither_commit_nor_replies_fails():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp, lane="none")
        assert run(env, wt).returncode == 1
        assert ledger(ld)[-1]["verdict"] == "no commit"
        assert not [c for c in gh_calls(tmp) if "-X" in c]


def test_a_reply_naming_a_commit_the_branch_lacks_is_skipped():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp, lane="answer", replies=[
            {"comment_id": 11, "body": "Fixed in 1a2b3c4."},
            {"comment_id": 11, "body": "Left as is: issue 1234567 settled it."}])
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        assert ledger(ld)[-1]["replied"] == 1
        assert "naming a commit the branch does not have" in r.stderr


def test_models_come_from_the_runtime_command_or_are_null():
    for script, expected in (("echo '[\"m1\", \"m2\"]'", ["m1", "m2"]),
                             ("echo 'not json'", None),
                             ("echo '{\"a\": 1}'", None),
                             (None, None)):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            wt, _, _, env, ld = setup(tmp)
            if script is not None:
                cmd = tmp / "fakebin" / "models"
                cmd.write_text(f"#!/bin/sh\n{script}\n")
                cmd.chmod(0o755)
                env["AGENT_FIX_PR_MODELS"] = str(cmd)
            assert run(env, wt).returncode == 0
            assert ledger(ld)[-1]["models"] == expected, (script, ledger(ld)[-1])


def test_implementer_setting_is_word_split():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp)
        spy = tmp / "fakebin" / "implementer"
        spy.write_text("#!/usr/bin/env bash\necho \"$LANE_OPTION\" >\"$SPY\"\nmktemp -d\n")
        env["SPY"] = str(tmp / "seen")
        env["PIPELINE_IMPLEMENTER"] = f"env LANE_OPTION=from-runtime {spy}"
        run(env, wt)
        assert (tmp / "seen").read_text().strip() == "from-runtime"


def test_refuses_to_run_without_an_implementer():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp)
        del env["PIPELINE_IMPLEMENTER"]
        assert run(env, wt).returncode == 2
        assert not (ld / "rounds.jsonl").exists()


def test_resume_pushes_a_round_whose_gate_could_not_run():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, gate_rc=1)
        assert run(env, wt).returncode == 1
        lane_commit = git(wt, "rev-parse", "HEAD")
        set_gate(tmp, "exit 0")
        r = run(env, wt, "--resume", review=False)
        assert r.returncode == 0, r.stderr
        assert git(origin, "rev-parse", BRANCH) == lane_commit
        first, resumed = ledger(ld)
        assert resumed["resumed"] and resumed["round"] == first["round"] == 1
        assert resumed["start"] == head and resumed["replied"] == 1, resumed
        # The resumed line is not a round: the cap still has room for round 2,
        # once the fake PR reports the head the resume pushed.
        data = json.loads(Path(env["FAKE_GH_DATA"]).read_text())
        data["pr"]["headRefOid"] = lane_commit
        Path(env["FAKE_GH_DATA"]).write_text(json.dumps(data))
        assert run(env, wt, "--max-rounds", "1").returncode == 4
        assert run(env, wt, "--max-rounds", "2").returncode == 0


def test_resume_refuses_with_nothing_unpushed():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp)
        assert run(env, wt, "--resume", review=False).returncode == 2
        assert run(env, wt).returncode == 0
        assert run(env, wt, "--resume", review=False).returncode == 2


def test_resume_refuses_a_tree_that_moved():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp, gate_rc=1)
        run(env, wt)
        (wt / "file.txt").write_text("moved\n")
        git(wt, "commit", "-qam", "someone's next commit")
        set_gate(tmp, "exit 0")
        assert run(env, wt, "--resume", review=False).returncode == 3


def test_gate_finds_this_checkouts_tools_by_name():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, ld = setup(tmp)
        set_gate(tmp, "command -v agent-fix-pr >/dev/null")
        assert run(env, wt).returncode == 0
        assert ledger(ld)[-1]["gate"] == "pass"


def test_detach_returns_at_once_and_the_round_lands_in_the_ledger():
    import time
    for resume in (False, True):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            wt, _, _, env, ld = setup(tmp, gate_rc=1 if resume else 0)
            if resume:
                run(env, wt)
                set_gate(tmp, "exit 0")
            # A lane slow enough that --status sees the round running.
            spy = tmp / "fakebin" / "implementer"
            spy.write_text("sleep 2\n" + spy.read_text().split("\n", 1)[1])
            spy.write_text("#!/usr/bin/env bash\n" + spy.read_text())
            extra = ("--resume", "--detach") if resume else ("--detach",)
            r = run(env, wt, *extra, review=not resume)
            assert r.returncode == 0, (resume, r.stdout, r.stderr)
            status = [str(BIN), "--status", "--repo", REPO, "--pr", "5"]
            deadline = time.time() + 30
            while subprocess.run(status, env=env, capture_output=True).returncode == 5:
                assert time.time() < deadline, "the detached round never finished"
                time.sleep(0.2)
            assert ledger(ld)[-1]["verdict"] == "pushed", (resume, ledger(ld))
            assert ledger(ld)[-1]["resumed"] is resume


def test_next_round_builds_on_an_unpushed_round():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, origin, head, env, ld = setup(tmp, gate_rc=1)
        assert run(env, wt).returncode == 1
        first_end = ledger(ld)[-1]["end"]
        set_gate(tmp, "exit 0")
        r = run(env, wt)
        assert r.returncode == 0, r.stderr
        second = ledger(ld)[-1]
        assert second["pr_head"] == head and second["start"] == first_end, second
        assert git(origin, "rev-parse", BRANCH) == second["end"]
        assert git(wt, "merge-base", "--is-ancestor", first_end, second["end"]) == ""


def test_next_round_refuses_work_on_top_of_an_unpushed_round():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        wt, _, _, env, _ = setup(tmp, gate_rc=1)
        run(env, wt)
        (wt / "file.txt").write_text("someone else\n")
        git(wt, "commit", "-qam", "someone's work")
        assert run(env, wt).returncode == 3

if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # a crash is a failure too, not a silent exit
                failed += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
