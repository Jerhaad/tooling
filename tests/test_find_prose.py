#!/usr/bin/env python3
"""Pin that find_prose.py reads Rust and SQL the way it reads the languages it
already handled, and that a session-link to the agent that wrote the change is
flagged as ``provenance``.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
FIND_PROSE = REPO_ROOT / "skills" / "condense-prose" / "find_prose.py"
IDENTITY = {"GIT_AUTHOR_NAME": "find-prose-test",
            "GIT_AUTHOR_EMAIL": "find-prose-test@example.com",
            "GIT_COMMITTER_NAME": "find-prose-test",
            "GIT_COMMITTER_EMAIL": "find-prose-test@example.com"}


def fail(failures: list[str], msg: str, detail: str = "") -> None:
    failures.append(f"{msg}: {detail}" if detail else msg)


def run_find_prose(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Invoke find_prose.py as a subprocess so the test exercises the CLI surface that the condense gate calls."""
    return subprocess.run(
        [sys.executable, str(FIND_PROSE), "--json", *args],
        cwd=str(cwd), capture_output=True, text=True, check=False,
    )


def run_find_prose_text(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Invoke find_prose.py without ``--json``."""
    return subprocess.run(
        [sys.executable, str(FIND_PROSE), *args],
        cwd=str(cwd), capture_output=True, text=True, check=False,
    )


def blocks_for(proc: subprocess.CompletedProcess) -> list[dict]:
    """Parse --json output; an empty stdout is a failure because it means
    the scanner read no prose at all, the very symptom this test guards."""
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"non-JSON output from find_prose.py: "
                             f"{proc.stdout[:200]!r} (stderr={proc.stderr!r})") from exc


# The end-of-report line is the only structured field on the text path: ``N of
# M prose blocks flagged, K words in play.``.
TOTALS = re.compile(r"(\d+) of (\d+) prose blocks flagged")


def totals_for(proc: subprocess.CompletedProcess) -> tuple[int, int]:
    """Return ``(flagged, total)`` from the prose report. ``flagged`` is the
    count the gate reads; ``total`` is the population size the gate's
    "0 of 0" trap relies on."""
    match = TOTALS.search(proc.stdout)
    if not match:
        raise AssertionError(f"no totals line in find_prose.py output: "
                             f"{proc.stdout[:200]!r} (stderr={proc.stderr!r})")
    return int(match.group(1)), int(match.group(2))


# A Rust doc comment long enough to count as oversize at the default 40-word
# threshold, and with a "is not" / "does not" phrase for negative-space.
RUST_SOURCE = """\
//! Inner module doc comment that anchors the module's role for readers
//! who arrive at this file without context. Long enough to be oversize.

/// Outer doc comment. This block is intentionally long so the finder
/// flags it as oversize: the words below pad past the 40-word threshold
/// without being meaningful, and the phrase "is not" at the end trips
/// the negative-space finding so the test pins both findings at once.

use std::collections::HashMap;

fn main() {
    let map = HashMap::new();
}
"""

# A SQL migration header long enough to count as oversize, with a "is not" /
# "does not" / "no longer" phrase for negative-space.
SQL_SOURCE = """\
-- This migration creates the users table. It is not just a schema patch:
-- it documents the negative-space decision the prior migration did not
-- record, so the next reader has the full picture and is not relying on
-- tribal knowledge that no longer lives anywhere in this file.

CREATE TABLE users (id INTEGER PRIMARY KEY);

-- noqa: long line, intentional, do not flag
INSERT INTO users (id) VALUES (1);
"""

# A drafted PR body with an agent session link: the exact wording the agent
# appends by default and that the finder must flag as ``provenance`` so the
# condense gate can ask the author to delete it.
BODY_WITH_SESSION_LINK = (
    "Why this exists.\n"
    "\n"
    "Closes #1\n"
    "\n"
    "https://claude.ai/code/session_01ABC\n"
)
BODY_WITHOUT_SESSION_LINK = "Why this exists.\n\nCloses #1\n"


def _load_module() -> ModuleType:
    """Import find_prose.py from disk so the test reads the same source the gate invokes, not a cached copy installed elsewhere."""
    spec = importlib.util.spec_from_file_location("find_prose_under_test",
                                                  FIND_PROSE)
    if spec is None or spec.loader is None:
        raise AssertionError(f"could not load find_prose.py from {FIND_PROSE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["find_prose_under_test"] = module
    spec.loader.exec_module(module)
    return module


def case_rust_extension_in_prose_set(failures: list[str]) -> None:
    """The .rs suffix must be in PROSE_SUFFIXES, otherwise the condense gate
    never even offers the file to the scanner, regardless of lexer."""
    module = _load_module()
    if ".rs" not in module.PROSE_SUFFIXES:
        fail(failures, "PROSE_SUFFIXES missing .rs",
             f"got {sorted(module.PROSE_SUFFIXES)}")
    if ".rs" not in module.SLASH_SUFFIXES:
        fail(failures, "SLASH_SUFFIXES missing .rs",
             f"got {sorted(module.SLASH_SUFFIXES)}")


def case_sql_extension_in_prose_set(failures: list[str]) -> None:
    """Same contract for .sql: if it is not in PROSE_SUFFIXES, the gate
    cannot gate a SQL-only diff."""
    module = _load_module()
    if ".sql" not in module.PROSE_SUFFIXES:
        fail(failures, "PROSE_SUFFIXES missing .sql",
             f"got {sorted(module.PROSE_SUFFIXES)}")


def case_rust_doc_comment_flagged(failures: list[str]) -> None:
    """The /// block must surface both ``oversize`` and ``negative-space``,
    and the ``use`` code line that follows it must not appear anywhere --
    it is a code line, not a comment."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "lib.rs").write_text(RUST_SOURCE)
        proc = run_find_prose("lib.rs", cwd=cwd)
        blocks = blocks_for(proc)
        if not blocks:
            fail(failures, "rust: scanner read no blocks from lib.rs",
                 f"stderr={proc.stderr!r}")
            return

        doc = [b for b in blocks if b["path"] == "lib.rs" and b["line"] >= 4]
        if not doc:
            fail(failures, "rust: no /// block found",
                 f"got lines {[b['line'] for b in blocks]}")
            return

        block = doc[0]
        if "oversize" not in block["findings"]:
            fail(failures, "rust: /// block missing oversize",
                 f"findings={block['findings']!r}")
        if "negative-space" not in block["findings"]:
            fail(failures, "rust: /// block missing negative-space",
                 f"findings={block['findings']!r}")

        # The lexer's `lstrip('/')` strips all leading slashes, so a /// or //!
        if block["text"].lstrip().startswith("/"):
            fail(failures, "rust: /// block still has '/' prefix in text",
                 f"text={block['text'][:80]!r}")

        # The `use std::collections::HashMap;` line is at line 9.
        for b in blocks:
            if "HashMap" in b["text"] or b["line"] == 9:
                fail(failures, "rust: code line read as prose",
                     f"block on line {b['line']}: {b['text'][:80]!r}")


def case_rust_inner_doc_counted(failures: list[str]) -> None:
    """`//!` inner doc comments must count as the same blocks as `///`, matching the issue's contract."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "inner.rs").write_text(RUST_SOURCE)
        proc = run_find_prose_text("inner.rs", cwd=cwd)
        flagged, total = totals_for(proc)
        if total < 2:
            fail(failures, "rust: scanner saw fewer than 2 prose blocks",
                 f"flagged={flagged} total={total}\nstdout={proc.stdout}")


def case_sql_migration_flagged(failures: list[str]) -> None:
    """The `--` migration header must surface both ``oversize`` and
    ``negative-space``, and the `-- noqa: ...` directive must not appear
    in any block's text."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "0001_users.sql").write_text(SQL_SOURCE)
        proc = run_find_prose("0001_users.sql", cwd=cwd)
        blocks = blocks_for(proc)
        header = [b for b in blocks if b["path"].endswith(".sql")]
        if not header:
            fail(failures, "sql: scanner read no blocks",
                 f"stderr={proc.stderr!r}")
            return

        block = header[0]
        if "oversize" not in block["findings"]:
            fail(failures, "sql: header missing oversize",
                 f"findings={block['findings']!r}")
        if "negative-space" not in block["findings"]:
            fail(failures, "sql: header missing negative-space",
                 f"findings={block['findings']!r}")

        # A regression that fails to strip the two dashes would leave them
        # attached to the first word: `-- This migration ...` rather than `This
        # migration ...`.
        if block["text"].lstrip().startswith("-"):
            fail(failures, "sql: header still has '--' prefix in text",
                 f"text={block['text'][:80]!r}")

        # The noqa directive is at line 9 of SQL_SOURCE (preceded by the header
        # block on 1-5, an empty line, and CREATE TABLE on 7).
        for b in blocks:
            if "noqa" in b["text"].lower():
                fail(failures, "sql: -- noqa directive read as prose",
                     f"block on line {b['line']}: {b['text'][:80]!r}")


def case_sql_block_does_not_swallow_trailing_code(failures: list[str]) -> None:
    """The CREATE TABLE on line 7 and INSERT on line 10 are code, not
    comments. Neither may appear inside a block's text."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "0002.sql").write_text(SQL_SOURCE)
        proc = run_find_prose("0002.sql", cwd=cwd)
        blocks = blocks_for(proc)
        for b in blocks:
            if "CREATE TABLE" in b["text"] or "INSERT INTO" in b["text"]:
                fail(failures, "sql: code line read as prose",
                     f"block on line {b['line']}: {b['text'][:80]!r}")


def case_provenance_session_link_is_flagged(failures: list[str]) -> None:
    """A PR body carrying an agent-session link yields a ``provenance`` finding."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "body.md").write_text(BODY_WITH_SESSION_LINK)
        proc = run_find_prose("body.md", cwd=cwd)
        blocks = blocks_for(proc)
        matching = [b for b in blocks
                    if "claude.ai/code/session_" in b["text"]]
        if not matching:
            fail(failures, "provenance: no block contained the session link",
                 f"blocks={[b['text'][:60] for b in blocks]}")
            return
        for b in matching:
            if "provenance" not in b["findings"]:
                fail(failures, "provenance: session-link block missing finding",
                     f"findings={b['findings']!r} text={b['text'][:80]!r}")


def case_provenance_absent_when_no_link(failures: list[str]) -> None:
    """Without the link, no block carries ``provenance``."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        (cwd / "body.md").write_text(BODY_WITHOUT_SESSION_LINK)
        proc = run_find_prose("body.md", cwd=cwd)
        blocks = blocks_for(proc)
        flagged = [b for b in blocks if "provenance" in b["findings"]]
        if flagged:
            fail(failures, "provenance: finding raised without a session link",
                 f"blocks={[b['text'][:60] for b in flagged]}")


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=False, env={
                              **os.environ, **IDENTITY})


def init_repo(tmp: Path) -> Path:
    """Init a repo with one empty commit so a follow-up commit produces a
    real diff against HEAD~1..HEAD. ``-b main`` keeps the default branch
    name stable across git versions."""
    repo = tmp / "repo"
    repo.mkdir()
    if git(repo, "init", "-q", "-b", "main").returncode != 0:
        git(repo, "init", "-q")
        git(repo, "checkout", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / "README").write_text("seed\n")
    git(repo, "add", "README")
    git(repo, "commit", "-q", "-m", "seed")
    return repo


def case_diff_reports_both_files(failures: list[str]) -> None:
    """The done-when condition: `find_prose.py --diff` over a branch that adds a Rust doc comment AND a SQL migration header reports blocks from BOTH."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = init_repo(Path(tmp))
        (repo / "src" / "lib.rs").parent.mkdir(parents=True, exist_ok=True)
        (repo / "src" / "lib.rs").write_text(RUST_SOURCE)
        (repo / "migrations" / "0001_users.sql").parent.mkdir(parents=True,
                                                              exist_ok=True)
        (repo / "migrations" / "0001_users.sql").write_text(SQL_SOURCE)
        git(repo, "add", "src/lib.rs", "migrations/0001_users.sql")
        git(repo, "commit", "-q", "-m", "add rust and sql prose")

        proc = run_find_prose("src/lib.rs", "migrations/0001_users.sql",
                              "--diff", "HEAD~1..HEAD", cwd=repo)
        if proc.returncode != 0:
            fail(failures, "diff: scanner returned non-zero",
                 f"rc={proc.returncode} stderr={proc.stderr!r}")
            return
        blocks = blocks_for(proc)
        paths = {b["path"] for b in blocks}
        if "src/lib.rs" not in paths:
            fail(failures, "diff: --diff did not report Rust doc comment",
                 f"paths={sorted(paths)}")
        if "migrations/0001_users.sql" not in paths:
            fail(failures, "diff: --diff did not report SQL migration header",
                 f"paths={sorted(paths)}")


def _oversize_rust_block(num_lines: int = 3, words_per_line: int = 65) -> str:
    """A `///` comment well over the 40-word threshold."""
    return "\n".join(
        "/// " + " ".join(f"w{i}" for i in range(words_per_line))
        for _ in range(num_lines)
    ) + "\n"


def _distinct_oversize_rust_block(num_lines: int = 3, words_per_line: int = 65) -> str:
    """Like `_oversize_rust_block` with distinct lines: with identical lines git
    anchors a deletion hunk on the last copy, not the deleted one."""
    return "\n".join(
        "/// " + " ".join(f"L{n}w{i}" for i in range(words_per_line))
        for n in range(num_lines)
    ) + "\n"


def _setup_comment_in_repo(repo: Path, comment: str, leading: str = "") -> None:
    """Commit `leading + comment` as `lib.rs`, the base the case's diff runs against."""
    (repo / "lib.rs").write_text(leading + comment)
    git(repo, "add", "lib.rs")
    git(repo, "commit", "-q", "-m", "set up doc comment")


def case_diff_added_fn_above_unchanged_comment_not_reported(failures: list[str]) -> None:
    """An unchanged comment stays unreported when a function lands just above it."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = init_repo(Path(tmp))
        comment = _oversize_rust_block()
        _setup_comment_in_repo(repo, comment,
                               leading="fn first() {}\nfn second() {}\n")
        text = (repo / "lib.rs").read_text().splitlines(keepends=True)
        text.insert(2, "fn third() {}\n")
        (repo / "lib.rs").write_text("".join(text))
        git(repo, "add", "lib.rs")
        git(repo, "commit", "-q", "-m", "add function above comment")

        proc = run_find_prose("lib.rs", "--diff", "HEAD~1..HEAD", cwd=repo)
        if proc.returncode != 0:
            fail(failures, "diff-fn-above: scanner returned non-zero",
                 f"rc={proc.returncode} stderr={proc.stderr!r}")
            return
        blocks = blocks_for(proc)
        if any(b["path"] == "lib.rs" for b in blocks):
            fail(failures, "diff-fn-above: comment reported though unchanged",
                 f"blocks={[(b['path'], b['line']) for b in blocks]}")


def case_diff_added_line_inside_comment_is_reported(failures: list[str]) -> None:
    """A line added inside a comment flags it."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = init_repo(Path(tmp))
        comment = _oversize_rust_block(num_lines=5)
        _setup_comment_in_repo(repo, comment,
                               leading="fn first() {}\nfn second() {}\n")
        text = (repo / "lib.rs").read_text().splitlines(keepends=True)
        # Insert a new comment line in the middle of the block (between
        # line 4 and line 5 of the file -- the second and third /// lines).
        text.insert(4, "/// " + " ".join(f"insert{i}" for i in range(60)) + "\n")
        (repo / "lib.rs").write_text("".join(text))
        git(repo, "add", "lib.rs")
        git(repo, "commit", "-q", "-m", "add line inside comment")

        proc = run_find_prose("lib.rs", "--diff", "HEAD~1..HEAD", cwd=repo)
        blocks = blocks_for(proc)
        if not any(b["path"] == "lib.rs" for b in blocks):
            fail(failures, "diff-inside-comment: comment not reported",
                 f"blocks={blocks!r}")


def case_diff_deleted_line_inside_comment_is_reported(failures: list[str]) -> None:
    """A line deleted inside a comment flags it."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = init_repo(Path(tmp))
        comment = _oversize_rust_block(num_lines=5)
        _setup_comment_in_repo(repo, comment,
                               leading="fn first() {}\nfn second() {}\n")
        text = (repo / "lib.rs").read_text().splitlines(keepends=True)
        # Delete the third /// line (1-indexed line 5 of the file, the
        # middle of the 5-line comment block).
        del text[4]
        (repo / "lib.rs").write_text("".join(text))
        git(repo, "add", "lib.rs")
        git(repo, "commit", "-q", "-m", "delete line from comment")

        proc = run_find_prose("lib.rs", "--diff", "HEAD~1..HEAD", cwd=repo)
        blocks = blocks_for(proc)
        if not any(b["path"] == "lib.rs" for b in blocks):
            fail(failures, "diff-deleted-inside-comment: comment not reported",
                 f"blocks={blocks!r}")


def case_diff_deleted_code_line_above_unchanged_comment_not_reported(failures: list[str]) -> None:
    """An unchanged comment stays unreported when a code line two above it is deleted."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = init_repo(Path(tmp))
        comment = _oversize_rust_block()
        _setup_comment_in_repo(
            repo, comment,
            leading="fn first() {}\nfn doomed() {}\nfn between() {}\n\n",
        )
        text = (repo / "lib.rs").read_text().splitlines(keepends=True)
        # Delete line 2 (``fn doomed()``); ``fn between()`` shifts to
        # line 2 and the comment to lines 4+.
        del text[1]
        (repo / "lib.rs").write_text("".join(text))
        git(repo, "add", "lib.rs")
        git(repo, "commit", "-q", "-m", "delete code line above comment")

        proc = run_find_prose("lib.rs", "--diff", "HEAD~1..HEAD", cwd=repo)
        blocks = blocks_for(proc)
        if any(b["path"] == "lib.rs" for b in blocks):
            fail(failures,
                 "diff-deleted-code-above: comment reported though unchanged",
                 f"blocks={[(b['path'], b['line']) for b in blocks]}")


def case_diff_deleted_first_line_of_comment_is_reported(failures: list[str]) -> None:
    """Deleting a comment's first line flags what remains of it."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = init_repo(Path(tmp))
        comment = _distinct_oversize_rust_block()
        _setup_comment_in_repo(repo, comment)
        text = (repo / "lib.rs").read_text().splitlines(keepends=True)
        # Delete line 1 (the first /// line).
        del text[0]
        (repo / "lib.rs").write_text("".join(text))
        git(repo, "add", "lib.rs")
        git(repo, "commit", "-q", "-m", "delete first line of comment")

        proc = run_find_prose("lib.rs", "--diff", "HEAD~1..HEAD", cwd=repo)
        blocks = blocks_for(proc)
        if not any(b["path"] == "lib.rs" for b in blocks):
            fail(failures,
                 "diff-deleted-first-of-comment: comment not reported",
                 f"blocks={blocks!r}")


def case_diff_deleted_file_does_not_touch_previous_file(failures: list[str]) -> None:
    """A deleted file's `+++ /dev/null` hunk must not mark lines in the file
    sorted before it as touched, and a file edited after the deletion must
    still be reported."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = init_repo(Path(tmp))
        # File names sort the deletion between the two edits in git's output.
        (repo / "a.rs").write_text(_oversize_rust_block() + "fn first() {}\nfn second() {}\n")
        (repo / "b.json").write_text("{\"a\":\"b\"}\n")
        (repo / "c.rs").write_text("fn first() {}\nfn second() {}\n")
        git(repo, "add", "a.rs", "b.json", "c.rs")
        git(repo, "commit", "-q", "-m", "seed a.rs, b.json, c.rs")

        text = (repo / "a.rs").read_text().splitlines(keepends=True)
        # Insert a function on a line far below the doc comment.
        text.append("fn third() {}\n")
        (repo / "a.rs").write_text("".join(text))
        (repo / "b.json").unlink()
        # Add a 60+ word doc comment to c.rs.
        comment = _oversize_rust_block(num_lines=1, words_per_line=60)
        (repo / "c.rs").write_text(comment + "fn first() {}\nfn second() {}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "edit a.rs, delete b.json, comment c.rs")

        proc = run_find_prose("a.rs", "b.json", "c.rs",
                              "--diff", "HEAD~1..HEAD", cwd=repo)
        if proc.returncode != 0:
            fail(failures, "diff-deleted-prev: scanner returned non-zero",
                 f"rc={proc.returncode} stderr={proc.stderr!r}")
            return
        blocks = blocks_for(proc)
        paths = {b["path"] for b in blocks}
        if "a.rs" in paths:
            fail(failures, "diff-deleted-prev: a.rs doc comment reported though unchanged",
                 f"paths={sorted(paths)}")
        if "c.rs" not in paths:
            fail(failures, "diff-deleted-prev: c.rs comment not reported after deletion",
                 f"paths={sorted(paths)}")


def case_diff_single_ref_skips_files_main_added_after_fork(failures: list[str]) -> None:
    """Files main added after the fork stay out of a single-ref diff."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = init_repo(Path(tmp))
        # Branch point: a.rs is two short functions, no comment.
        (repo / "a.rs").write_text("fn first() {}\nfn second() {}\n")
        git(repo, "add", "a.rs")
        git(repo, "commit", "-q", "-m", "branch point")
        git(repo, "checkout", "-q", "-b", "feature")

        # Branch commit: a long doc comment lands at the top of a.rs.
        prose = _oversize_rust_block()
        (repo / "a.rs").write_text(prose + "fn first() {}\nfn second() {}\n")
        git(repo, "add", "a.rs")
        git(repo, "commit", "-q", "-m", "feature adds a 60+ word comment")

        # Main moves on: a new file with its own 60+ word comment lands.
        git(repo, "checkout", "-q", "main")
        main_file = "//! " + " ".join(f"mainword{i}" for i in range(60)) + "\nfn feature() {}\n"
        (repo / "new_main_file.rs").write_text(main_file)
        git(repo, "add", "new_main_file.rs")
        git(repo, "commit", "-q", "-m", "main moves on")

        # Back on the branch: ``--diff main`` must skip main's new file but
        # still report the branch's own prose change.
        git(repo, "checkout", "-q", "feature")
        proc = run_find_prose(".", "--diff", "main", cwd=repo)
        if proc.returncode != 0:
            fail(failures, "diff-single-ref: scanner returned non-zero",
                 f"rc={proc.returncode} stderr={proc.stderr!r}")
            return
        blocks = blocks_for(proc)
        paths = {b["path"] for b in blocks}
        if "new_main_file.rs" in paths:
            fail(failures, "diff-single-ref: main-only file reported",
                 f"paths={sorted(paths)}")
        if "a.rs" not in paths:
            fail(failures, "diff-single-ref: branch's own prose not reported",
                 f"paths={sorted(paths)}")


def case_diff_explicit_two_dot_range_keeps_today_behavior(failures: list[str]) -> None:
    """An explicit ``A..B`` is used as given, so the branch's own change is reported."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = init_repo(Path(tmp))
        (repo / "a.rs").write_text("fn first() {}\nfn second() {}\n")
        git(repo, "add", "a.rs")
        git(repo, "commit", "-q", "-m", "branch point")
        git(repo, "checkout", "-q", "-b", "feature")

        prose = _oversize_rust_block()
        (repo / "a.rs").write_text(prose + "fn first() {}\nfn second() {}\n")
        git(repo, "add", "a.rs")
        git(repo, "commit", "-q", "-m", "feature adds a 60+ word comment")

        git(repo, "checkout", "-q", "main")
        main_file = "//! " + " ".join(f"mainword{i}" for i in range(60)) + "\nfn feature() {}\n"
        (repo / "new_main_file.rs").write_text(main_file)
        git(repo, "add", "new_main_file.rs")
        git(repo, "commit", "-q", "-m", "main moves on")

        git(repo, "checkout", "-q", "feature")
        proc = run_find_prose(".", "--diff", "main..HEAD", cwd=repo)
        if proc.returncode != 0:
            fail(failures, "diff-two-dot: scanner returned non-zero",
                 f"rc={proc.returncode} stderr={proc.stderr!r}")
            return
        blocks = blocks_for(proc)
        paths = {b["path"] for b in blocks}
        # The branch's own prose change is in ``A..B`` and must surface.
        if "a.rs" not in paths:
            fail(failures, "diff-two-dot: branch's own prose not reported",
                 f"paths={sorted(paths)}")


def case_diff_uncommitted_edit_on_branch_is_reported(failures: list[str]) -> None:
    """An uncommitted edit on a branch lands in the diff because the merge
    base is the only rev, so ``git diff`` compares it against the working tree."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = init_repo(Path(tmp))
        # Branch point: lib.rs is two short functions, no comment.
        (repo / "lib.rs").write_text("fn first() {}\nfn second() {}\n")
        git(repo, "add", "lib.rs")
        git(repo, "commit", "-q", "-m", "branch point")
        git(repo, "checkout", "-q", "-b", "feature")

        # Left uncommitted: only a diff against the working tree sees it.
        prose = _oversize_rust_block()
        (repo / "lib.rs").write_text(prose + "fn first() {}\nfn second() {}\n")

        proc = run_find_prose(".", "--diff", "main", cwd=repo)
        if proc.returncode != 0:
            fail(failures, "diff-uncommitted-on-branch: scanner returned non-zero",
                 f"rc={proc.returncode} stderr={proc.stderr!r}")
            return
        blocks = blocks_for(proc)
        paths = {b["path"] for b in blocks}
        if "lib.rs" not in paths:
            fail(failures, "diff-uncommitted-on-branch: uncommitted edit not reported",
                 f"paths={sorted(paths)}")


def case_hash_commented_files_scanned(failures: list[str]) -> None:
    """A file no suffix identifies is scanned when its first line is a shebang
    or a `#` comment, and skipped when it is generated or uncommented."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        comment = "# " + " ".join(["word"] * 50) + "\n"
        files = {
            "remote-task": "#!/usr/bin/env bash\n" + comment + "true\n",
            "hosts.env.example": comment + "export A=b\n",
            "Cargo.lock": "# This file is automatically @generated by Cargo.\n" + comment,
            "notes.txt": "plain\n" + comment,
        }
        for name, text in files.items():
            (cwd / name).write_text(text)
        paths = {b["path"] for b in blocks_for(run_find_prose(".", cwd=cwd))}
        for name in ("remote-task", "hosts.env.example"):
            if name not in paths:
                fail(failures, f"hash-commented: {name} not scanned",
                     f"paths={sorted(paths)}")
        for name in ("Cargo.lock", "notes.txt"):
            if name in paths:
                fail(failures, f"hash-commented: {name} scanned",
                     f"paths={sorted(paths)}")


def main() -> int:
    failures: list[str] = []
    case_rust_extension_in_prose_set(failures)
    case_sql_extension_in_prose_set(failures)
    case_rust_doc_comment_flagged(failures)
    case_rust_inner_doc_counted(failures)
    case_sql_migration_flagged(failures)
    case_sql_block_does_not_swallow_trailing_code(failures)
    case_provenance_session_link_is_flagged(failures)
    case_provenance_absent_when_no_link(failures)
    case_diff_reports_both_files(failures)
    case_diff_added_fn_above_unchanged_comment_not_reported(failures)
    case_diff_added_line_inside_comment_is_reported(failures)
    case_diff_deleted_line_inside_comment_is_reported(failures)
    case_diff_deleted_code_line_above_unchanged_comment_not_reported(failures)
    case_diff_deleted_first_line_of_comment_is_reported(failures)
    case_diff_deleted_file_does_not_touch_previous_file(failures)
    case_diff_single_ref_skips_files_main_added_after_fork(failures)
    case_diff_explicit_two_dot_range_keeps_today_behavior(failures)
    case_diff_uncommitted_edit_on_branch_is_reported(failures)
    case_hash_commented_files_scanned(failures)

    if failures:
        print("FAIL", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("PASS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
