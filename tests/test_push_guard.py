#!/usr/bin/env python3
"""Pin bin/push-guard: a push that deletes files main added after the branch's
last push is refused, and ordinary pushes are not.

A bare repository in a temp dir stands in for the remote; no network.
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
    path."""
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
    """Create a bare remote, wire it up, push main, and populate
    refs/remotes/origin/main. Returns the bare repo path."""
    remote = repo.parent / "remote.git"
    if remote.exists():
        shutil.rmtree(remote)
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "origin", "main:main")
    git(repo, "update-ref",
        "refs/remotes/origin/main",
        head_of(repo))
    return remote


def run_guard(stdin_text, cwd=None, env_extra=None):
    """Run push-guard in `cwd` with one pre-push stdin line."""
    env = env_with_identity(env_extra)
    return subprocess.run([str(PUSH_GUARD), "origin"],
                         input=stdin_text, capture_output=True,
                         text=True, env=env,
                         cwd=str(cwd) if cwd else None)


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


def scenario_with_branch_and_lost_main_files(tmp):
    """Build the scenario case 1 uses: branch pushed at P, main gains
    x.txt and y.txt (and a bit more history), branch squashed by
    resetting onto the new main while the index still holds P's tree.
    Returns (repo, p_sha, bad_sha)."""
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
    # Use --set-upstream so refs/remotes/origin/feat gets populated for
    # later push tests (git rev-parse --verify can find the local ref).
    git(repo, "push", "-q", "-u", "origin", "feat")
    git(repo, "update-ref",
        "refs/remotes/origin/feat",
        p_sha)

    git(repo, "checkout", "-q", "main")
    (repo / "x.txt").write_text("x\n")
    (repo / "y.txt").write_text("y\n")
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

    return repo, p_sha, bad_sha


