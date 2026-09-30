#!/usr/bin/env python3
"""Pin finding-check against the cases the issue separates."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "bin" / "finding-check"


def _load_tool_module():
    """Load `bin/finding-check` as an importable module."""
    tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
    tmp.close()
    shutil.copy(str(TOOL), tmp.name)
    spec = importlib.util.spec_from_file_location("_fc_under_test",
                                                  tmp.name)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {TOOL} as a module")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True)


def init_repo(tmpdir: Path, base_content: str, branch_content: str,
              name: str = "a.txt") -> Path:
    """Build a one-file repo with one commit on main, then branch with a change."""
    repo = tmpdir / "repo"
    repo.mkdir()
    bare = tmpdir / "bare.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare", "-q", str(bare)],
                   check=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t")
    git(repo, "config", "user.name", "t")
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(base_content)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "-b", "branch")
    (repo / name).write_text(branch_content)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change")
    return repo


def run(worktree: Path, findings) -> subprocess.CompletedProcess:
    """Invoke the tool with a JSON list of findings. `findings` may
    be a Python list (serialised here) or a Path to a JSON file."""
    if isinstance(findings, list):
        path = worktree / "_findings.json"
        path.write_text(json.dumps(findings))
        findings = path
    return subprocess.run(["python3", str(TOOL), str(worktree),
                           str(findings)],
                          capture_output=True, text=True)


def fail(failures, msg, detail=None):
    failures.append(f"{msg}: {detail}" if detail is not None else msg)


def find_by(annotated, **want):
    """Pull one annotated finding out by an arbitrary field match.
    A test asserting on one case uses this to ignore order; a test
    asserting on a count of `malformed` does not need it."""
    for f in annotated:
        if all(f.get(k) == v for k, v in want.items()):
            return f
    return None


def case_drifted_line_corrected(failures):
    """The cited line is wrong by tens of lines; the snippet still
    matches at one place; the corrected line is the match nearest
    the cited line, not the first match anywhere in the file."""
    base = "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\neta\n"
    # Branch adds two lines near the bottom so the diff's new-side
    # range covers the snippet's corrected line and `in_diff` is
    # true. A reviewer citing `line=1` against a snippet that lives
    # at the new line 8 has drifted.
    branch = "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta\neta\nTHETA\nIOTA\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [{
            "severity": "low", "path": "a.txt", "line": 1,
            "snippet": "THETA", "claim": "x", "settled_by": "y",
            "recommendation": "z",
        }]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "drifted-line exit", r.returncode)
            return
        annotated = json.loads(r.stdout)
        f = find_by(annotated, path="a.txt")
        if not f:
            fail(failures, "drifted-line: no annotated finding")
            return
        if f["status"] != "located":
            fail(failures, "drifted-line: status", f["status"])
        if f["line"] != 8:
            fail(failures, "drifted-line: corrected line", f["line"])
        # The branch added lines 8-9; the corrected line is 8, so
        # it falls inside the diff's new-side range and `in_diff`
        # is true. This is the case the issue names -- a defect
        # introduced by the branch.
        if f["in_diff"] is not True:
            fail(failures, "drifted-line: in_diff", f["in_diff"])


def case_two_matches_picks_nearest(failures):
    """The same snippet appears in two places."""
    base = "alpha\nmatch\nbeta\ngamma\ndelta\nepsilon\nmatch\nzeta\n"
    branch = "alpha\nmatch\nbeta\ngamma\ndelta\nepsilon\nmatch\nzeta\nETA\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        # Cite line 7 (the second `match`); the snippet `match`
        # is on lines 2 and 7, so the nearest match is line 7
        # (distance 0) and the tool must return 7, not 2.
        findings = [{
            "severity": "low", "path": "a.txt", "line": 7,
            "snippet": "match", "claim": "x", "settled_by": "y",
            "recommendation": "z",
        }]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "two-matches exit", r.returncode)
            return
        annotated = json.loads(r.stdout)
        f = find_by(annotated, path="a.txt")
        if f["line"] != 7:
            fail(failures, "two-matches: line", f["line"])
        if f["status"] != "located":
            fail(failures, "two-matches: status", f["status"])


def case_redaction_artifact(failures):
    """A `***` the file does not hold is a redaction artifact."""
    base = "alpha\nbeta\ngamma\ndelta\nepsilon\n"
    # Branch inserts `SECRET = "abc"` between `gamma` and `delta`, pushing the
    # original line 4 (`delta`) down to line 5.
    branch = "alpha\nbeta\ngamma\nSECRET = \"abc\"\ndelta\nepsilon\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [{
            "severity": "high", "path": "a.txt", "line": 4,
            "snippet": "gamma\nSECRET = \"***\"\ndelta",
            "claim": "leak",
            "settled_by": "grep", "recommendation": "rotate",
        }]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "redaction-artifact exit", r.returncode)
            return
        annotated = json.loads(r.stdout)
        f = find_by(annotated, path="a.txt")
        if f["status"] != "redaction-artifact":
            fail(failures, "redaction-artifact: status", f["status"])
        if f["line"] != 3:
            fail(failures, "redaction-artifact: corrected line", f["line"])
        if f["in_diff"] is not False:
            fail(failures, "redaction-artifact: in_diff", f["in_diff"])


def case_redaction_negative(failures):
    """A `***` the file does hold is real source."""
    base = "alpha\nbeta\ngamma\ndelta\n"
    branch = "alpha\nbeta\ngamma\nSECRET = \"***\"\ndelta\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [{
            "severity": "high", "path": "a.txt", "line": 4,
            "snippet": "gamma\nSECRET = \"***\"\ndelta", "claim": "leak",
            "settled_by": "grep", "recommendation": "rotate",
        }]
        r = run(repo, findings)
        annotated = json.loads(r.stdout)
        f = find_by(annotated, path="a.txt")
        if f["status"] != "located":
            fail(failures, "redaction-negative: status", f["status"])
        if "***" not in r.stdout:
            fail(failures, "redaction-negative: tool dropped ***",
                 r.stdout)


def case_not_found(failures):
    """The path exists but the snippet does not."""
    base = "alpha\nbeta\ngamma\n"
    branch = "alpha\nbeta\ngamma\ndelta\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [{
            "severity": "low", "path": "a.txt", "line": 1,
            "snippet": "this is not in the file",
            "claim": "x", "settled_by": "y", "recommendation": "z",
        }]
        r = run(repo, findings)
        annotated = json.loads(r.stdout)
        f = find_by(annotated, path="a.txt")
        if f["status"] != "not-found":
            fail(failures, "not-found: status", f["status"])
        if f["line"] != 1:
            fail(failures, "not-found: line stays cited",
                 f["line"])


def case_path_missing(failures):
    """The path does not exist at all. Must not crash on the read;
    must report `path-missing` so the reviewer knows the cited
    file is gone, not that the snippet is wrong."""
    base = "alpha\nbeta\n"
    branch = "alpha\nbeta\ngamma\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [{
            "severity": "low", "path": "does_not_exist.txt", "line": 1,
            "snippet": "anything", "claim": "x",
            "settled_by": "y", "recommendation": "z",
        }]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "path-missing exit", r.returncode)
            return
        annotated = json.loads(r.stdout)
        f = find_by(annotated, path="does_not_exist.txt")
        if f["status"] != "path-missing":
            fail(failures, "path-missing: status", f["status"])


def case_malformed_missing_field(failures):
    """A finding without a required field is malformed. The other
    findings in the same batch must still be checked -- the tool
    drops nothing for that."""
    base = "alpha\nbeta\n"
    branch = "alpha\nbeta\ngamma\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [
            {"severity": "low", "path": "a.txt"},  # missing fields
            {"severity": "low", "path": "a.txt", "line": 3,
             "snippet": "gamma", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
        ]
        r = run(repo, findings)
        annotated = json.loads(r.stdout)
        malformed = [f for f in annotated if f["status"] == "malformed"]
        if len(malformed) != 1:
            fail(failures, "malformed: count", len(malformed))
        ok = find_by(annotated, snippet="gamma")
        if not ok or ok["status"] != "located":
            fail(failures, "malformed: second finding dropped")
        if "missing required field" not in (malformed[0].get("reason", "")
                                            if malformed else ""):
            fail(failures, "malformed: reason",
                 malformed[0].get("reason") if malformed else None)


def case_malformed_bad_severity(failures):
    """A severity outside the set is malformed too, for the same
    reason: a verdict on a finding the tool cannot place in the
    reviewer's vocabulary is the wrong shape of result."""
    base = "alpha\nbeta\n"
    branch = "alpha\nbeta\ngamma\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [{
            "severity": "critical", "path": "a.txt", "line": 3,
            "snippet": "gamma", "claim": "x", "settled_by": "y",
            "recommendation": "z",
        }]
        r = run(repo, findings)
        annotated = json.loads(r.stdout)
        f = find_by(annotated, snippet="gamma")
        if not f or f["status"] != "malformed":
            fail(failures, "bad-severity: status", f.get("status"))
        if "severity" not in (f.get("reason", "") if f else ""):
            fail(failures, "bad-severity: reason",
                 f.get("reason") if f else None)


