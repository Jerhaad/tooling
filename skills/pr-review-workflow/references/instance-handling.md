# Instance Handling

## Non-standard GitHub instances (private forks, github-reviewer, GHE)

Web extraction tools (`web_extract`, `web_url_read`) cannot reach private or custom GitHub instances. When the repo URL returns 404 from the public GitHub API:

1. **Check for a local clone** — `find "$HOME" -maxdepth 3 -name ".git" -type d | grep -i <repo-name>`.
2. **Inspect the remote** — `git remote -v` in the local clone to find the actual instance URL.
3. **Fetch the PR via git** — `git fetch origin pull/<N>/head:pr<N>` (works with SSH remotes on any instance).
4. **Diff against main** — `git diff origin/main..pr<N>` to get the full patch.

## Branch naming conflicts

The swe-reviewer procedure creates `reviewer-<YYYY-MM-DD>`. If that branch already exists on the remote (rejected push, non-fast-forward), fall back to `reviewer-PR<N>` or `reviewer-<YYYY-MM-DD>-<N>` where N is a counter. Always check with `git fetch origin reviewer-<date>` before pushing.

## PR metadata extraction

When the GitHub API is unreachable, extract PR metadata from the local clone:
- `git log --oneline pr<N>..origin/main` — commits introduced by the PR
- `git diff origin/main..pr<N> --stat` — files changed
- `git diff origin/main..pr<N>` — full patch
