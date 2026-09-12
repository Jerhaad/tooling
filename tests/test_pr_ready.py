#!/usr/bin/env python3
"""Pin pr-ready's verdict on the three cases the issue separates.

A check that ran and failed has steps with conclusions, and a log. A
check that never started has zero steps and no log. The two are
different answers about the branch: the first refuses the promotion
because CI produced evidence against it; the second promotes because
CI produced no evidence at all, and an annotation tells the reviewer
what they are getting. Lumping them together is the defect this test
guards against.

The third case -- genuine failure mixed with never-started checks --
is the one most likely to be wrong, because the obvious code path
that "promotes under no evidence" will also promote under a real
failure that simply happens to share the run. The fixture isolates
each shape, and the test asserts the verdict on each in isolation,
so a regression on one case cannot be hidden by another.

No network. A stub `gh` answers every subcommand pr-ready invokes,
reading from fixtures on disk so the canned JSON is the kind a real
`gh` actually prints and not something this test only ever feeds
itself.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PR_READY = REPO_ROOT / "bin" / "pr-ready"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def make_repo(tmpdir: Path) -> tuple[Path, str, str]:
    """Initialise a git repo with one commit on `issue-28`. Returns
    (worktree, branch, oid) so the test can build a pr_view fixture
    whose HEAD OID matches what `git rev-parse` will return."""
    tmpdir.mkdir(parents=True, exist_ok=True)
    repo = tmpdir / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "issue-28", str(repo)],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email",
                    "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name",
                    "Test"], check=True)
    (repo / "README").write_text("test\n")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"],
                   check=True)
    oid = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    return repo, "issue-28", oid


def write_stub_gh(bin_dir: Path, data_dir: Path) -> None:
    """Write a stub `gh` that answers the subcommands pr-ready invokes,
    reading each response from $data_dir so the test sets the canned
    JSON on disk rather than in a here-string the script only sees once.

    Every invocation is logged to data_dir/gh.log so a test can read
    back what pr-ready actually asked for. pr-ready treats `gh pr
    checks` exit non-zero as "no checks reported"; the stub exits 0.

    `pr view` is called with --jq to flatten the JSON into a TSV the
    caller reads into separate variables; the stub pipes the canned
    JSON through the same jq expression so the test sees what
    pr-ready actually sees.
    """
    body = (
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$STUB_DATA/gh.log\"\n"
        "case \"$1\" in\n"
        "  pr)\n"
        "    case \"$2\" in\n"
        "      view)\n"
        "        # args: pr view <PR> --json FIELDS --jq EXPR\n"
        "        json=() jq_expr=\"\"\n"
        "        prev=\"\"\n"
        "        for arg in \"$@\"; do\n"
        "          case \"$prev\" in\n"
        "            --json) json=(\"$arg\") ;;\n"
        "            --jq)   jq_expr=\"$arg\" ;;\n"
        "          esac\n"
        "          prev=\"$arg\"\n"
        "        done\n"
        "        if [ -n \"$jq_expr\" ]; then\n"
        "          cat \"$STUB_DATA/pr_view.json\" | jq -r \"$jq_expr\"\n"
        "        else\n"
        "          cat \"$STUB_DATA/pr_view.json\"\n"
        "        fi\n"
        "        ;;\n"
        "      checks)\n"
        "        cat \"$STUB_DATA/pr_checks.json\"\n"
        "        ;;\n"
        "      comment)\n"
        "        # args: pr comment <PR> --body-file <PATH>\n"
        "        prev=\"\" body=\"\"\n"
        "        for arg in \"$@\"; do\n"
        "          [ \"$prev\" = \"--body-file\" ] && body=\"$arg\"\n"
        "          prev=\"$arg\"\n"
        "        done\n"
        "        if [ -n \"$body\" ]; then\n"
        "          cp \"$body\" \"$STUB_DATA/comment_body.txt\"\n"
        "        fi\n"
        "        ;;\n"
        "      ready)\n"
        "        echo \"$3\" > \"$STUB_DATA/ready_called\"\n"
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "  run)\n"
        "    # args: run view <RUN_ID> --json jobs\n"
        "    cat \"$STUB_DATA/run_$3.json\" 2>/dev/null || echo '{\"jobs\":[]}'\n"
        "    ;;\n"
        "esac\n"
    )
    gh = bin_dir / "gh"
    gh.write_text(body)
    gh.chmod(0o755)


def write_stub_gate_verify(bin_dir: Path) -> None:
    """Stub gate-verify so a test that runs it does not have to build a
    real one. Two faces:
      --list: prints one gate per line.
      default: exits 0 (pass).
    """
    body = (
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$STUB_DATA/gate_verify.log\"\n"
        "case \"$2\" in\n"
        "  --list)\n"
        "    echo '  bench       remote-task $REPO'\n"
        "    echo '  artifacts   redaction-check $REPO'\n"
        "    ;;\n"
        "  *)\n"
        "    echo '==> all gates passed: bench,artifacts'\n"
        "    ;;\n"
        "esac\n"
    )
    p = bin_dir / "gate-verify"
    p.write_text(body)
    p.chmod(0o755)


def install_fixtures(data_dir: Path, scenario: str, oid: str,
                     branch: str) -> None:
    """Copy the canned JSON for a scenario into data_dir, substituting
    the test repo's actual HEAD OID and branch into pr_view so the
    local/pushed OID comparison passes."""
    pr_view = {
        "state": "OPEN",
        "isDraft": True,
        "headRefName": branch,
        "headRefOid": oid,
        "mergeable": "MERGEABLE",
    }
    (data_dir / "pr_view.json").write_text(json.dumps(pr_view))

    if scenario == "all_pass":
        shutil.copy(FIXTURES / "gh_pr_checks_pass.json",
                    data_dir / "pr_checks.json")
    elif scenario == "real_failure":
        shutil.copy(FIXTURES / "gh_pr_checks_failed.json",
                    data_dir / "pr_checks.json")
        shutil.copy(FIXTURES / "gh_run_view_failed.json",
                    data_dir / "run_2001.json")
    elif scenario == "never_started":
        shutil.copy(FIXTURES / "gh_pr_checks_never_started.json",
                    data_dir / "pr_checks.json")
        shutil.copy(FIXTURES / "gh_run_view_never_started.json",
                    data_dir / "run_3001.json")
    elif scenario == "mixed":
        shutil.copy(FIXTURES / "gh_pr_checks_mixed.json",
                    data_dir / "pr_checks.json")
        shutil.copy(FIXTURES / "gh_run_view_mixed_5001.json",
                    data_dir / "run_5001.json")
        shutil.copy(FIXTURES / "gh_run_view_mixed_5002.json",
                    data_dir / "run_5002.json")
    else:
        raise AssertionError(f"unknown scenario: {scenario}")


def run_pr_ready(worktree: Path, stub_path: Path, data_dir: Path,
                 *extra: str) -> subprocess.CompletedProcess:
    """Invoke pr-ready with a PATH where the stub `gh` and `gate-verify`
    come first, and STUB_DATA pointing at the scenario's canned JSON.
    --no-condense keeps the prose finder out of the picture; the
    installation environment does not have it, but the flag makes that
    explicit and removes any future surprise."""
    env = os.environ.copy()
    env["PATH"] = f"{stub_path}:{env['PATH']}"
    env["STUB_DATA"] = str(data_dir)
    env.pop("PR_READY_BASE", None)
    env["HOME"] = str(worktree.parent)
    return subprocess.run(
        ["bash", str(PR_READY), "42", str(worktree),
         "--no-condense", *extra],
        capture_output=True, text=True, env=env,
    )


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        worktree, branch, oid = make_repo(tmp / "src")

        # One stub dir shared across scenarios; the per-scenario data dir
        # is what the stub reads from, so swapping fixtures swaps the
        # canned `gh` behaviour without rewriting the script.
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        write_stub_gh(bin_dir, bin_dir)
        write_stub_gate_verify(bin_dir)

        try:
            # Scenario 1: every CI check has passed. Promote, no
            # annotation needed (no never-started checks to call out).
            data = tmp / "data_all_pass"
            data.mkdir()
            install_fixtures(data, "all_pass", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data, "--check")
            if r.returncode != 0:
                failures.append(
                    f"all_pass --check should exit 0: rc={r.returncode} "
                    f"stderr={r.stderr.strip()!r} stdout={r.stdout.strip()!r}"
                )
            else:
                # --check must not have posted an annotation, and must
                # not have called `gh pr ready`. Both are writes.
                if (data / "comment_body.txt").exists():
                    failures.append(
                        "all_pass --check posted a comment: "
                        f"{(data / 'comment_body.txt').read_text()!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "all_pass --check called `gh pr ready`"
                    )

            # Same scenario without --check: must call `gh pr ready`
            # (the PR is a draft) and post no annotation.
            data = tmp / "data_all_pass_real"
            data.mkdir()
            install_fixtures(data, "all_pass", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode != 0:
                failures.append(
                    f"all_pass (promote) should exit 0: rc={r.returncode} "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                if not (data / "ready_called").exists():
                    failures.append(
                        "all_pass (promote) did not call `gh pr ready`"
                    )
                if (data / "comment_body.txt").exists():
                    failures.append(
                        "all_pass (promote) posted a comment without "
                        "any never-started check to annotate"
                    )

            # Scenario 2: one check failed for real (steps present).
            # Refuse. The reason must name the failing check, and
            # crucially must not claim the account is at fault.
            data = tmp / "data_real_failure"
            data.mkdir()
            install_fixtures(data, "real_failure", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode == 0:
                failures.append(
                    "real_failure should refuse (exit 1): "
                    f"stderr={r.stderr.strip()!r}"
                )
            else:
                msg = r.stderr
                if "bench" not in msg:
                    failures.append(
                        f"real_failure reason did not name 'bench': "
                        f"{msg!r}"
                    )
                # The fix the issue names: nothing pr-ready prints may
                # say the cause is on the account rather than the
                # branch. Refusing to promote here is about the
                # branch -- the check ran and produced a failure --
                # so the reason must read as such.
                for word in ("quota", "account", "runner not",
                             "never started", "no evidence"):
                    if word in msg.lower():
                        failures.append(
                            f"real_failure reason frames a real "
                            f"failure as '{word}': {msg!r}"
                        )
                if (data / "ready_called").exists():
                    failures.append(
                        "real_failure promoted after refusing"
                    )

            # Scenario 3: one check reports bucket=fail but the
            # matching job has zero steps. Promote, annotate, do not
            # refuse.
            data = tmp / "data_never_started"
            data.mkdir()
            install_fixtures(data, "never_started", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode != 0:
                failures.append(
                    f"never_started should promote (exit 0): "
                    f"rc={r.returncode} stderr={r.stderr.strip()!r}"
                )
            else:
                if not (data / "ready_called").exists():
                    failures.append(
                        "never_started did not call `gh pr ready`"
                    )
                body_file = data / "comment_body.txt"
                if not body_file.exists():
                    failures.append(
                        "never_started did not post an annotation"
                    )
                else:
                    body = body_file.read_text()
                    if "bench" not in body:
                        failures.append(
                            f"annotation did not name the never-started "
                            f"check: {body!r}"
                        )
                    if oid[:7] not in body:
                        failures.append(
                            f"annotation did not name the local sha "
                            f"({oid[:7]}): {body!r}"
                        )

            # Same scenario with --no-verify: the annotation must say
            # no local gates ran, because --no-verify skipped them.
            # A check name happens to overlap with a gate name in the
            # canonical fixture, so we read the gate-list line out of
            # the annotation rather than scanning for the names --
            # what the test really pins is that the annotation's
            # gate section says none ran, not that no gate name string
            # appears anywhere in the file.
            data = tmp / "data_never_started_no_verify"
            data.mkdir()
            install_fixtures(data, "never_started", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data, "--no-verify")
            if r.returncode != 0:
                failures.append(
                    f"never_started --no-verify should promote: "
                    f"rc={r.returncode} stderr={r.stderr.strip()!r}"
                )
            elif (data / "comment_body.txt").exists():
                body = (data / "comment_body.txt").read_text()
                gates_line = [
                    ln for ln in body.splitlines()
                    if ln.startswith("Local gates run")
                ]
                if not gates_line:
                    failures.append(
                        f"--no-verify annotation missing 'Local gates "
                        f"run' line: {body!r}"
                    )
                elif "none" not in gates_line[0].lower():
                    failures.append(
                        f"--no-verify annotation lists local gates that "
                        f"did not run: {gates_line[0]!r}"
                    )

            # Mixed: one check failed for real (steps present), one
            # never started (steps empty). The issue calls this the case
            # to get right: the obvious "promote under no evidence"
            # branch would also promote a real failure that happens to
            # sit alongside a never-started one. The verdict must
            # refuse, and the reason must name the real failure -- not
            # the never-started one, and not both.
            data = tmp / "data_mixed"
            data.mkdir()
            install_fixtures(data, "mixed", oid, branch)
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode == 0:
                failures.append(
                    "mixed (real failure + never-started) should refuse "
                    f"(exit 1): stderr={r.stderr.strip()!r}"
                )
            else:
                msg = r.stderr
                if "real-failure" not in msg:
                    failures.append(
                        f"mixed reason did not name the real failure "
                        f"('real-failure'): {msg!r}"
                    )
                if "quota-blocked" in msg:
                    failures.append(
                        "mixed reason named the never-started check "
                        "as a reason to refuse: "
                        f"{msg!r}"
                    )
                if (data / "ready_called").exists():
                    failures.append(
                        "mixed promoted after refusing"
                    )

            # Scenario 5: the run fetches, but carries no job with the
            # id the check's link names. That is a run we could not
            # inspect, not a run with no steps, and reading it as
            # never-started promotes a check that ran and failed.
            data = tmp / "data_job_absent"
            data.mkdir()
            install_fixtures(data, "real_failure", oid, branch)
            run = json.loads((data / "run_2001.json").read_text())
            for job in run["jobs"]:
                job["databaseId"] = 999999
            (data / "run_2001.json").write_text(json.dumps(run))
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode == 0:
                failures.append(
                    "job absent from run: promoted a failing check "
                    f"because its job id matched nothing: "
                    f"stderr={r.stderr.strip()!r}"
                )
            if (data / "ready_called").exists():
                failures.append(
                    "job absent from run: called `gh pr ready`"
                )

            # Scenario 6: the PR is already ready. The script documents
            # re-running as a no-op, and posting another annotation on
            # every run is not one.
            data = tmp / "data_already_ready"
            data.mkdir()
            install_fixtures(data, "never_started", oid, branch)
            pv = json.loads((data / "pr_view.json").read_text())
            pv["isDraft"] = False
            (data / "pr_view.json").write_text(json.dumps(pv))
            r = run_pr_ready(worktree, bin_dir, data)
            if r.returncode != 0:
                failures.append(
                    f"already-ready should exit 0: rc={r.returncode} "
                    f"stderr={r.stderr.strip()!r}"
                )
            if (data / "comment_body.txt").exists():
                failures.append(
                    "already-ready re-run posted another annotation: "
                    f"{(data / 'comment_body.txt').read_text()!r}"
                )

        except AssertionError as e:
            failures.append(f"unexpected assertion error: {e}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