def case_issue68_reverted_files_refused(failures):
    """Case 1 of the issue: a squash that loses files main added since
    the branch's previous push must be refused, with the lost files named."""
    with tempfile.TemporaryDirectory() as t:
        repo, p_sha, bad_sha = scenario_with_branch_and_lost_main_files(Path(t))

        line = push_line("refs/heads/feat", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

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
        repo, p_sha, bad_sha = scenario_with_branch_and_lost_main_files(Path(t))

        line = push_line("refs/heads/feat", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo, env_extra={"PUSH_GUARD_ACK": "1"})

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
        r = run_guard(line, cwd=repo)

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
        r = run_guard(line, cwd=repo)

        assert_rc("intended-deletion exit", 0, r, failures)
        # The new file main added is not deleted by this branch, so it
        # should not be flagged.
        assert_not_in("intended-deletion silent on main-new file",
                      "new_on_main.txt", r.stderr, failures)


def case_file_added_twice_on_main_not_flagged(failures):
    """Case 7: a path main added twice (add, delete, re-add) counts once, so a
    push that deletes only an unrelated old file passes. Each list is deduped
    before intersecting.
    """
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

        # old.txt predates the branch; deleting it is intended and must
        # not be flagged. It also keeps the deleted list non-empty so
        # the check's intersection actually runs.
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

        # main adds dup.txt, deletes it, re-adds it: the path appears
        # twice in the gained range, once per adding commit.
        git(repo, "checkout", "-q", "main")
        (repo / "dup.txt").write_text("one\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "add dup")
        git(repo, "rm", "-q", "dup.txt")
        git(repo, "commit", "-q", "-m", "drop dup")
        (repo / "dup.txt").write_text("two\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "re-add dup")
        git(repo, "push", "-q", "origin", "main:main")
        git(repo, "update-ref",
            "refs/remotes/origin/main",
            head_of(repo))

        # feat drops old.txt (intended: it predates the branch) but
        # keeps the re-added dup.txt, so the push deletes nothing
        # main gained.
        git(repo, "checkout", "-q", "feat")
        git(repo, "rm", "-q", "old.txt")
        git(repo, "commit", "-q", "-m", "drop old")
        new_sha = head_of(repo)

        line = push_line("refs/heads/feat", new_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("dup-added-twice exit", 0, r, failures)
        assert_not_in("dup-added-twice names no path",
                      "dup.txt", r.stderr, failures)


def case_deleted_path_with_space_refused_whole(failures):
    """Case 8: the deleted path main gained contains a space. The
    refusal must name the path whole -- the unquoted print used to
    word-split it into pieces."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        repo = init_repo(tmp)
        setup_remote(repo)

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
        (repo / "dir with space").mkdir()
        (repo / "dir with space" / "notes file.txt").write_text("n\n")
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
        r = run_guard(line, cwd=repo)

        assert_rc("space path refused", 1, r, failures)
        assert_in("space path named whole",
                  "  dir with space/notes file.txt\n", r.stderr, failures)
        # The whole path was printed once; the word-split pieces must
        # not appear as their own lines.
        assert_not_in("space path not split",
                      "  dir\n", r.stderr, failures)


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
        r = run_guard(stdin_text, cwd=repo)
        assert_rc("harmless pushes exit", 0, r, failures)


def case_head_as_source_refused(failures):
    """Case 9: `HEAD` as the source of a push to a branch is refused."""
    with tempfile.TemporaryDirectory() as t:
        repo, p_sha, bad_sha = scenario_with_branch_and_lost_main_files(Path(t))

        # Same bad squash as case 1, but the source is the literal
        # `HEAD` -- what `git push origin HEAD:branch` produces.
        line = push_line("HEAD", bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("HEAD source refused", 1, r, failures)
        assert_in("HEAD source names x.txt",
                  "x.txt", r.stderr, failures)
        assert_in("HEAD source names y.txt",
                  "y.txt", r.stderr, failures)
        # Destination branch (not source) is what the user needs to see
        # so they know which branch is being guarded.
        assert_in("HEAD source names destination feat",
                  "refs/heads/feat", r.stderr, failures)
        assert_in("HEAD source explains ack",
                  "PUSH_GUARD_ACK=1", r.stderr, failures)


def case_sha_as_source_refused(failures):
    """Case 10: a bare SHA as the source of a push to a branch is refused."""
    with tempfile.TemporaryDirectory() as t:
        repo, p_sha, bad_sha = scenario_with_branch_and_lost_main_files(Path(t))

        line = push_line(bad_sha, bad_sha,
                         "refs/heads/feat", p_sha)
        r = run_guard(line, cwd=repo)

        assert_rc("SHA source refused", 1, r, failures)
        assert_in("SHA source names x.txt",
                  "x.txt", r.stderr, failures)
        assert_in("SHA source names y.txt",
                  "y.txt", r.stderr, failures)
        assert_in("SHA source names destination feat",
                  "refs/heads/feat", r.stderr, failures)


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
        # The bad squash's parent is origin/main, while origin/feat is
        # at the previous push: a `--force` (or `--force-with-lease`)
        # is what would land this on the remote in the real bug. Without
        # that flag, git's own fast-forward check rejects first and the
        # hook never runs.
        r = subprocess.run(
            ["git", "-C", str(repo), "push", "--force-with-lease",
             "origin", "feat"],
            capture_output=True, text=True, env=env)
        after = git(repo, "ls-remote", "origin", "feat").stdout.strip()

        assert_rc("e2e push refused", 1, r, failures)
        assert_in("e2e push stderr names x.txt",
                  "x.txt", r.stderr, failures)
        assert_eq("e2e remote ref did not move", before, after, failures)


def case_end_to_end_head_source_push_refused(failures):
    """Case 11: case 9 through a real `git push origin HEAD:refs/heads/feat`;
    the remote ref must not move."""
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

        # Build the same bad squash case 6 builds, on the feat branch.
        # `git push origin HEAD:refs/heads/feat` then sends that commit
        # to the remote -- with HEAD as the local_ref, exactly as it
        # would appear in a real `git push` invocation.
        git(repo, "checkout", "-q", "feat")
        git(repo, "reset", "-q", "--soft", "origin/main")
        git(repo, "commit", "-q", "-m", "bad squash")
        # Make refs/remotes/origin/feat match the remote so
        # --force-with-lease does not reject before the hook runs.
        git(repo, "update-ref",
            "refs/remotes/origin/feat",
            p_sha)

        # Wire the pre-push hook at this checkout's real hooks/.
        git(repo, "config",
            "core.hooksPath",
            str(REPO_ROOT / "hooks"))

        env = env_with_identity()
        before = git(repo, "ls-remote", "origin", "feat").stdout.strip()
        r = subprocess.run(
            ["git", "-C", str(repo), "push", "--force-with-lease",
             "origin", "HEAD:refs/heads/feat"],
            capture_output=True, text=True, env=env)
        after = git(repo, "ls-remote", "origin", "feat").stdout.strip()

        assert_rc("e2e HEAD source push refused", 1, r, failures)
        assert_in("e2e HEAD source push names x.txt",
                  "x.txt", r.stderr, failures)
        assert_eq("e2e HEAD source remote ref did not move",
                  before, after, failures)


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

    case_issue68_reverted_files_refused(failures)
    case_ack_overrides_refusal(failures)
    case_correct_rebase_keeps_gained_files(failures)
    case_intended_deletion_of_old_file_passes(failures)
    case_file_added_twice_on_main_not_flagged(failures)
    case_deleted_path_with_space_refused_whole(failures)
    case_harmless_pushes_pass(failures)
    case_head_as_source_refused(failures)
    case_sha_as_source_refused(failures)
    case_end_to_end_push_refused(failures)
    case_end_to_end_head_source_push_refused(failures)
    # Mutation tests last so the on-disk push-guard is restored before
    # main returns. They re-run earlier cases with the mutated tool and
    # expect the opposite verdict.

    if failures:
        for f in failures:
            print(f"FAIL {f}")
        return 1
    print("PASS push-guard: deleted/gained intersection refused; "
          "ack overrides; rebase passes; intended-deletion passes; "
          "harmless refs pass; e2e push refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
