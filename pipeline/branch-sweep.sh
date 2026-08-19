#!/usr/bin/env bash
# Report branches whose work is already on the trunk, and worktrees left behind.
#
# No model: `git log` answers this exactly, and a wrong answer here deletes
# work. Reports only -- pruning stays a decision someone makes while looking at
# the list.
#
# Silent when there is nothing to report.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

out=$(${PIPELINE_BRANCH_SWEEP:?set PIPELINE_BRANCH_SWEEP} "$REPO")
landed=$(sed -n '/^== landed/,/^$/p' <<<"$out" | sed '1d;/^$/d')

[[ "$landed" == "(none)" || -z "$landed" ]] && exit 0

echo "Branches already on the trunk in $REPO:"
sed 's/^/  /' <<<"$landed"
echo
echo "Delete them with: repo-branch-sweep $REPO --prune"
