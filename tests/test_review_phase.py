#!/usr/bin/env python3
"""Pin that the review phase's prompt stays under the one-argument cap.

Linux refuses an execve whose single argument passes MAX_ARG_STRLEN (128 KiB),
and the previous review the prompt carries forward only grows. The previous
review and the changed-file list therefore travel as files the prompt names.

Each case runs the real pipeline/review.sh against a local bare origin, with
$PIPELINE_AGENT a stub that records the prompt it was given, follows the paths
it names, and writes REVIEW_NOTES.md the way the real agent would.

Run directly: `python3 tests/test_review_phase.py`.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "pipeline" / "review.sh"
MAX_ARG_STRLEN = 131072

STUB_AGENT = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, re, sys
    args = sys.argv[1:]
    prompt = args[args.index("-z") + 1]
    seen = {"prompt_bytes": len(prompt.encode())}
    for name in ("previous-review.md", "changed-files.txt"):
        m = re.search(r"(\\S+/" + re.escape(name) + r")", prompt)
        if m and os.path.exists(m.group(1)):
            seen[name] = open(m.group(1)).read()
            seen[name + ":path"] = m.group(1)
    json.dump(seen, open(os.environ["STUB_RECORD"], "w"))
    notes = "# Review\\n\\n" + seen.get("previous-review.md", "") + "\\n### new finding\\n"
    open(os.path.join(os.environ["PIPELINE_REVIEW_WORK"], "REVIEW_NOTES.md"), "w").write(notes)
""")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def setup(tmp: Path, prev_notes: str | None):
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
    if prev_notes is not None:
        git(seed, "checkout", "-q", "-b", "reviewer-2000-01-01")
        (seed / "REVIEW_NOTES.md").write_text(prev_notes)
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
    home = tmp / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PIPELINE_", "GPU_GROUP"))}
    env.update(HOME=str(home), TMPDIR=str(tmp),
               AGENT_PIPELINE_ENV=str(tmp / "no-env"),
               PIPELINE_REPO=str(work), PIPELINE_GH_REPO="owner/proj",
               PIPELINE_VERIFY="true", PIPELINE_AGENT=str(stub),
               PIPELINE_REVIEW_WORK=str(work),
               PIPELINE_STATE_DIR=str(tmp / "state"),
               PIPELINE_QUEUE=str(tmp / "queue"),
               STUB_RECORD=str(tmp / "record.json"))
    return origin, work, env


def run(env):
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)


def case_large_previous_review_reaches_the_agent(failures):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        prev = "### old finding\n" + ("x" * 99 + "\n") * 2000  # 200 KB
        origin, work, env = setup(tmp, prev)
        r = run(env)
        if r.returncode != 0:
            failures.append(f"large-previous: exit {r.returncode}: {r.stdout}{r.stderr}")
            return
        rec = json.loads((tmp / "record.json").read_text())
        if rec["prompt_bytes"] >= MAX_ARG_STRLEN // 8:
            failures.append(f"large-previous: prompt is {rec['prompt_bytes']} bytes")
        if rec.get("previous-review.md", "").strip() != prev.strip():
            failures.append("large-previous: the agent did not get the previous review")
        if Path(rec["previous-review.md:path"]).is_relative_to(work):
            failures.append("large-previous: context file sits inside the checkout")
        if Path(rec["previous-review.md:path"]).exists():
            failures.append("large-previous: context file left behind after the run")
        branch = [b for b in git(origin, "branch", "--format=%(refname:short)").split()
                  if b.startswith("reviewer-") and b != "reviewer-2000-01-01"]
        if not branch or "### old finding" not in git(origin, "show", f"{branch[0]}:REVIEW_NOTES.md"):
            failures.append(f"large-previous: no review pushed carrying the old finding: {branch}")


def case_whole_repository_scope_lists_files_through_a_file(failures):
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        origin, work, env = setup(tmp, None)
        r = run(env)
        if r.returncode != 0:
            failures.append(f"whole-repo: exit {r.returncode}: {r.stdout}{r.stderr}")
            return
        rec = json.loads((tmp / "record.json").read_text())
        if rec.get("changed-files.txt", "").split() != ["a.txt"]:
            failures.append(f"whole-repo: changed-file list was {rec.get('changed-files.txt')!r}")
        if "previous-review.md" in rec:
            failures.append("whole-repo: a previous review appeared with none on origin")


def main() -> int:
    failures: list[str] = []
    case_large_previous_review_reaches_the_agent(failures)
    case_whole_repository_scope_lists_files_through_a_file(failures)
    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