def case_malformed_bad_severity_type(failures):
    """A severity of the wrong type is malformed, and the valid finding
    beside it still locates."""
    base = "alpha\nbeta\n"
    branch = "alpha\nbeta\ngamma\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [
            {"severity": ["high"], "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "x",
             "settled_by": "y", "recommendation": "z"},
            {"severity": {"k": "v"}, "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "y",
             "settled_by": "y", "recommendation": "z"},
            {"severity": 7, "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "z",
             "settled_by": "y", "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "ok",
             "settled_by": "y", "recommendation": "z"},  # well-formed
        ]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "bad-severity-type: non-zero exit",
                 f"rc={r.returncode} stderr={r.stderr!r}")
            return
        try:
            annotated = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            fail(failures, "bad-severity-type: stdout not JSON",
                 f"{exc}: {r.stdout[:200]}")
            return
        if len(annotated) != len(findings):
            fail(failures, "bad-severity-type: batch length",
                 f"got {len(annotated)} want {len(findings)}")
            return
        # The first three are unhashable / wrong-typed severities.
        # Each must be malformed with a reason naming `severity`
        # and the type it received -- the wording follows the
        # existing type-validation pattern for `line` and the
        # other fields.
        bad_types = ("list", "dict", "int")
        for i, want_type in enumerate(bad_types):
            f = annotated[i]
            if f.get("status") != "malformed":
                fail(failures,
                     f"bad-severity-type[{i}] severity->{want_type}: "
                     f"status", f.get("status"))
                continue
            reason = f.get("reason", "")
            if "severity" not in reason:
                fail(failures,
                     f"bad-severity-type[{i}] severity->{want_type}: "
                     f"reason missing field name", reason)
            if want_type not in reason:
                fail(failures,
                     f"bad-severity-type[{i}] severity->{want_type}: "
                     f"reason missing type name", reason)
        # The well-formed finding must still locate -- this is the
        # core of the regression: a malformed severity used to
        # abort the batch and the valid finding vanished with it.
        ok = annotated[-1]
        if ok.get("status") != "located":
            fail(failures, "bad-severity-type: well-formed finding "
                           "dropped", ok.get("status"))


