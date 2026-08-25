#!/usr/bin/env python3
"""Read a project's gate manifest and print one field, shell-quoted.

Every value is printed quoted for `eval`, and lists reach bash as `declare -a`
rather than as a delimiter nothing can escape: a gate command is a shell
fragment the project wrote.
"""
import shlex
import sys
import tomllib
from pathlib import Path

MANIFEST = "gates.toml"


def find(start: Path) -> Path:
    """Search from `start` upward: a tool is handed a worktree by path rather
    than run from inside it."""
    for d in [start, *start.parents]:
        p = d / MANIFEST
        if p.is_file():
            return p
    sys.exit(f"no {MANIFEST} found in {start} or any parent")


def bash_array(name: str, values) -> str:
    items = " ".join(shlex.quote(str(v)) for v in values)
    return f"declare -a {name}=({items})"


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: manifest.py <repo> <query> [default]")
    repo, query = Path(sys.argv[1]).resolve(), sys.argv[2]
    default = sys.argv[3] if len(sys.argv) > 3 else None

    with find(repo).open("rb") as fh:
        doc = tomllib.load(fh)

    if query == "--remote":
        want = default
        for entry in doc.get("remote", []):
            if entry.get("name") == want:
                print(f"REMOTE_ROLE={shlex.quote(entry['role'])}")
                print(f"REMOTE_COMMAND={shlex.quote(entry['command'])}")
                print(bash_array("REMOTE_SEND", entry.get("send", [])))
                return
        names = ", ".join(e.get("name", "?") for e in doc.get("remote", [])) or "none"
        sys.exit(f"{MANIFEST}: no [[remote]] named {want!r} (have: {names})")

    if query == "--task":
        # Every field but name/role/command is optional and reaches bash empty.
        want = default
        for entry in doc.get("task", []):
            if entry.get("name") != want:
                continue
            db = entry.get("database", {})
            for var, val in [
                ("TASK_ROLE", entry["role"]),
                ("TASK_COMMAND", entry["command"]),
                ("TASK_FULL_COMMAND", entry.get("full-command", "")),
                ("TASK_DB_CONTAINER", db.get("container", "")),
                ("TASK_DB_USER", db.get("user", "")),
                ("TASK_DB_PORT", db.get("port", "")),
                ("TASK_DB_PREFIX", db.get("name-prefix", "")),
            ]:
                print(f"{var}={shlex.quote(str(val))}")
            print(bash_array("TASK_FETCH", entry.get("fetch", [])))
            return
        names = ", ".join(e.get("name", "?") for e in doc.get("task", [])) or "none"
        sys.exit(f"{MANIFEST}: no [[task]] named {want!r} (have: {names})")

    if query == "gates":
        # Three parallel arrays because bash has no array of records. Order is
        # the file's, so the manifest decides what runs first.
        gates = doc.get("gates", [])
        names = [g.get("name", f"gate{i}") for i, g in enumerate(gates)]
        whens = [g.get("when", "") for g in gates]
        runs = [g.get("run", "") for g in gates]
        if any(not r for r in runs):
            sys.exit(f"{MANIFEST}: every [[gates]] entry needs a `run`")
        print(bash_array("GATE_NAMES", names))
        print(bash_array("GATE_WHEN", whens))
        print(bash_array("GATE_RUN", runs))
        return

    node = doc
    for part in query.split("."):
        if not isinstance(node, dict) or part not in node:
            if default is not None:
                print(default)
                return
            sys.exit(f"{MANIFEST}: no `{query}`")
        node = node[part]

    if isinstance(node, list):
        print(" ".join(shlex.quote(str(v)) for v in node))
    elif isinstance(node, bool):
        print("1" if node else "")
    else:
        print(node)


if __name__ == "__main__":
    main()
