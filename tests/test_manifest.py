#!/usr/bin/env python3
"""Pin the manifest reader's refusal behaviour with a test.

A missing required field must surface as a named exit -- table, entry,
and field -- not as an uncaught KeyError that the caller sees as an
unbound-variable. Optional fields must still reach bash as empty
strings, so an entry that omits them is read the same as one that
fills them with nothing.

Run directly: `python3 tests/test_manifest.py`. The harness has no
test runner dependency; stdlib tomllib + subprocess is enough.
"""
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PY = REPO_ROOT / "lib" / "manifest.py"
EXAMPLE = REPO_ROOT / "gates.toml.example"


def setup(tmpdir: Path) -> None:
    """Copy gates.toml.example to tmpdir/gates.toml with comments stripped,
    so it round-trips through tomllib cleanly."""
    src = EXAMPLE.read_text()
    cleaned = "\n".join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#") and ln.strip() != ""
    )
    (tmpdir / "gates.toml").write_text(cleaned)


def run(tmpdir: Path, query: str, want: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(MANIFEST_PY), str(tmpdir), query, want],
        capture_output=True, text=True,
    )


def strip_first_line(tmpdir: Path, table: str, entry_name: str, field: str) -> None:
    """Within the first matching [[table]] block whose `name` is `entry_name`,
    delete the line that assigns `field`."""
    p = tmpdir / "gates.toml"
    text = p.read_text()
    lines = text.splitlines()
    # find the [[table]] whose immediate `name = entry_name` matches
    for i, ln in enumerate(lines):
        if ln != f"[[{table}]]":
            continue
        if i + 1 < len(lines) and f'name = "{entry_name}"' in lines[i + 1]:
            for j in range(i + 1, min(i + 12, len(lines))):
                if lines[j].startswith(f"{field} ="):
                    del lines[j]
                    p.write_text("\n".join(lines) + "\n")
                    # sanity: still valid TOML
                    tomllib.loads(p.read_text())
                    return
            raise AssertionError(
                f"could not locate {field!r} line for [[{table}]] {entry_name!r}"
            )
    raise AssertionError(f"could not locate [[{table}]] {entry_name!r}")


def assert_pass(tmpdir: Path, query: str, want: str) -> str:
    r = run(tmpdir, query, want)
    assert r.returncode == 0, (
        f"manifest rejected valid entry: exit={r.returncode} "
        f"stderr={r.stderr.strip()} stdout={r.stdout.strip()}"
    )
    return r.stdout