def case_out_of_diff(failures):
    """A located finding outside the changed ranges is not in the diff."""
    base = "alpha\nbeta\ngamma\ndelta\n"
    # Branch only edits line 1; lines 3-4 are untouched.
    branch = "ALPHA\nbeta\ngamma\ndelta\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [{
            "severity": "low", "path": "a.txt", "line": 4,
            "snippet": "delta", "claim": "x", "settled_by": "y",
            "recommendation": "z",
        }]
        r = run(repo, findings)
        annotated = json.loads(r.stdout)
        f = find_by(annotated, snippet="delta")
        if not f or f["status"] != "located":
            fail(failures, "out-of-diff: status", f.get("status"))
        if f["in_diff"] is not False:
            fail(failures, "out-of-diff: in_diff", f.get("in_diff"))


def case_whitespace_normalisation(failures):
    """A snippet with trailing whitespace and irregular internal
    spaces still matches when the underlying source has been
    re-indented; without normalisation a hand-written snippet
    would fail against a freshly formatted file."""
    base = "alpha\nbeta\ngamma\n"
    # The file has '   beta   ' indented oddly; the snippet has
    # just 'beta' with normal spacing. Normalisation collapses
    # internal whitespace so both reduce to 'beta' and match.
    branch = "alpha\n   beta   \ngamma\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [{
            "severity": "low", "path": "a.txt", "line": 1,
            "snippet": "  beta  ", "claim": "x", "settled_by": "y",
            "recommendation": "z",
        }]
        r = run(repo, findings)
        annotated = json.loads(r.stdout)
        f = find_by(annotated, path="a.txt")
        if not f or f["status"] != "located":
            fail(failures, "whitespace: status", f.get("status"))


