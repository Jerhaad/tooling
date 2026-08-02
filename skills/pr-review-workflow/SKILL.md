---
name: pr-review-workflow
description: Workflow for reviewing pull requests across different GitHub instances (public, private, github-reviewer, GHE). Covers fetching PRs when web tools fail, handling branch conflicts, and extracting metadata from local clones.
version: 1.0.0
metadata:
  hermes:
    tags: [software-engineering, code-review, pull-requests]
    category: software-development
---

# PR Review Workflow

Procedural skill for reviewing pull requests when the repo is not publicly accessible via web extraction tools.

## When to Use

- The swe-reviewer skill is loaded for the actual review content, but this skill handles the **setup** phase: fetching the PR, getting the diff, and handling instance-specific quirks.
- Any time you need to review a PR and `web_extract` or the GitHub API returns 404.

## Procedure

### Phase 1: Find the Local Clone

1. Search for a local clone: `find "$HOME" -maxdepth 3 -name ".git" -type d | grep -i <repo-name>`
2. Navigate to the clone and check the remote: `git remote -v`
3. Verify the branch exists: `git branch -a | grep -i <branch-or-pr>`

### Phase 2: Fetch the PR via Git

Web extraction does **not** work on private instances (github-reviewer, GHE, GitHub Enterprise). Use git instead:

```bash
git fetch origin pull/<N>/head:pr<N>
```

This works with SSH remotes on any instance. Then inspect:

- `git log --oneline pr<N>..origin/main` — commits in the PR
- `git diff origin/main..pr<N> --stat` — files changed summary
- `git diff origin/main..pr<N>` — full patch for review

### Phase 3: Handle Branch Conflicts

When creating a reviewer branch (`reviewer-<YYYY-MM-DD>`), the remote may already have one from a prior run:

```bash
git fetch origin reviewer-<YYYY-MM-DD>
git log --oneline origin/reviewer-<YYYY-MM-DD> -1
```

If it exists, fall back to `reviewer-PR<N>` or `reviewer-<YYYY-MM-DD>-<N>`.

### Phase 4: Run the Review

1. Load the `swe-reviewer` skill for the review methodology.
2. Feed the diff output from Phase 2 into the review process.
3. Write `REVIEW_NOTES.md` to the workspace.
4. Create and push the reviewer branch.

## Pitfalls

- **Web tools fail silently** on private instances — always try git fetch first if the URL returns 404.
- **Branch naming collisions** are common with date-based reviewer branches — always check before pushing.
- **SSH-only remotes** are common on private instances — ensure SSH keys are configured.
