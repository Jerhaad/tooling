#!/usr/bin/env python3
"""File a GitHub issue for each triaged finding that survived verification.

Only findings triage marks "still real": the reviewer has claimed axum accepts
unbounded JSON bodies, when Json carries a 2 MB DefaultBodyLimit, and a wrong
issue costs more to retract than to file.

Filing also writes the queue file the implement phase reads, so a costed
finding is not costed twice.
"""
import argparse
import hashlib
import os
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

# Design/Performance/UX findings stay in REVIEW_NOTES.md: a night's review runs
# ~13 findings and most are opinions about shape, which would bury the defects.
#
# Excluded rather than included, because the reviewer's severity vocabulary is
# not stable between nights -- one wrote "Critical Security" and "Correctness",
# another wrote bare "Critical" and "High" for the same kinds of finding. An
# inclusion list filed nothing from the second. Excluding also fails the safe
# way: an unfamiliar severity gets filed, and a wrongly filed issue is visible
# and closable where a dropped Critical is neither.
# Stamped into every issue this files, and how a later run knows which issues
# are its own. Set it per project only if two pipelines file into one tracker,
# and set it before the first filing: changing it orphans everything already
# filed, which silently drops dedup back to exact-title matching.
MARKER = os.environ.get("PIPELINE_FINDING_MARKER", "agent-finding:")

SKIPPED_SEVERITIES = re.compile(r"design|performance|\bux\b|style|nit", re.I)

# The implement phase only dispatches what its gates can verify, so an issue
# whose area it will not accept is filed for a human and skipped by the loop.
def area_map():
    """prefix=label pairs, comma separated, from PIPELINE_AREA_LABELS.

    Which directory means which label is a fact about a project, not about
    triage, so it is configuration. Unset means no labels -- the issue still
    gets filed, it just will not be picked up by a phase that filters on them.
    """
    raw = os.environ.get("PIPELINE_AREA_LABELS", "")
    out = []
    for pair in raw.split(","):
        prefix, _, label = pair.partition("=")
        if prefix.strip() and label.strip():
            out.append((prefix.strip(), label.strip()))
    return out


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def split_findings(text):
    """One dict per `# Finding N: title` block."""
    out = []
    parts = re.split(r"^#{1,3}\s*Finding\s+(\d+)\s*[:\u2014\u2013-]\s*(.+?)$", text, flags=re.M)
    for i in range(1, len(parts), 3):
        out.append({"n": parts[i], "title": parts[i + 1].strip(), "body": parts[i + 2]})
    return out


def field(body, name):
    m = re.search(rf"^\*\*{name}:\*\*\s*(.+?)$", body, flags=re.M | re.I)
    return m.group(1).strip() if m else ""