def case_misuse_exits_two(failures):
    """Exit code 2 is reserved for misuse, not for findings the
    tool could not place. Two misuse paths: a missing argument
    and a JSON file that does not parse."""
    r = subprocess.run(["python3", str(TOOL)],
                       capture_output=True, text=True)
    if r.returncode != 2:
        fail(failures, "misuse no-args: rc", r.returncode)
    if "usage" not in r.stderr.lower():
        fail(failures, "misuse no-args: stderr", r.stderr)
    with tempfile.TemporaryDirectory() as t:
        bad = Path(t) / "findings.json"
        bad.write_text("not json")
        repo = Path(t) / "repo"
        repo.mkdir()
        r = subprocess.run(["python3", str(TOOL), str(repo), str(bad)],
                           capture_output=True, text=True)
        if r.returncode != 2:
            fail(failures, "misuse bad-json: rc", r.returncode)


def case_tally_on_stderr(failures):
    """The tally on stderr counts each status once. The tool
    must print one line, not one line per finding, and the count
    must equal what the annotated JSON says."""
    base = "alpha\nbeta\ngamma\n"
    branch = "alpha\nbeta\ngamma\ndelta\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": "missing.txt", "line": 1,
             "snippet": "x", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low"},  # malformed
        ]
        r = run(repo, findings)
        lines = [ln for ln in r.stderr.strip().splitlines() if ln]
        if len(lines) != 1:
            fail(failures, "tally: not one line", r.stderr)
        else:
            line = lines[0]
            for expect in ("located=1", "path-missing=1", "malformed=1"):
                if expect not in line:
                    fail(failures, f"tally: missing {expect}", line)


def case_malformed_field_types(failures):
    """A wrong-typed field is malformed, not a crash."""
    base = "alpha\nbeta\ngamma\n"
    branch = "alpha\nbeta\ngamma\ndelta\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        # One finding per type error, plus a well-formed finding
        # at the end so the test also pins "the batch still
        # runs after a type error". The bad types come from the
        # reviewer's mistakes, not from a missing field.
        findings = [
            {"severity": "low", "path": "a.txt", "line": "42",
             "snippet": "alpha", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 42.5,
             "snippet": "alpha", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": None,
             "snippet": "alpha", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": True,
             "snippet": "alpha", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": 42, "line": 1,
             "snippet": "alpha", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": 7, "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": 1, "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "x", "settled_by": 2,
             "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "x", "settled_by": "y",
             "recommendation": [1]},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "x", "settled_by": "y",
             "recommendation": "z"},  # well-formed: must still locate
        ]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "type-validation: non-zero exit",
                 f"rc={r.returncode} stderr={r.stderr!r}")
            return
        try:
            annotated = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            fail(failures, "type-validation: stdout not JSON",
                 f"{exc}: {r.stdout[:200]}")
            return
        if len(annotated) != len(findings):
            fail(failures, "type-validation: batch length",
                 f"got {len(annotated)} want {len(findings)}")
            return
        # The first nine findings are type errors. Each must be
        # malformed with a reason naming the field and the type
        # it received; the exact wording is the caller's to read,
        # but the verdict must not be a different status.
        bad_types = (
            ("line", "str"),
            ("line", "float"),
            ("line", "NoneType"),
            ("line", "bool"),
            ("path", "int"),
            ("snippet", "int"),
            ("claim", "int"),
            ("settled_by", "int"),
            ("recommendation", "list"),
        )
        for i, (field, want_type) in enumerate(bad_types):
            f = annotated[i]
            if f.get("status") != "malformed":
                fail(failures,
                     f"type-validation[{i}] {field}->{want_type}: "
                     f"status", f.get("status"))
                continue
            reason = f.get("reason", "")
            if field not in reason:
                fail(failures,
                     f"type-validation[{i}] {field}->{want_type}: "
                     f"reason missing field name", reason)
            if want_type not in reason:
                fail(failures,
                     f"type-validation[{i}] {field}->{want_type}: "
                     f"reason missing type name", reason)
        # The well-formed finding must still locate.
        ok = annotated[-1]
        if ok.get("status") != "located":
            fail(failures,
                 "type-validation: well-formed finding dropped",
                 ok.get("status"))


