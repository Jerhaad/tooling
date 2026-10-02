# Sourced by every tool in bin/.

# Sourced here rather than left to the shell profile, so cron jobs and agent
# subprocesses get the values too.
ENV_FILE=${TOOLS_ENV:-$HOME/.config/agent-tools/env}
# An if, not `[ -r ] &&`: under `set -e` the && chain returns 1 when the file is
# absent and takes the whole script with it.
if [ -r "$ENV_FILE" ]; then
	# shellcheck source=/dev/null
	. "$ENV_FILE"
fi

# -P: bin/ is reached through a symlink, and a logical cd resolves ../lib
# against the link's parent rather than the checkout's.
TOOLS_LIB=$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# `manifest <repo> <query> [default]`. Without a default a missing field is
# fatal, which is the right outcome for a gate that would otherwise guess.
manifest() { python3 "$TOOLS_LIB/manifest.py" "$@"; }

env_name() { printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_'; }

# Roles are named for what a host can do, so a project asks for `cluster` and
# never for anyone's hostname.
#
# A lane is an alternative builder: REMOTE_LANE selects HOST_BUILDER_<LANE>
# and leaves every other role alone. There is no fallback, because the env file
# above may set HOST_BUILDER unconditionally, and falling back would send a
# lane's build to the default host silently.
host_for() {
	local var
	var="HOST_$(env_name "$1")"
	if [ "$1" = builder ] && [ -n "${REMOTE_LANE:-}" ]; then
		var="${var}_$(env_name "$REMOTE_LANE")"
	fi
	if [ -z "${!var:-}" ]; then
		echo "set $var to an ssh target for the '$1' role (see hosts.env.example)" >&2
		return 1
	fi
	printf '%s' "${!var}"
}

# Builders whose memory comes from one pool share a dispatch lock. A lane
# without LANE_POOL_<LANE> shares it too: a wrong shared lock costs
# parallelism, a wrong private one costs the protection the lock exists for.
lock_for() {
	local var pool=build
	if [ -n "${REMOTE_TASK_LOCK:-}" ]; then
		printf '%s' "$REMOTE_TASK_LOCK"
		return
	fi
	if [ -n "${REMOTE_LANE:-}" ]; then
		var="LANE_POOL_$(env_name "$REMOTE_LANE")"
		pool=${!var:-build}
	fi
	printf '%s' "$HOME/.local/state/agent-tools/$pool.lock"
}

# A non-interactive ssh reads no profile, so a version manager that puts tools on
# PATH from an interactive shell puts nothing there. Override for a host that
# keeps its shims elsewhere.
# The dollars stay literal here and expand on the far side: these name the
# remote account's home and the remote PATH, and expanding them locally sends
# this machine's paths to a host that has none of them.
: "${REMOTE_PATH:=\$HOME/.local/share/mise/shims:\$HOME/.local/bin:\$PATH}"

# A stable per-tree name for remote directories and databases, so two branches
# never share one.
tree_name() { basename "$1" | tr -c 'A-Za-z0-9_' '_' | tr -s '_' | sed 's/_$//'; }

repo_root() { (cd "${1:-$PWD}" && git rev-parse --show-toplevel); }

# Returns holding a review slot for PROFILE on fd 9, waiting for one first.
# The wait sits outside the review's timeout, because a review queued inside a
# single-slot model server spends its timeout waiting and dies having written
# nothing. The caller closes fd 9, and passes `9>&-` to anything it spawns, or
# that process holds the slot for its whole life.
# The state directory, because a lane sets its own TMPDIR.
acquire_review_slot() {
	local profile="$1" slots_var slots lock i dir printed=0
	slots_var="REVIEW_SLOTS_$(env_name "$profile")"
	slots=${!slots_var:-1}
	dir="${AGENT_STATE_DIR:-$HOME/.local/state/agent-tools}"
	mkdir -p "$dir"
	while true; do
		for ((i = 1; i <= slots; i++)); do
			lock="$dir/hermes-review-${profile}-${i}.lock"
			exec 9>"$lock"
			if flock -n 9; then
				return 0
			fi
			exec 9>&-
		done
		if [ "$printed" -eq 0 ]; then
			echo "waiting for a review slot on profile $profile (have $slots)" >&2
			printed=1
		fi
		sleep 2
	done
}

# Returns holding the lock for PROFILE's GPU group (GPU_GROUP_<PROFILE>, or
# GPU_GROUP_DEFAULT for the root profile) on fd 8, or holding nothing when no
# group is set. Two models sharing GPUs swap on every alternating call, so
# their callers queue here instead, outside their timeouts. Closing fd 8
# releases it.
acquire_gpu_lock() {
	local profile="$1" var group dir printed=0
	var="GPU_GROUP_$(env_name "${profile:-DEFAULT}")"
	group="${!var:-}"
	[ -z "$group" ] && return 0
	dir="${AGENT_STATE_DIR:-$HOME/.local/state/agent-tools}"
	mkdir -p "$dir"
	while true; do
		exec 8>"$dir/gpu-$group.lock"
		if flock -n 8; then
			return 0
		fi
		exec 8>&-
		if [ "$printed" -eq 0 ]; then
			echo "waiting for GPU group $group (profile $profile)" >&2
			printed=1
		fi
		sleep 2
	done
}

# The explicit value, then REVIEW_TIMEOUT_<PROFILE>, then HERMES_REVIEW_TIMEOUT,
# then 900. A caller passes the explicit value only when its own --timeout was
# given: forwarding its default would silently outrank the per-profile value.
review_timeout_for() {
	local profile="$1" explicit="${2:-}" var
	if [ -n "$explicit" ]; then
		printf '%s\n' "$explicit"
		return
	fi
	var="REVIEW_TIMEOUT_$(env_name "$profile")"
	if [ -n "${!var:-}" ]; then
		printf '%s\n' "${!var}"
		return
	fi
	printf '%s\n' "${HERMES_REVIEW_TIMEOUT:-900}"
}
