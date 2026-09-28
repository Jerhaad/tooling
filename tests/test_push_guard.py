#!/usr/bin/env python3
"""Pin bin/push-guard against the issue #68 deleted-files-on-push bug.

The bug: a squash that resets onto a moved origin/main produces a commit
whose tree still reflects the pre-squash tree, so on push its diff against
the branch's previous tip deletes everything main added since. Nothing
between the squash and `git push` saw it -- deletions are off the prose
and redaction passes' scope, remote-task is pre-squash, pr-ready never
runs for a PR that was never draft. The push is the first place this can
be caught for every PR.

The tool reads git's pre-push protocol: $1 is the remote name, stdin
carries lines of `<local ref> <local sha> <remote ref> <remote sha>`. For
each pushed branch ref (skipping deletions, main itself, and new
branches), it compares files main added since the branch's previous
push against files the push would delete, and refuses on any overlap.
The rule is a heuristic, so PUSH_GUARD_ACK=1 turns the refusal into a
warning.

Run directly: `python3 tests/test_push_guard.py`. No runner, no
dependency. No network -- a bare repository in a temp dir stands in for
the remote.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PUSH_GUARD = REPO_ROOT / "bin" / "push-guard"
HOOK = REPO_ROOT / "hooks" / "pre-push"

IDENTITY = {"GIT_AUTHOR_NAME": "push-guard-test",
            "GIT_AUTHOR_EMAIL": "push-guard-test@example.com",
            "GIT_COMMITTER_NAME": "push-guard-test",
            "GIT_COMMITTER_EMAIL": "push-guard-test@example.com"}


def git(cwd, *args, env=None, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, env=env,
                          check=check)


def env_with_identity(extra=None):
    e = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), **IDENTITY}
    if extra:
        e.update(extra)
    return e


def init_repo(parent):
    """Make a fresh non-bare repo with one base commit. Returns the repo
    path and the sha of the base commit."""
    repo = parent / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def head_of(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def setup_remote(repo):
    """Create a bare remote and wire it up. Returns the bare repo path."""
    remote = repo.parent / "remote.git"
    if remote.exists():
        shutil.rmtree(remote)
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    git(repo, "remote", "add", "origin", str(remote))
    # Push initial main so refs/remotes/origin/main exists locally.
    git(repo, "push", "-q", "origin", "main:main")
    git(repo, "update-ref",
        "refs/remotes/origin/main",
        head_of(repo))
    return remote


def run_guard(stdin_text, env_extra=None):
    """Invoke push-guard with a constructed stdin line. $1 (the remote
    name) is also passed as the script expects it."""
    env = env_with_identity(env_extra)
    return subprocess.run([str(PUSH_GUARD), "origin"],
                         input=stdin_text, capture_output=True,
                         text=True, env=env)


def push_line(local_ref, local_sha, remote_ref, remote_sha):
    return f"{local_ref} {local_sha} {remote_ref} {remote_sha}\n"


def fail(failures, msg, detail=""):
    failures.append(f"{msg}: {detail}" if detail else msg)


def assert_eq(label, want, got, failures):
    if want != got:
        fail(failures, label, f"want {want!r}, got {got!r}")


def assert_in(label, needle, haystack, failures):
    if needle not in haystack:
        fail(failures, label, f"{needle!r} not in {haystack!r}")


def assert_not_in(label, needle, haystack, failures):
    if needle in haystack:
        fail(failures, label, f"{needle!r} should not be in {haystack!r}")


def assert_rc(label, want, r, failures):
    if r.returncode != want:
        fail(failures, label,
             f"got {r.returncode}, stderr={r.stderr!r} stdout={r.stdout!r}")


# ---------------------------------------------------------------------------
# Helpers used by the cases to build the exact scenario the issue describes.
# ---------------------------------------------------------------------------


def case_issue68_reverted_files_refused(failures):
    """Case 1 of the issue: a squash that loses files main added since
    the branch's previous push must be refused, with the lost files named.

    Setup:
      - main has base.txt and a.txt
      - branch feat is created at commit P off main's tip
      - main gains x.txt (and y.txt) at commit M
      - feat is now stale: $refs/remotes/origin/main = M (with x.txt, y.txt)
      - branch P's last pushed tip is P (so remote_sha = P when pushing)
      - we then make the bad-squash commit: reset --soft onto M, keep
        P's tree (no x.txt, no y.txt), commit
      - feeding that to push-guard must refuse and list both x.txt and
        y.txt because main gained them after the last push and this push
        deletes them.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        # main reaches two-commit state: base.txt and a.txt.
        (repo / "a.txt").write_text("a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "a")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        # feat starts at P = main's tip. Push P to set remote_sha for later.
        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        # main moves: gain x.txt and y.txt after feat's last push.
        git(repo, "checkout", "-q", "main")
        (repo / "x.txt").write_text("x\n")
        (repo / "y.txt").write_text("y\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        # Reproduce the bad squash from the issue: reset --soft onto the
        # new main while the index still holds P's tree.
        git(repo, "checkout", "-q", "feat")
        git(repo, "reset", "-q", "--soft", "origin/main")
        git(repo, "commit", "-q", "-m", "bad squash")
        bad_sha = head_of(repo)

        line = push_line("refs/heads/feat", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line)

        assert_rc("issue68 refused", 1, r, failures)
        assert_in("issue68 names x.txt", "x.txt", r.stderr, failures)
        assert_in("issue68 names y.txt", "y.txt", r.stderr, failures)
        assert_in("issue68 names feat ref", "feat", r.stderr, failures)
        assert_in("issue68 explains ack",
                  "PUSH_GUARD_ACK=1",
                  r.stderr, failures)


def case_ack_overrides_refusal(failures):
    """Case 2: same push as case 1 with PUSH_GUARD_ACK=1 passes, but the
    same files are still named so the override is conscious."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        (repo / "a.txt").write_text("a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "a")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        git(repo, "checkout", "-q", "main")
        (repo / "x.txt").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "feat")
        git(repo, "reset", "-q", "--soft", "origin/main")
        git(repo, "commit", "-q", "-m", "bad squash")
        bad_sha = head_of(repo)

        line = push_line("refs/heads/feat", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, env_extra={"PUSH_GUARD_ACK": "1"})

        assert_rc("ack exit code", 0, r, failures)
        assert_in("ack still names x.txt", "x.txt", r.stderr, failures)
        assert_in("ack acknowledges",
                  "acknowledging",
                  r.stderr, failures)


def case_correct_rebase_keeps_gained_files(failures):
    """Case 3: rebasing onto the new main while keeping its newly added
    files means the push deletes nothing main added, and so it passes."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        (repo / "a.txt").write_text("a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "a")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        git(repo, "checkout", "-q", "main")
        (repo / "x.txt").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        # The correct rebase: rebase feat onto the new main; the
        # resulting tip still has x.txt.
        git(repo, "checkout", "-q", "feat")
        git(repo, "rebase", "--quiet", "origin/main")
        rebased_sha = head_of(repo)

        line = push_line("refs/heads/feat", rebased_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line)

        assert_rc("rebase exit", 0, r, failures)
        # x.txt is what we're protecting; the message should not name it.
        assert_not_in("rebase does not warn", "x.txt", r.stderr, failures)


def case_intended_deletion_of_old_file_passes(failures):
    """Case 4: the branch deletes a file that existed on main before
    its last push. That is an intended deletion predating the branch,
    and main did not add it after, so the intersection is empty and
    the push passes."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        # main has base.txt and old.txt -- the latter will be the file
        # the branch deletes.
        (repo / "old.txt").write_text("old\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "old file on main")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        # main moves, but with an unrelated new file. old.txt stays put.
        git(repo, "checkout", "-q", "main")
        (repo / "new_on_main.txt").write_text("new\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        # feat deletes old.txt (it existed before the last push, so the
        # deletion is intended).
        git(repo, "checkout", "-q", "feat")
        git(repo, "rm", "-q", "old.txt")
        git(repo, "commit", "-q", "-m", "drop old")
        new_sha = head_of(repo)

        line = push_line("refs/heads/feat", new_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line)

        assert_rc("intended-deletion exit", 0, r, failures)
        # The new file main added is not deleted by this branch, so it
        # should not be flagged.
        assert_not_in("intended-deletion silent on main-new file",
                      "new_on_main.txt", r.stderr, failures)


def case_harmless_pushes_pass(failures):
    """Case 5: new branches, branch deletions, and pushes of main itself
    each pass without engaging the check."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        # New branch push (remote sha is all zeros).
        git(repo, "checkout", "-q", "-b", "fresh")
        (repo / "fresh.txt").write_text("fresh\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "fresh")
        fresh_sha = head_of(repo)
        line_new = push_line(
            "refs/heads/fresh", fresh_sha,
            "refs/heads/fresh",
            "0" * 40)

        # Branch deletion push (local sha is all zeros).
        git(repo, "checkout", "-q", "main")
        line_del = push_line(
            "refs/heads/fresh", "0" * 40,
            "refs/heads/fresh", fresh_sha)

        # Push of main itself, even with deletions in it, is not the
        # check's concern.
        (repo / "to_drop.txt").write_text("drop\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "dropped on main")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))
        git(repo, "rm", "-q", "to_drop.txt")
        git(repo, "commit", "-q", "-m", "drop on main")
        main_sha = head_of(repo)
        line_main = push_line(
            "refs/heads/main", main_sha,
            "refs/heads/main",
            git(repo, "rev-parse",
                "refs/remotes/origin/main").stdout.strip())

        stdin_text = line_new + line_del + line_main
        r = run_guard(stdin_text)
        assert_rc("harmless pushes exit", 0, r, failures)


def case_end_to_end_push_refused(failures):
    """Case 6: the bad-squash branch actually pushed through a real
    `git push` with the pre-push hook installed; the remote ref must
    not move."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        (repo / "a.txt").write_text("a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "a")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "-b", "feat")
        (repo / "feat.txt").write_text("feat\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "feat on top")
        p_sha = head_of(repo)
        git(repo, "push", "-q", "origin", "feat")
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        git(repo, "checkout", "-q", "main")
        (repo / "x.txt").write_text("x\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "main moves")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        git(repo, "checkout", "-q", "feat")
        git(repo, "reset", "-q", "--soft", "origin/main")
        git(repo, "commit", "-q", "-m", "bad squash")

        # Wire the pre-push hook at this checkout's real hooks/.
        git(repo, "config",
            "core.hooksPath",
            str(REPO_ROOT / "hooks"))

        env = env_with_identity()
        before = git(repo, "ls-remote", "origin", "feat").stdout.strip()
        r = subprocess.run(["git", "-C", str(repo), "push", "origin", "feat"],
                           capture_output=True, text=True, env=env)
        after = git(repo, "ls-remote", "origin", "feat").stdout.strip()

        assert_rc("e2e push refused", 1, r, failures)
        assert_in("e2e push stderr names x.txt",
                  "x.txt", r.stderr, failures)
        assert_eq("e2e remote ref did not move", before, after, failures)


# ---------------------------------------------------------------------------
# Mutation tests: edit push-guard temporarily to confirm the intersection
# check is doing real work. Each restores the file before exiting.
# ---------------------------------------------------------------------------


def mutation_overbroad_rule(setup, run, restore, failures):
    """Apply a mutation, run the case, undo the mutation, verify the
    case flips its verdict."""
    setup()
    try:
        run()
    finally:
        restore()


def case_mutation_overbroad_rule_flags_intended_deletion(failures):
    """If the 'gained' set is replaced by every path on main, then a
    case-4 push (intended deletion of an old file) must also fail.
    This is the over-broad rule the check's structure avoids."""
    src = PUSH_GUARD.read_text()
    # Replace the gained computation with the full main tree.
    mutated = src.replace(
        'gained=$(git log --format= --name-only --diff-filter=A '
        '"$remote_sha".."$base_sha")',
        'gained=$(git ls-tree -r --name-only "$base_sha")')
    assert src != mutated, "expected to find the substitution target"
    try:
        PUSH_GUARD.write_text(mutated)
        case_intended_deletion_of_old_file_passes(failures)
    finally:
        PUSH_GUARD.write_text(src)


def case_mutation_no_intersection_skips_refusal(failures):
    """If the intersection check is removed, case 1 must NOT refuse:
    the protection comes from the intersection, not from the listing of
    deleted or gained files alone."""
    src = PUSH_GUARD.read_text()
    # Replace the intersection step with an unconditional `continue` so
    # we test what the intersection itself contributes.
    mutated = src.replace(
        'intersection=$(printf \'%s\\n%s\\n\' "$deleted" "$gained" \\\n'
        '\t\t| sort | uniq -d)',
        'intersection=""\n'
        '\t\t: # mutation: intersection always empty')
    assert src != mutated, "expected to find the substitution target"
    try:
        PUSH_GUARD.write_text(mutated)
        case_issue68_reverted_files_refused(failures)
    finally:
        PUSH_GUARD.write_text(src)


def main():
    failures: list = []
    if not PUSH_GUARD.is_file():
        failures.append(f"missing tool: {PUSH_GUARD}")
    if not HOOK.is_file():
        failures.append(f"missing hook: {HOOK}")
    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1

    # Sanity: the hook must invoke our push-guard. The mutation tests
    # edit push-guard, but they never edit the hook, and the end-to-end
    # case uses the real one.

    case_issue68_reverted_files_refused(failures)
    case_ack_overrides_refusal(failures)
    case_correct_rebase_keeps_gained_files(failures)
    case_intended_deletion_of_old_file_passes(failures)
    case_harmless_pushes_pass(failures)
    case_end_to_end_push_refused(failures)
    # Mutation tests last so the on-disk push-guard is restored before
    # main returns. They run the same cases as earlier, expecting the
    # opposite verdict.
    case_mutation_overbroad_rule_flags_intended_deletion(failures)
    case_mutation_no_intersection_skips_refusal(failures)

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS push-guard: deleted/gained intersection refused; "
          "ack overrides; rebase passes; intended-deletion passes; "
          "harmless refs pass; e2e push refused; mutations show the "
          "intersection does the work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