def case_malformed_non_object_entry(failures):
    """A non-object entry is malformed in place, keeping its value."""
    base = "alpha\nbeta\ngamma\n"
    branch = "alpha\nbeta\ngamma\ndelta\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [
            "just a string",
            42,
            ["nested"],
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
        ]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "non-object: non-zero exit",
                 f"rc={r.returncode} stderr={r.stderr!r}")
            return
        try:
            annotated = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            fail(failures, "non-object: stdout not JSON",
                 f"{exc}: {r.stdout[:200]}")
            return
        if len(annotated) != len(findings):
            fail(failures, "non-object: batch length",
                 f"got {len(annotated)} want {len(findings)}")
            return
        # The first three entries are not objects; each must be
        # malformed with the original value preserved verbatim.
        for i, original in enumerate(findings[:3]):
            f = annotated[i]
            if f.get("status") != "malformed":
                fail(failures, f"non-object[{i}]: status",
                     f.get("status"))
                continue
            if "value" not in f:
                fail(failures, f"non-object[{i}]: no `value` key", f)
                continue
            if f["value"] != original:
                fail(failures, f"non-object[{i}]: value not preserved",
                     f"got {f['value']!r} want {original!r}")
        # The well-formed finding must still locate.
        ok = annotated[-1]
        if ok.get("status") != "located":
            fail(failures, "non-object: well-formed finding dropped",
                 ok.get("status"))


def case_path_outside_worktree(failures):
    """A path outside the worktree is path-missing and never read."""
    base = "alpha\nbeta\ngamma\n"
    branch = "alpha\nbeta\ngamma\ndelta\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        # A symlink inside the worktree that points beyond it.
        link = repo / "link.txt"
        try:
            link.symlink_to("/etc/hostname")
        except (OSError, NotImplementedError):
            # Windows or restricted FS; the rest of the case
            # still pins the other two escapes.
            pass
        findings = [
            {"severity": "low", "path": "/etc/hostname", "line": 1,
             "snippet": "x", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": "../escape.txt", "line": 1,
             "snippet": "x", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": "link.txt", "line": 1,
             "snippet": "x", "claim": "x", "settled_by": "y",
             "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "x", "settled_by": "y",
             "recommendation": "z"},  # well-formed: still locates
        ]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "path-outside: non-zero exit",
                 f"rc={r.returncode} stderr={r.stderr!r}")
            return
        try:
            annotated = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            fail(failures, "path-outside: stdout not JSON",
                 f"{exc}: {r.stdout[:200]}")
            return
        # First three are escapes; each must be path-missing with
        # a reason that names the path.
        for i, path in enumerate(("/etc/hostname", "../escape.txt",
                                  "link.txt")):
            f = annotated[i]
            if f.get("status") != "path-missing":
                fail(failures, f"path-outside[{i}]: status",
                     f.get("status"))
                continue
            reason = f.get("reason", "")
            if "outside" not in reason:
                fail(failures, f"path-outside[{i}]: reason missing "
                                f"'outside'", reason)
            if path not in reason:
                fail(failures, f"path-outside[{i}]: reason missing "
                                f"path", reason)
        # The well-formed finding still locates.
        ok = annotated[-1]
        if ok.get("status") != "located":
            fail(failures, "path-outside: well-formed finding dropped",
                 ok.get("status"))


