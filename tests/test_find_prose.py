#!/usr/bin/env python3
"""Pin that find_prose.py reads Rust and SQL the way it reads the languages it already handled."""
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


def main() -> int:
    failures: list[str] = []
    case_rust_extension_in_prose_set(failures)
    case_sql_extension_in_prose_set(failures)
    case_rust_doc_comment_flagged(failures)
    case_rust_inner_doc_counted(failures)
    case_sql_migration_flagged(failures)
    case_sql_block_does_not_swallow_trailing_code(failures)
    case_diff_reports_both_files(failures)

    if failures:
        print("FAIL", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("PASS", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())