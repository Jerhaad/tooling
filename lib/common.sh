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

# Roles are named for what a host can do, so a project asks for `cluster` and
# never for anyone's hostname.
host_for() {
	local role=$1 var
	var="HOST_$(printf '%s' "$role" | tr '[:lower:]-' '[:upper:]_')"
	if [ -z "${!var:-}" ]; then
		echo "set $var to an ssh target for the '$role' role (see hosts.env.example)" >&2
		return 1
	fi
	printf '%s' "${!var}"
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
