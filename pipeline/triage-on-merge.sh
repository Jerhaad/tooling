#!/usr/bin/env bash
# Re-triage the open issues a merge could have made stale.
#
# A clock is the wrong trigger: nothing about an issue changes until the trunk
# does. This fires only when origin/main has moved since the last run, and then
# only re-checks issues whose bodies name a path the merge touched.
#
# Silent when the trunk has not moved -- the cron delivers stdout, so no output
# means no notification.
#
# State advances only when the triager reports the gap closed, which it does by
# touching $LEDGER.done. A run stopped by the 3600s cap leaves the ledger naming
# what it finished, so the next run has strictly less to do. See bin/hermes-triage
# for the ledger itself.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
STATE=$STATE_DIR/triage-last-trunk
LEDGER=$STATE_DIR/triage-issues
DONE=$LEDGER.done

mkdir -p "$(dirname "$STATE")" "$(dirname "$LEDGER")"
cd "$REPO"
git fetch --prune --quiet origin

HEAD_NOW=$(git rev-parse origin/main)
LAST=$(cat "$STATE" 2>/dev/null || true)

if [[ -z "$LAST" ]]; then
	# First run: record where the trunk is and triage nothing. Triaging the whole
	# queue from a standing start is hours of model time nobody asked for.
	# A stale ledger from a previous repository is cleared so it cannot seed
	# the next gap's skip list with issues that belong to a gap we no longer
	# have.
	echo "$HEAD_NOW" >"$STATE"
	rm -f "$LEDGER" "$DONE"
	exit 0
fi

[[ "$LAST" == "$HEAD_NOW" ]] && exit 0

# Cleared before the run, so the sentinel can only ever mean "the triager
# finished during THIS run". A run whose triager completed but whose script was
# killed before it could advance state leaves the file behind, and the next run
# would otherwise read someone else's completion as its own: it would advance
# past a gap it never triaged, and the ledger it cleared would take the record
# of what was skipped with it.
rm -f "$DONE"

# The triager appends to the ledger after each successful triage and touches
# $DONE only when it reaches the end of the candidate list. A SIGTERM between
# appends loses at most the issue it was inside; the rest of the ledger stays
# for the next run.
written=$(${PIPELINE_TRIAGER:?set PIPELINE_TRIAGER} --repo "$REPO" --since-merge "$LAST" --ledger "$LEDGER" 2>/dev/null || true)

[[ -z "$written" ]] && {
	# Either no candidates matched this gap, or the run was killed before
	# anything finished. The first case closes the gap (state advances); the
	# second case leaves the next run with the same candidates. The sentinel
	# distinguishes them.
	if [[ -f "$DONE" ]]; then
		echo "$HEAD_NOW" >"$STATE"
		rm -f "$LEDGER" "$DONE"
	fi
	exit 0
}

echo "origin/main moved $(git rev-parse --short "$LAST")..$(git rev-parse --short "$HEAD_NOW"); re-triaged:"
while read -r file; do
	[[ -z "$file" ]] && continue
	printf '  %s — %s\n' "$file" "$(grep -m1 '^\*\*Verdict:\*\*' "$file" | sed 's/\*\*Verdict:\*\* //')"
done <<<"$written"

# Display before advancing: if a state-advance error prints, the re-triage
# summary above is the answer the operator wants first.
if [[ -f "$DONE" ]]; then
	echo "$HEAD_NOW" >"$STATE"
	rm -f "$LEDGER" "$DONE"
fi
