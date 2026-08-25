#!/usr/bin/env bash
# Re-triage the open issues a merge could have made stale.
#
# A clock is the wrong trigger: nothing about an issue changes until the trunk
# does. This fires only when origin/main has moved since the last run, and then
# only re-checks issues whose bodies name a path the merge touched.
#
# Silent when the trunk has not moved -- the cron delivers stdout, so no output
# means no notification.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
STATE=$STATE_DIR/triage-last-trunk

mkdir -p "$(dirname "$STATE")"
cd "$REPO"
git fetch --prune --quiet origin

HEAD_NOW=$(git rev-parse origin/main)
LAST=$(cat "$STATE" 2>/dev/null || true)

if [[ -z "$LAST" ]]; then
	# First run: record where the trunk is and triage nothing. Triaging the whole
	# queue from a standing start is hours of model time nobody asked for.
	echo "$HEAD_NOW" >"$STATE"
	exit 0
fi

[[ "$LAST" == "$HEAD_NOW" ]] && exit 0

written=$(${PIPELINE_TRIAGER:?set PIPELINE_TRIAGER} --repo "$REPO" --since-merge "$LAST" 2>/dev/null || true)
echo "$HEAD_NOW" >"$STATE"

[[ -z "$written" ]] && exit 0

echo "origin/main moved $(git rev-parse --short "$LAST")..$(git rev-parse --short "$HEAD_NOW"); re-triaged:"
while read -r file; do
	[[ -z "$file" ]] && continue
	printf '  %s — %s\n' "$file" "$(grep -m1 '^\*\*Verdict:\*\*' "$file" | sed 's/\*\*Verdict:\*\* //')"
done <<<"$written"
