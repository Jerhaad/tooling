#!/usr/bin/env python3
"""Pin findings-ledger's sources, its Ready windows and its dedupe.

Each row of the window table is its own scenario, so one row passing cannot
hide another failing. A stub `gh` answers from fixtures on disk and refuses any
flag the real gh would, so the tool cannot pass here and fail against GitHub.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FINDINGS = REPO_ROOT / "bin" / "findings-ledger"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


# ----- stub gh -------------------------------------------------------------

# The flags the real gh accepts for each call this tool makes:
#   api:      endpoint, --paginate[=BOOL], --jq[=EXPR]
#   pr list:  --repo, --state, --limit, --json
STUB_TEMPLATE = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DATA/gh.log"

# unknown_short <flag-cluster> <letter> -- gh's own wording for short
# flag clusters, kept verbatim so the test reads as "the real gh
# refused" rather than "our stub misnames the failure".
unknown_short() {
  echo "unknown shorthand flag: '$2' in $1" >&2
  exit 1
}

# validate <accepted-flags>... -- consumes flags from "$@" (the
# caller's remaining positional args). Each accepted flag may take a
# value, supplied either as the next token (`--flag value`) or as
# `=value` (`--flag=value`); a flag not in the accepted list emits
# the real gh message and exits 1. A flag with no value is allowed
# when the next token is itself a flag; that matches gh's behaviour
# where some flags default to true and accept an optional value.
validate() {
  while [ "$#" -gt 0 ]; do
    arg="$1"
    case "$arg" in
      --*=*)
        bare="${arg#--}"
        bare="${bare%%=*}"
        case " $ACCEPTED " in
          *" $bare "*) shift ;;
          *) echo "unknown flag: --$bare" >&2; exit 1 ;;
        esac
        ;;
      --*)
        bare="${arg#--}"
        case " $ACCEPTED " in
          *" $bare "*)
            # Flag takes a value when the next token is not itself a flag.
            # If the next token is missing or another flag, the flag has no
            # value -- accept that as a bare form (matches the tool's
            # `--paginate=false` only form in practice).
            if [ "$#" -ge 2 ] && [ "${2#--}" = "$2" ]; then
              shift 2
            else
              shift
            fi
            ;;
          *)
            echo "unknown flag: --$bare" >&2
            exit 1
            ;;
        esac
        ;;
      -*)
        # Short flag cluster; gh parses one letter at a time. Emit
        # gh's own error on the first letter that is not accepted.
        letters="${arg#-}"
        i=0
        while [ "$i" -lt "${#letters}" ]; do
          ch="${letters:$i:1}"
          case " $ACCEPTED_SHORT " in
            *" $ch "*) i=$((i + 1)) ;;
            *) unknown_short "$arg" "$ch" ;;
          esac
        done
        shift
        ;;
      *)
        # Positional argument -- not permitted after the endpoint
        # for `api`, not permitted at all for `pr list` since the
        # tool passes every argument as a flag.
        echo "unknown positional argument: $arg" >&2
        exit 1
        ;;
    esac
  done
}

case "$1" in
  api)
    # Endpoint is the third token (`api` `endpoint` ...).
    if [ "$#" -lt 2 ]; then
      echo "missing endpoint" >&2
      exit 1
    fi
    endpoint="$2"
    shift 2
    ACCEPTED="paginate jq"
    ACCEPTED_SHORT=""
    validate "$@"
    # Strip query string.
    base="${endpoint%%\?*}"
    case "$base" in
      */issues/*/events)
        pr=$(printf '%s' "$base" | sed -n 's@.*/issues/\([0-9]\+\)/events@\1@p')
        page_file="$STUB_DATA/pr_${pr}_events.json"
        if [ -f "$page_file" ]; then
          cat "$page_file"
        else
          echo "[]"
        fi
        ;;
      */pulls/*/reviews)
        pr=$(printf '%s' "$base" | sed -n 's@.*/pulls/\([0-9]\+\)/reviews@\1@p')
        page_file="$STUB_DATA/pr_${pr}_reviews.json"
        if [ -f "$page_file" ]; then
          cat "$page_file"
        else
          echo "[]"
        fi
        ;;
      */pulls/*/comments)
        pr=$(printf '%s' "$base" | sed -n 's@.*/pulls/\([0-9]\+\)/comments@\1@p')
        page_file="$STUB_DATA/pr_${pr}_review_comments.json"
        if [ -f "$page_file" ]; then
          cat "$page_file"
        else
          echo "[]"
        fi
        ;;
      */issues/*/comments)
        pr=$(printf '%s' "$base" | sed -n 's@.*/issues/\([0-9]\+\)/comments@\1@p')
        page_file="$STUB_DATA/pr_${pr}_issue_comments.json"
        if [ -f "$page_file" ]; then
          cat "$page_file"
        else
          echo "[]"
        fi
        ;;
      *)
        echo "[]"
        ;;
    esac
    ;;
  pr)
    if [ "$2" != "list" ]; then
      echo "unknown pr subcommand: $2" >&2
      exit 1
    fi
    shift 2
    ACCEPTED="repo state limit json"
    ACCEPTED_SHORT=""
    validate "$@"
    cat "$STUB_DATA/pr_list.json" 2>/dev/null || echo "[]"
    ;;
  *)
    echo "unknown subcommand: $1" >&2
    exit 1
    ;;
esac
'''


def write_stub_gh(bin_dir: Path, data_dir: Path) -> None:
    p = bin_dir / "gh"
    p.write_text(STUB_TEMPLATE)
    p.chmod(0o755)


# ----- fixture helpers ----------------------------------------------------

def write_pr_list(data_dir: Path, prs: list[dict]) -> None:
    (data_dir / "pr_list.json").write_text(json.dumps(prs))


def write_pr(data_dir: Path, n: int, *, events: list[dict],
             reviews: list[dict] | None = None,
             review_comments: list[dict] | None = None,
             issue_comments: list[dict] | None = None) -> None:
    (data_dir / f"pr_{n}_events.json").write_text(json.dumps(events))
    (data_dir / f"pr_{n}_reviews.json").write_text(
        json.dumps(reviews or []))
    (data_dir / f"pr_{n}_review_comments.json").write_text(
        json.dumps(review_comments or []))
    (data_dir / f"pr_{n}_issue_comments.json").write_text(
        json.dumps(issue_comments or []))


# ----- runner --------------------------------------------------------------

def run_collect(repo: str, stub_path: Path, data_dir: Path,
                ledger_path: Path,
                *extra: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{stub_path}:{env['PATH']}"
    env["STUB_DATA"] = str(data_dir)
    env["FINDINGS_LEDGER"] = str(ledger_path)
    env.pop("GH_TOKEN", None)
    return subprocess.run(
        [str(FINDINGS), "collect", "--repo", repo, "--limit", "50",
         *extra],
        capture_output=True, text=True, env=env)


def run_summary(stub_path: Path, data_dir: Path,
                ledger_path: Path,
                *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{stub_path}:{env['PATH']}"
    env["STUB_DATA"] = str(data_dir)
    env["FINDINGS_LEDGER"] = str(ledger_path)
    env.pop("GH_TOKEN", None)
    return subprocess.run(
        [str(FINDINGS), "summary", *args],
        capture_output=True, text=True, env=env)


def read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ----- assertions ----------------------------------------------------------

def assert_eq(label: str, want, got, failures: list) -> None:
    if want != got:
        failures.append(f"{label}: want {want!r}, got {got!r}")


# ----- shared timestamps ---------------------------------------------------

# Each table row uses a different set of timestamps to make a fixture-level
# cross-contamination impossible. They share the T_ prefix so the cases stay
# readable side by side.

T_CREATE_DRAFT = "2026-01-01T08:00:00Z"
T_OPEN_NON_DRAFT = "2026-01-01T08:00:00Z"
T_READY_1 = "2026-01-01T09:00:00Z"
T_DRAFT_1 = "2026-01-01T10:00:00Z"
T_READY_2 = "2026-01-01T11:00:00Z"
T_DRAFT_2 = "2026-01-01T12:00:00Z"
T_BEFORE_READY = "2026-01-01T08:30:00Z"
T_DURING_READY_1 = "2026-01-01T09:30:00Z"
T_DURING_DRAFT_GAP = "2026-01-01T10:30:00Z"
T_DURING_READY_2 = "2026-01-01T11:30:00Z"
T_AFTER_DRAFT_2 = "2026-01-01T13:00:00Z"


# ----- per-scenario harness ------------------------------------------------

def fresh_setup(tmp: Path, name: str) -> tuple[Path, Path, Path]:
    data_dir = tmp / f"data_{name}"
    data_dir.mkdir()
    bin_dir = tmp / f"bin_{name}"
    bin_dir.mkdir()
    write_stub_gh(bin_dir, data_dir)
    return tmp, bin_dir, data_dir


def scenario(tmp: Path, name: str, pr_list: list[dict], prs: dict[int, dict],
             *, ledger_name: str = "ledger.jsonl"
             ) -> tuple[list, Path, Path, Path]:
    failures: list = []
    _, bin_dir, data_dir = fresh_setup(tmp, name)
    write_pr_list(data_dir, pr_list)
    for n, fixture in prs.items():
        write_pr(data_dir, n, **fixture)
    ledger = tmp / f"{name}_{ledger_name}"
    return failures, bin_dir, data_dir, ledger


# ----- case helpers --------------------------------------------------------

def review(id_: int, at: str, commit: str = "c",
           body: str = "review body",
           url: str = "") -> dict:
    return {"id": id_, "state": "COMMENTED",
            "user": {"login": "reviewer"},
            "submitted_at": at, "commit_id": commit, "body": body,
            "html_url": url or f"https://example/pr/1#review-{id_}"}


def review_comment(id_: int, at: str, commit: str = "c",
                   body: str = "inline body",
                   url: str = "") -> dict:
    return {"id": id_, "user": {"login": "reviewer"},
            "created_at": at, "commit_id": commit,
            "path": "lib/x.py", "body": body,
            "html_url": url or f"https://example/pr/1#rc-{id_}"}


def issue_comment(id_: int, at: str, body: str = "conv body",
                  url: str = "") -> dict:
    return {"id": id_, "user": {"login": "reviewer"},
            "created_at": at, "body": body,
            "html_url": url or f"https://example/pr/1#ic-{id_}"}


# ----- the ten table cases + three correctness cases ----------------------

def case_1_opened_draft_never_ready(tmp, failures):
    """opened draft, never ready: any comment, recorded: no."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "c1",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": True,
                  "createdAt": T_CREATE_DRAFT}],
        prs={1: dict(events=[],
                     reviews=[review(11, T_CREATE_DRAFT),
                              review(12, "2026-12-31T00:00:00Z")],
                     issue_comments=[issue_comment(13, "2026-12-31T00:00:00Z")])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("c1 returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    assert_eq("c1 entries", [], entries, failures)


def case_2_draft_ready_before(tmp, failures):
    """opened draft, later ready, before ready: no."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "c2",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": T_CREATE_DRAFT}],
        prs={1: dict(events=[
                     {"event": "ready_for_review",
                      "created_at": T_READY_1}],
                     reviews=[review(21, T_BEFORE_READY)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("c2 returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    assert_eq("c2 entries", [], entries, failures)


def case_3_draft_ready_after(tmp, failures):
    """opened draft, later ready, after ready: yes."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "c3",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": T_CREATE_DRAFT}],
        prs={1: dict(events=[
                     {"event": "ready_for_review",
                      "created_at": T_READY_1}],
                     reviews=[review(31, T_DURING_READY_1)],
                     issue_comments=[issue_comment(32, T_DURING_READY_1)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("c3 returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    ids = sorted(e["id"] for e in entries)
    assert_eq("c3 entries", ["31", "32"], ids, failures)


def case_4_first_ready_window(tmp, failures):
    """draft -> ready -> draft -> ready: in first ready window: yes."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "c4",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": T_CREATE_DRAFT}],
        prs={1: dict(events=[
                     {"event": "ready_for_review",
                      "created_at": T_READY_1},
                     {"event": "convert_to_draft",
                      "created_at": T_DRAFT_1},
                     {"event": "ready_for_review",
                      "created_at": T_READY_2}],
                     reviews=[review(41, T_DURING_READY_1)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("c4 returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    ids = sorted(e["id"] for e in entries)
    assert_eq("c4 entries", ["41"], ids, failures)


def case_5_draft_between(tmp, failures):
    """draft -> ready -> draft -> ready: while back in draft: no."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "c5",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": T_CREATE_DRAFT}],
        prs={1: dict(events=[
                     {"event": "ready_for_review",
                      "created_at": T_READY_1},
                     {"event": "convert_to_draft",
                      "created_at": T_DRAFT_1},
                     {"event": "ready_for_review",
                      "created_at": T_READY_2}],
                     reviews=[review(51, T_DURING_DRAFT_GAP)],
                     issue_comments=[issue_comment(52, T_DURING_DRAFT_GAP)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("c5 returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    assert_eq("c5 entries", [], entries, failures)


def case_6_opened_non_draft_anytime(tmp, failures):
    """opened non-draft, never converted: any comment: yes."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "c6",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": T_OPEN_NON_DRAFT}],
        prs={1: dict(events=[],
                     reviews=[review(61, T_OPEN_NON_DRAFT,
                                     body="at create"),
                              review(62, "2026-12-31T00:00:00Z",
                                     body="later")],
                     issue_comments=[issue_comment(63, T_OPEN_NON_DRAFT)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("c6 returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    ids = sorted(e["id"] for e in entries)
    assert_eq("c6 entries", ["61", "62", "63"], ids, failures)


def case_7_non_draft_converted_before(tmp, failures):
    """opened non-draft, converted to draft: before conversion: yes."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "c7",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": True,
                  "createdAt": T_OPEN_NON_DRAFT}],
        prs={1: dict(events=[
                     {"event": "convert_to_draft",
                      "created_at": T_DRAFT_1}],
                     reviews=[review(71, T_BEFORE_READY,
                                     body="before draft")])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("c7 returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    ids = sorted(e["id"] for e in entries)
    assert_eq("c7 entries", ["71"], ids, failures)


def case_8_non_draft_converted_after(tmp, failures):
    """opened non-draft, converted to draft: after conversion: no."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "c8",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": True,
                  "createdAt": T_OPEN_NON_DRAFT}],
        prs={1: dict(events=[
                     {"event": "convert_to_draft",
                      "created_at": T_DRAFT_1}],
                     reviews=[review(81, T_AFTER_DRAFT_2)],
                     issue_comments=[issue_comment(82, T_AFTER_DRAFT_2)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("c8 returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    assert_eq("c8 entries", [], entries, failures)


def case_9_exactly_at_ready(tmp, failures):
    """any: exactly at a ready_for_review instant: yes."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "c9",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": T_CREATE_DRAFT}],
        prs={1: dict(events=[
                     {"event": "ready_for_review",
                      "created_at": T_READY_1}],
                     reviews=[review(91, T_READY_1)],   # same second
                     issue_comments=[issue_comment(92, T_READY_1)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("c9 returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    ids = sorted(e["id"] for e in entries)
    assert_eq("c9 entries", ["91", "92"], ids, failures)


def case_10_exactly_at_draft(tmp, failures):
    """any: exactly at a convert_to_draft instant: no."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "c10",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": True,
                  "createdAt": T_OPEN_NON_DRAFT}],
        prs={1: dict(events=[
                     {"event": "convert_to_draft",
                      "created_at": T_DRAFT_1}],
                     reviews=[review(101, T_DRAFT_1)],
                     issue_comments=[issue_comment(102, T_DRAFT_1)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("c10 returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    assert_eq("c10 entries", [], entries, failures)


def case_conversation_source(tmp, failures):
    """A finding posted only as a conversation comment is recorded."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "csrc",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": T_OPEN_NON_DRAFT}],
        prs={1: dict(events=[],
                     reviews=[],
                     review_comments=[],
                     issue_comments=[issue_comment(
                         7777, "2026-01-01T15:00:00Z",
                         body="the only finding is here")])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("csrc returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    assert_eq("csrc entries count", 1, len(entries), failures)
    if entries:
        e = entries[0]
        assert_eq("csrc kind", "issue_comment", e.get("kind"), failures)
        assert_eq("csrc id", "7777", e.get("id"), failures)
        assert_eq("csrc body", "the only finding is here",
                  e.get("body"), failures)


def case_head_sha_only_from_comment(tmp, failures):
    """A conversation comment after a force-push and an ordinary push records
    an empty `head_sha`, not the force-pushed head."""
    force_push_at = "2026-01-01T10:00:00Z"
    force_pushed_head = "force_pushed_head_sha"
    comment_at = "2026-01-01T11:00:00Z"
    pr_current_head = "current_head_at_run"

    _, bin_dir, data_dir, ledger = scenario(
        tmp, "headplain",
        pr_list=[{"number": 1, "headRefOid": pr_current_head,
                  "isDraft": False,
                  "createdAt": T_OPEN_NON_DRAFT}],
        prs={1: dict(
            events=[
                {"event": "head_ref_force_pushed",
                 "created_at": force_push_at,
                 "commit_id": force_pushed_head},
            ],
            reviews=[{
                "id": 1, "state": "COMMENTED",
                "user": {"login": "r"},
                "submitted_at": comment_at,
                "commit_id": "review_commit",
                "body": "review body",
                "html_url": "https://example/pr/1#review-1",
            }],
            review_comments=[{
                "id": 2, "user": {"login": "r"},
                "created_at": comment_at,
                "commit_id": "inline_commit",
                "path": "lib/x.py", "body": "inline body",
                "html_url": "https://example/pr/1#rc-2",
            }],
            issue_comments=[{
                "id": 3, "user": {"login": "r"},
                "created_at": comment_at, "body": "conv body",
                "html_url": "https://example/pr/1#ic-3",
            }],
        )})
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("headplain returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    by_kind = {e["kind"]: e for e in entries}
    # Reviews and inline comments: head_sha is the comment's commit_id.
    assert_eq("headplain review head_sha", "review_commit",
              by_kind.get("review", {}).get("head_sha"), failures)
    assert_eq("headplain inline head_sha", "inline_commit",
              by_kind.get("review_comment", {}).get("head_sha"), failures)
    # Conversation comment: empty, even though a force-push is on the
    # timeline. The reviewer could have seen a different head after an
    # ordinary push the tool cannot see.
    assert_eq("headplain conv head_sha", "",
              by_kind.get("issue_comment", {}).get("head_sha"), failures)
    # And the conversation comment's head_sha must NOT be the
    # force-pushed head. Pin it both positive (the empty value) and
    # negative (the wrong value we are rejecting) so the failure is
    # legible at the assert site.
    assert_eq("headplain conv head_sha != force_pushed_head", True,
              by_kind.get("issue_comment", {}).get("head_sha")
              != force_pushed_head,
              failures)


def case_repo_and_html_url_recorded(tmp, failures):
    """Every entry records `repo` and `html_url`, and findings with the same
    kind and id from two repositories are both kept."""
    base_at = T_OPEN_NON_DRAFT

    repo_a = "Jerhaad/agent-tools"
    repo_b = "Jerhaad/tooling"

    # PR list uses owner/name; a comment on PR 1 in repo_a and a comment
    # on PR 1 in repo_b share (kind, id) but not (repo, kind, id).
    _, bin_dir_a, data_dir_a, ledger_a = scenario(
        tmp, "recaa",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": base_at}],
        prs={1: dict(events=[],
                     reviews=[],
                     issue_comments=[issue_comment(
                         555, base_at, body="from tooling",
                         url="https://example/tooling/pr/1#ic-555")])})
    # Run for repo_a first.
    r = run_collect(repo_a, bin_dir_a, data_dir_a, ledger_a)
    assert_eq("recaa recca returncode", 0, r.returncode, failures)

    # Same body, same id, different repo -- new fixtures, same ledger.
    _, bin_dir_b, data_dir_b = fresh_setup(tmp, "recab")
    write_pr_list(data_dir_b, [
        {"number": 1, "headRefOid": "h1", "isDraft": False,
         "createdAt": base_at}])
    write_pr(data_dir_b, 1,
             events=[],
             reviews=[],
             issue_comments=[issue_comment(
                 555, base_at, body="from tooling",
                 url="https://example/tooling/pr/1#ic-555")])
    r = run_collect(repo_b, bin_dir_b, data_dir_b, ledger_a)
    assert_eq("recaa recb returncode", 0, r.returncode, failures)

    entries = read_ledger(ledger_a)
    assert_eq("recaa entries", 2, len(entries), failures)
    # Each entry carries its repo and html_url.
    repos = sorted(e["repo"] for e in entries)
    assert_eq("recaa repos", [repo_a, repo_b], repos, failures)
    for e in entries:
        if not e.get("html_url"):
            failures.append(
                f"recaa: entry {e.get('id')!r} missing html_url: {e!r}")
    # Rerunning with repo_a must skip its own entry but add nothing new
    # for repo_b.
    r = run_collect(repo_a, bin_dir_a, data_dir_a, ledger_a)
    assert_eq("recaa rerun returncode", 0, r.returncode, failures)
    if "skipped 1" not in r.stdout:
        failures.append(
            f"recaa rerun did not skip repo_a entry: stdout={r.stdout!r}")
    final = read_ledger(ledger_a)
    assert_eq("recaa rerun entries unchanged", 2, len(final), failures)


def case_summary(tmp, failures):
    """Summary counts per class and stage over a date range, unclassified apart,
    from a hand-built ledger.
    """
    ledger = tmp / "summary.jsonl"
    sample = [
        {"repo": "Jerhaad/agent-tools", "pr": 1, "head_sha": "h1", "id": "1",
         "kind": "review", "author": "r", "created_at": T_DURING_READY_1,
         "html_url": "https://example/pr/1#review-1",
         "body": "", "defect_class": "missing check",
         "loop_stage": "review"},
        {"repo": "Jerhaad/agent-tools", "pr": 1, "head_sha": "h1", "id": "2",
         "kind": "review", "author": "r",
         "created_at": T_DURING_READY_2,
         "html_url": "https://example/pr/1#review-2",
         "body": "", "defect_class": "missing check",
         "loop_stage": "implement"},
        {"repo": "Jerhaad/agent-tools", "pr": 1, "head_sha": "h1", "id": "3",
         "kind": "review", "author": "r",
         "created_at": "2026-01-02T00:00:00Z",
         "html_url": "https://example/pr/1#review-3",
         "body": "", "defect_class": "", "loop_stage": ""},
    ]
    ledger.write_text("\n".join(json.dumps(e) for e in sample) + "\n")
    _, bin_dir, data_dir = fresh_setup(tmp, "summary")
    r = run_summary(bin_dir, data_dir, ledger)
    out = r.stdout
    if "missing check" not in out:
        failures.append(
            f"summary: missing 'missing check' class count: {out!r}")
    if "implement" not in out or "review" not in out:
        failures.append(
            f"summary: missing loop_stage counts: {out!r}")
    if "1 not yet classified" not in out:
        failures.append(
            f"summary: missing '1 not yet classified' line: {out!r}")

    # --since excluding the unclassified entry must not count it.
    r = run_summary(bin_dir, data_dir, ledger,
                    "--since", "2026-01-01T10:30:00Z",
                    "--until", "2026-01-01T12:30:00Z")
    out = r.stdout
    if "1 not yet classified" in out:
        failures.append(
            f"summary range: classified-out entry counted as unclassified: "
            f"{out!r}")
    if "missing check" not in out:
        failures.append(
            f"summary range: in-range classified entry dropped: {out!r}")

    # --since after the latest classified entry -> empty in-range set.
    r = run_summary(bin_dir, data_dir, ledger,
                    "--since", "2026-02-01T00:00:00Z")
    out = r.stdout
    if "missing check" in out:
        failures.append(
            f"summary empty range: should not report a class count: {out!r}")


def case_blank_fields_per_entry(tmp, failures):
    """A collected entry reaches summary with both classification fields blank."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "blank",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": T_OPEN_NON_DRAFT}],
        prs={1: dict(events=[],
                     reviews=[review(91, T_OPEN_NON_DRAFT)],
                     issue_comments=[issue_comment(92, T_OPEN_NON_DRAFT)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("blank returncode", 0, r.returncode, failures)
    entries = read_ledger(ledger)
    assert_eq("blank entries", 2, len(entries), failures)
    for e in entries:
        if "defect_class" not in e or e["defect_class"] != "":
            failures.append(
                f"blank: entry {e.get('id')!r} missing or non-blank "
                f"defect_class: {e.get('defect_class')!r}")
        if "loop_stage" not in e or e["loop_stage"] != "":
            failures.append(
                f"blank: entry {e.get('id')!r} missing or non-blank "
                f"loop_stage: {e.get('loop_stage')!r}")


def case_rerun_idempotent(tmp, failures):
    """Running collect twice over the same PRs adds nothing the second
    time, even when there are findings across all three kinds."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "idemp",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": T_OPEN_NON_DRAFT}],
        prs={1: dict(events=[],
                     reviews=[review(11, T_OPEN_NON_DRAFT)],
                     review_comments=[review_comment(
                         12, T_OPEN_NON_DRAFT)],
                     issue_comments=[issue_comment(
                         13, T_OPEN_NON_DRAFT)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("idemp first returncode", 0, r.returncode, failures)
    first_entries = read_ledger(ledger)
    assert_eq("idemp first entries", 3, len(first_entries), failures)
    first_lines = (ledger.read_text().count("\n")
                   if ledger.exists() else 0)

    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("idemp second returncode", 0, r.returncode, failures)
    second_lines = (ledger.read_text().count("\n")
                    if ledger.exists() else 0)
    if second_lines != first_lines:
        failures.append(
            f"idemp second added lines: before={first_lines} "
            f"after={second_lines}")
    if "skipped 3" not in r.stdout:
        failures.append(
            f"idemp second did not report 'skipped 3': stdout={r.stdout!r}")


def case_no_repo_flag_to_gh_api(tmp, failures):
    """The tool never passes -R or --repo to `gh api`."""
    _, bin_dir, data_dir, ledger = scenario(
        tmp, "norepo",
        pr_list=[{"number": 1, "headRefOid": "h1", "isDraft": False,
                  "createdAt": T_OPEN_NON_DRAFT}],
        prs={1: dict(events=[],
                     reviews=[review(91, T_OPEN_NON_DRAFT)],
                     issue_comments=[issue_comment(92, T_OPEN_NON_DRAFT)])
             })
    r = run_collect("owner/repo", bin_dir, data_dir, ledger)
    assert_eq("norepo returncode", 0, r.returncode, failures)
    log = data_dir / "gh.log"
    if not log.exists():
        failures.append("norepo: gh.log missing -- stub never ran")
        return
    for line in log.read_text().splitlines():
        tokens = line.split()
        if len(tokens) < 1 or tokens[0] != "api":
            continue
        for tok in tokens[1:]:
            if tok == "-R" or tok.startswith("-R"):
                failures.append(
                    f"norepo: gh api called with -R: {line!r}")
            if tok == "--repo" or tok.startswith("--repo="):
                failures.append(
                    f"norepo: gh api called with --repo: {line!r}")


# ----- main ---------------------------------------------------------------

def main() -> int:
    failures: list = []
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        # The ten table cases.
        case_1_opened_draft_never_ready(tmp, failures)
        case_2_draft_ready_before(tmp, failures)
        case_3_draft_ready_after(tmp, failures)
        case_4_first_ready_window(tmp, failures)
        case_5_draft_between(tmp, failures)
        case_6_opened_non_draft_anytime(tmp, failures)
        case_7_non_draft_converted_before(tmp, failures)
        case_8_non_draft_converted_after(tmp, failures)
        case_9_exactly_at_ready(tmp, failures)
        case_10_exactly_at_draft(tmp, failures)
        # The three correctness clauses.
        case_conversation_source(tmp, failures)
        case_head_sha_only_from_comment(tmp, failures)
        case_repo_and_html_url_recorded(tmp, failures)
        # House-keeping tests pinning behaviour the issue keeps.
        case_blank_fields_per_entry(tmp, failures)
        case_rerun_idempotent(tmp, failures)
        case_summary(tmp, failures)
        case_no_repo_flag_to_gh_api(tmp, failures)

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())