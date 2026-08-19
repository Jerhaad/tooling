#!/usr/bin/env bash
# Check the newest review notes against the tree, so findings become candidate
# work rather than a branch nobody opens.
#
# Findings age the way issues do: some are fixed before anyone reads them, and
# the rest need a mechanism costed before they are worth dispatching.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
PICK="$PIPELINE_DIR/pick_lane.py"
PROFILE=${PIPELINE_LANE:-$("$PICK" --fallback triage)}
HERMES=$AGENT
LOCK=${PIPELINE_LOCK:-/tmp/agent-pipeline.lock}

cd "$REPO"
git remote set-branches origin '*' >/dev/null 2>&1 || true
git fetch --prune --quiet origin

# Oldest untriaged, not newest. Taking the newest and exiting when its file
# already existed meant a run starting before the review finished skipped that
# night for good: five branches were never triaged that way.
branch=""
for b in $(git for-each-ref --format='%(refname:short)' --sort=refname 'refs/remotes/origin/reviewer-*'); do
	[[ -s "$QUEUE/review-${b##*reviewer-}.md" ]] || { branch="$b"; break; }
done
[[ -z "$branch" ]] && exit 0

date=${branch##*reviewer-}
out="$QUEUE/review-$date.md"

work=$(mktemp -d -t hermes-notes-XXXXXX)
git show "$branch:REVIEW_NOTES.md" >"$work/REVIEW_NOTES.md" 2>/dev/null || exit 0
mkdir -p "$QUEUE"

flock "$LOCK" timeout "${PIPELINE_TRIAGE_TIMEOUT:-3600}" $HERMES -p "$PROFILE" \
	-z "Triage a set of review findings against the repository at $REPO, following the swe-triage skill.

The findings are at $work/REVIEW_NOTES.md. They describe the tree as it was when they were written, which is not the tree you are looking at. For each finding, do the skill's Phase 1 against the current code and report the verdict with a path:line citation; for the ones still real, do Phases 2 through 5.
Carry each finding's severity through verbatim from the notes as a '**Severity:** <value>' line directly under the verdict. The next phase files an issue for the Security and Correctness ones and it reads that line, so a finding without it is a finding nobody acts on.

Write one file, $out, with a section per finding in the skill's output shape. Do not modify anything in $REPO and do not touch the issue tracker." \
	--skills swe-triage --yolo >"$work/stdout" 2>"$work/stderr" || true

[[ -s "$out" ]] || { echo "review notes $date: triage produced nothing ($work)"; exit 0; }

still_real=$(grep -c '^\*\*Verdict:\*\* still real' "$out" || true)
fixed=$(grep -c '^\*\*Verdict:\*\* already fixed' "$out" || true)
echo "review notes $date triaged: $still_real still real, $fixed already fixed — $out"

# A finding that survived verification and never reaches the tracker is a
# finding nobody acts on: fifteen nights of these sat in branches, the oldest
# Critical still true in the tree. Filing is what closes that loop, and it
# writes the queue file the implement phase reads.
"$PIPELINE_DIR/file_findings.py" "$out" \
	--repo "$GH_REPO" --queue "$QUEUE"
