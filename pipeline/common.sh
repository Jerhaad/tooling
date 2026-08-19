# Shared by every phase. Sourced, not executed.
#
# Nothing here knows the name of your project. Every path, command and label
# comes from the environment or the config file, so the phases are the only
# thing this repository contributes -- the logic holds whatever the repo is
# called and whoever runs it.

# Resolved before anything cd's. A phase that resolves $0 after changing
# directory finds nothing: a relative $0 is relative to the old cwd, and the
# sibling script it wanted becomes "./name" against the repository it just
# entered.
PIPELINE_DIR=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)

CONF="${AGENT_PIPELINE_ENV:-$HOME/.config/agent-pipeline/env}"
if [ -r "$CONF" ]; then
	# shellcheck source=/dev/null
	. "$CONF"
fi

# Where the work happens.
REPO=${PIPELINE_REPO:?set PIPELINE_REPO to the repository checkout}
GH_REPO=${PIPELINE_GH_REPO:?set PIPELINE_GH_REPO to owner/name for gh}
QUEUE=${PIPELINE_QUEUE:-$HOME/.local/share/agent-pipeline/queue}
WT_ROOT=${PIPELINE_WT_ROOT:-$HOME/wt}
STATE_DIR=${PIPELINE_STATE_DIR:-$HOME/.local/state/agent-pipeline}

# The gate. One command taking a worktree, exiting non-zero when the tree is not
# fit to push. Whatever proves your project correct goes here.
VERIFY=${PIPELINE_VERIFY:?set PIPELINE_VERIFY to a command taking a worktree path}

# The agent. A command taking a prompt on stdin-equivalent argv; see the phases
# for how it is called.
AGENT=${PIPELINE_AGENT:?set PIPELINE_AGENT to the agent CLI}

# Which issues the implement phase will take. It only dispatches work its gate
# can actually check, so this is the list of labels the gate covers.
VERIFIABLE=${PIPELINE_AREAS:-area:rust|area:db|area:ci}

# Branch naming. `issue-` by default; the number is appended.
BRANCH_PREFIX=${PIPELINE_BRANCH_PREFIX:-issue-}

mkdir -p "$QUEUE" "$STATE_DIR"

# cron records a job completed whether it exited 0 or 128, and every phase is
# silent when it has nothing to say, so a crash reads as a quiet night unless
# it announces itself.
#
# stdout, not stderr: a --no-agent job delivers stdout and drops the rest.
_pipeline_failed() {
	local rc=$? line=$1
	[ "$rc" -eq 0 ] && return 0
	echo "FAILED: ${BASH_SOURCE[1]##*/} exited $rc at line $line"
	return "$rc"
}
trap '_pipeline_failed $LINENO' ERR
set -euo pipefail