def section(body, heading):
    m = re.search(rf"^## {heading}\s*$(.*?)(?=^## |\Z)", body, flags=re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def paths(body):
    return re.findall(r"`?((?:[\w.\-]+/)+[\w.\-]+\.\w+)(?::\d+)?`?", body)


def labels_for(body):
    found = []
    for p in paths(body):
        for prefix, label in area_map():
            if p.startswith(prefix) and label not in found:
                found.append(label)
    return found


def fingerprint(title):
    """Stable across nights: the same finding carries its title forward.

    Keyed on the title alone, not on path:line -- a line number moves when the
    file above it changes, and a finding that moved is the same finding.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
    return f"{slug}-{hashlib.sha1(title.encode()).hexdigest()[:8]}"


STOPWORDS = {"the", "a", "an", "with", "without", "on", "in", "to", "for",
             "and", "or", "of", "is", "are", "no", "not", "its", "via"}


def tokens(title):
    return {w for w in re.findall(r"[a-z0-9_]+", title.lower()) if w not in STOPWORDS}


def ask_judge(title, candidates):
    """Ask a model whether this finding is one of these issues.

    Only for the middle ground the mechanical checks cannot settle.

    Fails open: a judge that errors, times out or answers unintelligibly files
    the finding. A duplicate issue is visible and closable; one dropped by a
    confused judge is neither.
    """
    judge = os.environ.get("PIPELINE_DEDUP_JUDGE")
    if not judge or not candidates:
        return None
    listing = "\n".join(f"{n}. {t}" for n, t in candidates)
    prompt = (
        "Two bug reports are the same finding when they describe the same defect "
        "in the same place, however differently they are worded.\n\n"
        f"New finding:\n{title}\n\nExisting issues:\n{listing}\n\n"
        "Reply with the number of the issue that is the same finding, or NONE. "
        "Reply with nothing else."
    )
    try:
        r = subprocess.run(shlex.split(judge) + [prompt], capture_output=True,
                           text=True, timeout=int(os.environ.get("PIPELINE_DEDUP_TIMEOUT", "180")))
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"  judge unavailable ({e.__class__.__name__}); filing", file=sys.stderr)
        return None
    m = re.search(r"\b(\d+)\b", r.stdout or "")
    if not m:
        return None
    num = int(m.group(1))
    return num if any(num == n for n, _ in candidates) else None


def already_filed(repo, title, fp):
    """Fingerprint, exact title, then token overlap.

    The reviewer rephrases what it carries forward: "publishes database port
    with trust authentication" and "publishes the database to the host with
    `trust` auth" are one defect written twice. Overlap catches the rewording;
    requiring most of the words keeps different findings apart.

    Searching the fingerprint alone would file a duplicate nightly -- the marker
    is an HTML comment, which GitHub does not reliably index.
    """
    want = tokens(title)
    seen = {}
    near = []
    for query in (f'in:title {title}', " ".join(sorted(want))[:200]):
        r = run(["gh", "issue", "list", "--repo", repo, "--state", "all",
                 "--search", query, "--json", "number,title,body", "--limit", "50"])
        if r.returncode != 0:
            raise SystemExit(f"gh issue list failed: {r.stderr.strip()}")
        for issue in json.loads(r.stdout or "[]"):
            seen[issue["number"]] = issue
    for num, issue in sorted(seen.items()):
        body = issue.get("body") or ""
        if fp in body or issue.get("title") == title:
            return num
        # Only against issues this pipeline filed: an overlap rule loose enough
        # to catch a rewording is loose enough to collide with a human's issue.
        if MARKER not in body:
            continue
        have = tokens(issue.get("title", ""))
        if not (want and have):
            continue
        overlap = len(want & have) / len(want | have)
        if overlap >= 0.6:
            return num
        # Suspicious but not decisive: "Dev compose exposes API without
        # authentication on host network" against "Dev compose runs the API with
        # no bearer token, published to the host" scores 0.36 and is one defect.
        # Lowering the threshold instead would start dropping real findings.
        if overlap >= 0.25:
            near.append((num, issue.get("title", "")))
    return ask_judge(title, near)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("triage_file")
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--queue", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    text = Path(args.triage_file).read_text()
    filed = skipped = existing = 0

    for f in split_findings(text):
        if not field(f["body"], "Verdict").lower().startswith("still real"):
            skipped += 1
            continue
        severity = field(f["body"], "Severity")
        if SKIPPED_SEVERITIES.search(severity):
            skipped += 1
            continue

        fp = fingerprint(f["title"])
        if (n := already_filed(args.repo, f["title"], fp)) is not None:
            print(f"  #{n} already tracks: {f['title']}")
            existing += 1
            continue

        body = "\n".join([
            f"**Severity:** {severity}",
            "",
            section(f["body"], "What the code does today"),
            "",
            "## Acceptance criteria",
            section(f["body"], "Acceptance criteria"),
            "",
            "## Candidate mechanisms",
            section(f["body"], "Candidate mechanisms"),
            "",
            f"Found by the nightly review and verified against the tree by triage.",
            f"<!-- {MARKER} {fp} -->",
        ])
        cmd = ["gh", "issue", "create", "--repo", args.repo,
               "--title", f["title"], "--body", body]
        for lab in labels_for(f["body"]):
            cmd += ["--label", lab]
        if args.dry_run:
            print(f"  would file: {f['title']}  labels={labels_for(f['body'])}")
            filed += 1
            continue
        r = run(cmd)
        if r.returncode != 0:
            print(f"  FAILED to file {f['title']!r}: {r.stderr.strip()}", file=sys.stderr)
            continue
        num = r.stdout.strip().rstrip("/").split("/")[-1]
        print(f"  #{num} filed: {f['title']}")
        filed += 1

        # The implement phase reads this. Writing it here is what stops the
        # same finding being costed twice by two different phases.
        Path(args.queue, f"issue-{num}.md").write_text(
            f"# Finding {f['n']}: {f['title']}\n{f['body']}"
        )

    print(f"filed {filed}, already tracked {existing}, skipped {skipped}")


if __name__ == "__main__":
    main()