def case_wildcard_only_snippet(failures):
    """A pure-wildcard line matches only a line holding `***`."""
    # File with no `***` at all.
    base_clean = "alpha\nbeta\ngamma\n"
    branch_clean = "alpha\nbeta\ngamma\ndelta\n"
    # File with a `***` line that the pure-wildcard snippet
    # can land on.
    base_star = "alpha\n***\ngamma\n"
    branch_star = "alpha\n***\ngamma\ndelta\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base_clean, branch_clean)
        # Snippet made entirely of pure-wildcard lines: against a
        # file with no `***`, the verdict is `not-found`. A
        # version that treated `***` as a universal wildcard
        # would locate at the first line and report
        # `redaction-artifact` -- the regression this case pins.
        findings = [
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "***", "claim": "x",
             "settled_by": "y", "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "***\n***", "claim": "x",
             "settled_by": "y", "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "***\nfoo", "claim": "x",
             "settled_by": "y", "recommendation": "z"},
        ]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "wildcard-only: non-zero exit",
                 f"rc={r.returncode} stderr={r.stderr!r}")
            return
        try:
            annotated = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            fail(failures, "wildcard-only: stdout not JSON",
                 f"{exc}: {r.stdout[:200]}")
            return
        for i in range(3):
            f = annotated[i]
            if f.get("status") != "not-found":
                fail(failures,
                     f"wildcard-only[{i}]: status against clean file",
                     f.get("status"))
    # Same snippet against a file that does contain `***`: the single-line
    # pure-wildcard lands on the line that contains `***`.
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base_star, branch_star)
        findings = [
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "***", "claim": "x",
             "settled_by": "y", "recommendation": "z"},
        ]
        r = run(repo, findings)
        try:
            annotated = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            fail(failures, "wildcard-only: stdout not JSON",
                 f"{exc}: {r.stdout[:200]}")
            return
        f = annotated[0]
        if f.get("status") != "located":
            fail(failures, "wildcard-only: status against starred file",
                 f.get("status"))
        elif f.get("line") != 2:
            fail(failures, "wildcard-only: line against starred file",
                 f.get("line"))


def case_snippet_line_must_equal_file_line(failures):
    """A substring of a line is not a match."""
    base = "alpha\nreturn x + y\nbeta\n"
    branch = "alpha\nreturn x + y\nbeta\ngamma\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        findings = [
            # Substring-only: the file has `return x + y` but
            # never `return x` as its own line. The current
            # segment-in-order logic locates at line 2; the fix
            # must refuse.
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "return x", "claim": "x",
             "settled_by": "y", "recommendation": "z"},
            # Sanity check that the rule is not over-broad: a
            # snippet that exactly matches a file line still
            # locates, with the corrected line reported.
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "x",
             "settled_by": "y", "recommendation": "z"},
        ]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "exact-line: non-zero exit",
                 f"rc={r.returncode} stderr={r.stderr!r}")
            return
        try:
            annotated = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            fail(failures, "exact-line: stdout not JSON",
                 f"{exc}: {r.stdout[:200]}")
            return
        sub = annotated[0]
        if sub.get("status") != "not-found":
            fail(failures, "exact-line[0]: substring must be not-found",
                 sub.get("status"))
        if sub.get("line") != 1:
            fail(failures, "exact-line[0]: line stays cited on not-found",
                 sub.get("line"))
        eq = annotated[1]
        if eq.get("status") != "located":
            fail(failures, "exact-line[1]: exact match must locate",
                 eq.get("status"))
        if eq.get("line") != 1:
            fail(failures, "exact-line[1]: corrected line", eq.get("line"))


