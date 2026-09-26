#!/usr/bin/env python3
"""Find source files to invalidate when synced SQL migrations change.

Scan Rust sources conservatively: comments/string literals can cause extra
rebuilds. This is not a Rust parser. Declared paths add custom embedding sites;
they cannot hide a sqlx::migrate! site discovered elsewhere in the tree.
"""
import os
import re
import sys
from pathlib import Path

from manifest import bash_array

EXCLUDED = {".git", "target", "node_modules"}  # remote-task's rsync exclusions
EMBEDS = re.compile(r"\bsqlx\s*::\s*migrate\s*!")


def checked_path(root: Path, value: str, *, directory: bool = False) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"migrator path must be relative to the synced tree: {value!r}")
    if EXCLUDED.intersection(path.parts):
        raise ValueError(f"migrator path is excluded from sync: {value!r}")
    candidate = root
    for part in path.parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError(f"migrator path must not traverse a symlink: {value!r}")
    if not (candidate.is_dir() if directory else candidate.is_file()):
        raise ValueError(f"migrator path does not exist: {value!r}")
    return path


def discover(root: Path, declared: list[str]) -> list[str]:
    sources = {checked_path(root, value).as_posix() for value in declared}

    def fail(error):
        raise error

    for parent, dirs, files in os.walk(root, onerror=fail, followlinks=False):
        dirs[:] = [d for d in dirs if d not in EXCLUDED
                   and not (Path(parent) / d).is_symlink()]
        for name in files:
            path = Path(parent) / name
            if path.suffix != ".rs" or path.is_symlink():
                continue
            if EMBEDS.search(path.read_text(encoding="utf-8", errors="surrogateescape")):
                sources.add(path.relative_to(root).as_posix())
    return sorted(sources)


def main():
    root, directory, *declared = sys.argv[1:]
    root = Path(root)
    try:
        checked_path(root, directory, directory=True)
        sources = discover(root, declared)
    except (OSError, ValueError) as error:
        sys.exit(f"migration guard: {error}")
    print(bash_array("TASK_MIGRATOR_SOURCES", sources))


if __name__ == "__main__":
    main()
