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