def case_deleted_file_does_not_leak_path(failures):
    """A deleted file's hunk is not charged to the next file."""
    diff_text = (
        "diff --git a/kept.txt b/kept.txt\n"
        "index 1111111..2222222 100644\n"
        "--- a/kept.txt\n"
        "+++ b/kept.txt\n"
        "@@ -1,1 +1,2 @@\n"
        " keep\n"
        "+added\n"
        "diff --git a/deleted.txt b/deleted.txt\n"
        "deleted file mode 100644\n"
        "index 3333333..0000000\n"
        "--- a/deleted.txt\n"
        "+++ /dev/null\n"
        "@@ -1,3 +0,0 @@\n"
        "-old1\n"
        "-old2\n"
        "-old3\n"
    )
    mod = _load_tool_module()
    ranges = mod.parse_hunks(diff_text)
    # The kept file owns the addition: the hunk's new side
    # runs from line 1 (one unchanged `keep`) to line 2
    # (the new `added` line).
    if ranges.get("kept.txt") != [(1, 2)]:
        fail(failures, "deleted-leak: kept.txt ranges wrong",
             f"got {ranges.get('kept.txt')!r}")
    # The deleted file's name must not appear at all in the
    # ranges. Without the fix, the deletion's `@@ -1,3 +0,0`
    # leaks into `kept.txt` as a phantom `(0, 0)` entry.
    if "deleted.txt" in ranges:
        fail(failures, "deleted-leak: deleted.txt leaked into ranges",
             f"got {ranges!r}")
    # And no extra (0, 0) entry sits on the kept file.
    if any(start == 0 and end == 0 for start, end in
           ranges.get("kept.txt", [])):
        fail(failures, "deleted-leak: phantom (0, 0) on kept.txt",
             f"got {ranges!r}")


def case_path_with_null_byte(failures):
    """A path with a NUL byte is path-missing, and the valid finding
    beside it still locates."""
    base = "alpha\nbeta\n"
    branch = "alpha\nbeta\ngamma\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        # `json.dumps` escapes the NUL as `\u0000`; the tool sees
        # the real character when it loads the file.
        findings = [
            {"severity": "low", "path": "bin/\u0000x", "line": 1,
             "snippet": "x", "claim": "x",
             "settled_by": "y", "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "ok",
             "settled_by": "y", "recommendation": "z"},
        ]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "path-null: non-zero exit",
                 f"rc={r.returncode} stderr={r.stderr!r}")
            return
        try:
            annotated = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            fail(failures, "path-null: stdout not JSON",
                 f"{exc}: {r.stdout[:200]}")
            return
        if len(annotated) != len(findings):
            fail(failures, "path-null: batch length",
                 f"got {len(annotated)} want {len(findings)}")
            return
        bad = annotated[0]
        if bad.get("status") != "path-missing":
            fail(failures, "path-null: status", bad.get("status"))
        ok = annotated[1]
        if ok.get("status") != "located":
            fail(failures, "path-null: well-formed finding dropped",
                 ok.get("status"))


def case_unreadable_file(failures):
    """A path to a file the process cannot read is path-missing, and the
    valid finding beside it still locates."""
    base = "alpha\nbeta\n"
    branch = "alpha\nbeta\ngamma\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch)
        # Skipped where root can read a mode-000 file anyway.
        secret = repo / "secret.txt"
        secret.write_text("the password is hunter2\n")
        try:
            secret.chmod(0o000)
        except (OSError, NotImplementedError):
            return  # chmod unsupported on this FS; nothing to pin
        # Confirm the file is genuinely unreadable for this
        # process before relying on the test: a buggy chmod
        # would silently make the test vacuous.
        try:
            secret.read_text()
            secret.chmod(0o644)  # restore so the tempdir cleans
            return  # the host is permissive; skip the case
        except PermissionError:
            pass
        findings = [
            {"severity": "high", "path": "secret.txt", "line": 1,
             "snippet": "x", "claim": "leak",
             "settled_by": "y", "recommendation": "z"},
            {"severity": "low", "path": "a.txt", "line": 1,
             "snippet": "alpha", "claim": "ok",
             "settled_by": "y", "recommendation": "z"},
        ]
        r = run(repo, findings)
        # Restore before anything else: a torn-down tempdir must
        # not leave a mode-000 file behind for the next test.
        secret.chmod(0o644)
        if r.returncode != 0:
            fail(failures, "unreadable: non-zero exit",
                 f"rc={r.returncode} stderr={r.stderr!r}")
            return
        try:
            annotated = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            fail(failures, "unreadable: stdout not JSON",
                 f"{exc}: {r.stdout[:200]}")
            return
        if len(annotated) != len(findings):
            fail(failures, "unreadable: batch length",
                 f"got {len(annotated)} want {len(findings)}")
            return
        bad = annotated[0]
        if bad.get("status") != "path-missing":
            fail(failures, "unreadable: status", bad.get("status"))
        ok = annotated[1]
        if ok.get("status") != "located":
            fail(failures, "unreadable: well-formed finding dropped",
                 ok.get("status"))


