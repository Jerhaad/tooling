---
name: swe-reviewer
description: Periodic code reviewer. Analyzes a target repository, classifies findings by severity, and writes REVIEW_NOTES.md to a dated reviewer-<YYYY-MM-DD> branch it pushes. Touches only that branch — never main. Intended for a scheduled cron watching a project's health over time.
version: 2.1.0
metadata:
  hermes:
    tags: [software-engineering, code-review, static-analysis, monitoring]
    category: software-engineering
---

# Software Engineer Reviewer (`swe-reviewer`)

A periodic review of a target repository that runs on a schedule — independent
of whether anyone is actively developing — and records its findings on an
isolated review branch. The target repository and its git remote/alias are named
by the caller (a cron prompt); this skill hardcodes no repository or credential.

> **Scope: the review branch only.** The single permitted write to the project
> is creating and pushing `REVIEW_NOTES.md` on a `reviewer-<YYYY-MM-DD>` branch.
> Never modify source, never commit to or merge into `main` or any trunk, never
> push anywhere but the dated review branch. Review notes live on their own
> branch precisely so they never land in the project's code.

## Procedure

### Phase 1: Isolated review branch
1. `git fetch --prune origin`.
2. `git checkout <default-branch> && git pull` (detect the default from
   `git symbolic-ref refs/remotes/origin/HEAD`; do not assume `main`).
3. `git checkout -b reviewer-<YYYY-MM-DD>` (delete a same-named local branch
   first if a prior run left one).

### Phase 2: Static analysis
Detect the stack from its manifests (`Cargo.toml`, `package.json`,
`pyproject.toml`, `go.mod`, …) and adapt — never assume one language. Look for:

1. **Security:** missing authn/authz on routes/middleware; raw SQL or string
   interpolation at data boundaries; unvalidated input; secrets in source;
   permissive CORS; `0.0.0.0` binds where loopback is intended.
2. **Correctness:** lifecycle mis-captures in async/threaded code; unhandled
   error/panic paths; resource/connection leaks on hot paths; boundary errors;
   missing retry/timeout handling.
3. **Design:** schema gaps (missing constraints, FK indexes, audit columns);
   unbounded/unpaginated list endpoints; missing UI error boundaries or
   accessibility attributes; abstractions over a single caller; duplicated
   codepaths that should be one.

### Phase 3: Classify
Sort by impact, highest first: **Critical Security → High/Medium Security →
Correctness bugs → Design/Performance/UX.** Drop anything not tied to a specific
`path:line`.

### Phase 4: Write `REVIEW_NOTES.md`
At the repo root, on the review branch, write a structured `REVIEW_NOTES.md`.
Lead with a summary — counts per severity and the reviewed commit — so
successive runs read as a progress signal. Each finding carries:
1. A one-line title naming the exact issue.
2. Its severity.
3. Precise `path:line` references.
4. The offending snippet or a concrete structural description.
5. A specific, actionable recommendation — a concrete code or structural change,
   never "improve the security." Downstream automation applies these verbatim,
   so vagueness forces it to guess.

### Phase 5: Commit and push the review branch
**Before committing, verify you are on the review branch:** `git branch --show-current` must print `reviewer-<YYYY-MM-DD>`. If it prints anything else, abort — the review commit would land on a trunk branch.

Then: `git add REVIEW_NOTES.md && git commit -m "review: <YYYY-MM-DD>"` then
`git push origin reviewer-<YYYY-MM-DD>`. Only `REVIEW_NOTES.md` may be committed.

## Pitfalls
- **Touching source or a trunk.** The tree must be identical to the reviewed
  commit except for the added `REVIEW_NOTES.md`; nothing merges to `main`.
- **Review commit lands on main.** If the branch-creation step fails or is
  skipped (e.g., due to an approval gate), `git commit` writes to whatever
  branch is checked out — usually `main`. Always verify `git branch --show-current`
  before committing.
- **Vague recommendations.** "Fix the auth" is not actionable; show the change.
- **Assuming the stack.** Detect it from the manifests.

## Verification
Before finishing: `git status` shows only the added `REVIEW_NOTES.md`, and
`git diff HEAD~1` confirms the notes are the sole change on the review branch.