def assert_refuses(tmpdir: Path, query: str, want: str,
                   table: str, field: str) -> None:
    r = run(tmpdir, query, want)
    msg = (r.stderr or r.stdout).strip()
    assert r.returncode != 0, (
        f"manifest accepted entry with missing {field!r}: {msg!r}"
    )
    assert field in msg, f"message did not name field {field!r}: {msg!r}"
    assert table in msg, f"message did not name table {table!r}: {msg!r}"


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as t:
        tmpdir = Path(t)

        # Baseline: complete manifest passes both queries.
        setup(tmpdir)
        try:
            assert_pass(tmpdir, "--task", "bench")
            assert_pass(tmpdir, "--remote", "chart")
        except AssertionError as e:
            failures.append(f"baseline: {e}")

        # Each required field of a [[task]] bench: stripped -> named refusal.
        for field in ("name", "role", "command"):
            setup(tmpdir)
            try:
                if field == "name":
                    # Stripping `name` makes the entry unaddressable; the
                    # existing "no [[task]] named X" refusal is also a named
                    # message. Either path is correct, but we accept only the
                    # one that names the missing `name` field.
                    strip_first_line(tmpdir, "task", "bench", "name")
                    r = run(tmpdir, "--task", "bench")
                    msg = (r.stderr or r.stdout).strip()
                    if r.returncode == 0:
                        failures.append(
                            f"task missing 'name' did not refuse: {msg!r}"
                        )
                    elif "name" not in msg or "task" not in msg:
                        failures.append(
                            f"task missing 'name' refused without naming "
                            f"field/table: {msg!r}"
                        )
                else:
                    strip_first_line(tmpdir, "task", "bench", field)
                    assert_refuses(tmpdir, "--task", "bench", "task", field)
            except AssertionError as e:
                failures.append(f"[[task]] missing {field!r}: {e}")

        # Each required field of a [[remote]] chart: stripped -> named refusal.
        for field in ("name", "role", "command", "send"):
            setup(tmpdir)
            try:
                if field == "name":
                    strip_first_line(tmpdir, "remote", "chart", "name")
                    r = run(tmpdir, "--remote", "chart")
                    msg = (r.stderr or r.stdout).strip()
                    if r.returncode == 0:
                        failures.append(
                            f"remote missing 'name' did not refuse: {msg!r}"
                        )
                    elif "name" not in msg or "remote" not in msg:
                        failures.append(
                            f"remote missing 'name' refused without naming "
                            f"field/table: {msg!r}"
                        )
                else:
                    strip_first_line(tmpdir, "remote", "chart", field)
                    assert_refuses(tmpdir, "--remote", "chart", "remote", field)
            except AssertionError as e:
                failures.append(f"[[remote]] missing {field!r}: {e}")

        # Optional fields still reach bash as empty strings.
        setup(tmpdir)
        text = (tmpdir / "gates.toml").read_text()
        lines = [ln for ln in text.splitlines() if ln.strip() != ""]
        # Drop `full-command`, `fetch`, and every line of the [task.database]
        # sub-table that follows the first [[task]] bench entry.
        keep = []
        i = next(k for k, ln in enumerate(lines) if ln == "[[task]]")
        # Lines from i through the start of the next [[task]] or [[remote]].
        skip_db = False
        for k, ln in enumerate(lines):
            if k > i and ln.startswith("[[") and ln != "[[task]]":
                skip_db = False
            if ln.startswith("full-command =") or ln.startswith("fetch ="):
                continue
            if ln.startswith("[task.database]"):
                skip_db = True
                continue
            if skip_db:
                continue
            keep.append(ln)
        (tmpdir / "gates.toml").write_text("\n".join(keep) + "\n")
        try:
            out = assert_pass(tmpdir, "--task", "bench")
        except AssertionError as e:
            failures.append(f"optional fields missing: {e}")
            out = ""
        if out:
            for var in ("TASK_FULL_COMMAND", "TASK_DB_CONTAINER",
                        "TASK_DB_USER", "TASK_DB_PORT", "TASK_DB_PREFIX"):
                if f"{var}=''" not in out:
                    failures.append(f"{var} not empty in output: {out!r}")
            if "declare -a TASK_FETCH=()" not in out:
                failures.append(f"TASK_FETCH not empty array: {out!r}")

        # The [[gates]] `run` refusal still fires (existing behaviour, not
        # touched here, but worth pinning).
        setup(tmpdir)
        text = (tmpdir / "gates.toml").read_text()
        lines = text.splitlines()
        i = next(k for k, ln in enumerate(lines) if ln == "[[gates]]")
        j = next(k for k in range(i + 1, len(lines)) if lines[k].startswith("run ="))
        del lines[j]
        (tmpdir / "gates.toml").write_text("\n".join(lines) + "\n")
        r = run(tmpdir, "gates", "")
        if r.returncode == 0:
            failures.append(f"[[gates]] missing 'run' did not refuse: {r.stdout!r}")

        # Bash integration: under `set -euo pipefail`, an unset required field
        # must reach the caller as the manifest's named error, not as an
        # unbound-variable shell error. This is the exact failure mode the
        # issue names. The caller pattern is the one remote-task and
        # remote-run now use: capture to a variable so `set -e` sees the
        # reader's non-zero exit, then eval.
        setup(tmpdir)
        strip_first_line(tmpdir, "task", "bench", "role")
        script = f"""#!/usr/bin/env bash
set -euo pipefail
output=$(python3 '{MANIFEST_PY}' '{tmpdir}' --task bench)
eval "$output"
echo "TASK_ROLE=$TASK_ROLE"
"""
        bash_script = tmpdir / "test.sh"
        bash_script.write_text(script)
        bash_script.chmod(0o755)
        r = subprocess.run(["bash", str(bash_script)],
                           capture_output=True, text=True)
        msg = (r.stderr or r.stdout).strip()
        if r.returncode == 0:
            failures.append("bash integration: refusal did not propagate")
        if "unbound variable" in msg:
            failures.append(
                f"bash saw 'unbound variable' instead of named refusal: {msg!r}"
            )
        if "[[task]]" not in msg or "role" not in msg:
            failures.append(
                f"bash integration message missing table/field: {msg!r}"
            )

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())