def case_in_diff_normalises_path(failures):
    """`./crates/a.rs` is in the diff that changed `crates/a.rs`."""
    base = "alpha\nbeta\ngamma\n"
    branch = "alpha\nbeta\ngamma\nDELTA\n"
    with tempfile.TemporaryDirectory() as t:
        repo = init_repo(Path(t), base, branch,
                         name="crates/a.txt")
        findings = [
            # Leading `./`; matches the canonical `crates/a.txt`
            # after worktree-relative resolution.
            {"severity": "low", "path": "./crates/a.txt", "line": 1,
             "snippet": "DELTA", "claim": "x",
             "settled_by": "y", "recommendation": "z"},
            # Double slash and `./` segment; the worktree
            # resolves to `crates/a.txt` and the diff lookup
            # finds the change.
            {"severity": "low", "path": "./crates/./a.txt", "line": 1,
             "snippet": "DELTA", "claim": "x",
             "settled_by": "y", "recommendation": "z"},
        ]
        r = run(repo, findings)
        if r.returncode != 0:
            fail(failures, "in-diff-normalise: non-zero exit",
                 f"rc={r.returncode} stderr={r.stderr!r}")
            return
        try:
            annotated = json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            fail(failures, "in-diff-normalise: stdout not JSON",
                 f"{exc}: {r.stdout[:200]}")
            return
        for i, finding_in in enumerate(findings):
            f = annotated[i]
            if f.get("status") != "located":
                fail(failures,
                     f"in-diff-normalise[{i}]: status",
                     f.get("status"))
                continue
            if f.get("in_diff") is not True:
                fail(failures,
                     f"in-diff-normalise[{i}]: in_diff must be true "
                     f"for path {finding_in['path']!r}",
                     f.get("in_diff"))
            # The finding's `path` is the reviewer-written
            # spelling; only the diff lookup was canonicalised.
            if f.get("path") != finding_in["path"]:
                fail(failures,
                     f"in-diff-normalise[{i}]: finding path "
                     f"rewritten",
                     f"got {f.get('path')!r} want "
                     f"{finding_in['path']!r}")


def main() -> int:
    if not TOOL.is_file():
        print(f"FAIL tool not built: {TOOL}")
        return 1
    failures: list = []
    for case in CASES:
        try:
            case(failures)
        except Exception as exc:  # noqa: BLE001
            fail(failures, f"{case.__name__} raised", f"{type(exc).__name__}: {exc}")
    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS finding-check: each status; drifted line; double-match; "
          "normalisation; tally; misuse")
    return 0


CASES = (case_drifted_line_corrected, case_two_matches_picks_nearest,
         case_redaction_artifact, case_redaction_negative,
         case_not_found, case_path_missing,
         case_malformed_missing_field, case_malformed_bad_severity,
         case_malformed_bad_severity_type,
         case_malformed_field_types, case_malformed_non_object_entry,
         case_path_outside_worktree, case_wildcard_only_snippet,
         case_out_of_diff, case_whitespace_normalisation,
         case_misuse_exits_two, case_tally_on_stderr,
         case_snippet_line_must_equal_file_line,
         case_deleted_file_does_not_leak_path,
         case_path_with_null_byte, case_unreadable_file,
         case_in_diff_normalises_path)


if __name__ == "__main__":
    sys.exit(main())
