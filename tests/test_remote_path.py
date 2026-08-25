#!/usr/bin/env python3
"""Pin that the remote PATH is built on the far side, not this one.

The bug this covers was invisible to every fixture: the leaked PATH still
contained /usr/bin, so a command living there resolved and the run looked
healthy. Only a command under the remote's $HOME failed. Asserting "the command
was found" therefore proves nothing -- what has to hold is that no directory
belonging to the dispatching host reaches the remote at all.

No host is contacted. The default is a literal string, and whether its dollars
survive to the far side is decided here.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib" / "common.sh"


def remote_path(env: dict) -> str:
    """The value common.sh leaves in REMOTE_PATH, with a scratch env file so a
    real one on this machine cannot mask the default."""
    out = subprocess.run(
        ["bash", "-c", f'. "{LIB}"; printf "%s" "$REMOTE_PATH"'],
        capture_output=True, text=True, env=env, check=True)
    return out.stdout


def main() -> int:
    empty = Path(os.environ.get("TMPDIR", "/tmp")) / "test_remote_path.env"
    empty.write_text("")
    env = {**os.environ, "TOOLS_ENV": str(empty), "HOME": "/home/dispatcher",
           "PATH": "/home/dispatcher/.cargo/bin:/usr/bin:/bin"}
    env.pop("REMOTE_PATH", None)
    value = remote_path(env)

    failures = []

    # The dollars must still be dollars: expanded here, they name directories
    # that exist only on this machine.
    for var in ("$HOME", "$PATH"):
        if var not in value:
            failures.append(f"{var} expanded before it was sent: {value!r}")

    # The dispatching host's own directories must appear nowhere. Compared as
    # whole entries: "/bin" is a substring of "$HOME/.local/bin", and a
    # substring test fails on the correct value.
    entries = value.split(":")
    for leaked in ("/home/dispatcher", "/usr/bin", "/bin"):
        if leaked in entries:
            failures.append(f"a dispatching-host directory reached the remote: {leaked!r}")

    # Quotes inside the value become literal characters in the remote script.
    if "'" in value or '"' in value:
        failures.append(f"quotes are inside the value, not around it: {value!r}")

    # An explicit setting must win, so a host that keeps its shims elsewhere
    # can say so.
    override = remote_path({**env, "REMOTE_PATH": "/opt/tools/bin"})
    if override != "/opt/tools/bin":
        failures.append(f"an explicit REMOTE_PATH was overwritten: {override!r}")

    for f in failures:
        print(f"FAIL {f}")
    if failures:
        return 1
    print(f"PASS remote PATH stays unexpanded and host-free: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
