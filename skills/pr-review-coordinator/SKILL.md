---
name: pr-review-coordinator
description: Drive pull requests to mergeable from the reviewer's seat - review as the reviewer account, hand each review to an agent lane on the agent host with agent-fix-pr, check what comes back, repeat until approved, with a hard round cap; and brief an agent lane on new work before its PR exists, via `hermes-implement --issue N --extra BRIEF`. Use when asked to "address the review with lanes", "iterate until mergeable", "kick off fix lanes", to run the review loop across a set of PRs, or to "brief this lane".
---

# PR review coordinator

Three identities, and keeping them apart is what makes the loop readable on
GitHub:

| Role | Who | Where |
|---|---|---|
| Author | the account the PR's commits come from | the agent host's git and `gh` |
| Implementer | an agent lane, by profile | the agent host, through `agent-fix-pr` |
| Reviewer | the reviewer account, through `gh` here | this host |

Every review is posted as the reviewer and every reply comes from the author's
side, so a thread reads as a conversation. The ledger `agent-fix-pr` keeps on
the agent host, not this conversation, is the record of what each round did.

## Briefing a lane on new work

New work starts as an issue. Write a brief and pass it as
`hermes-implement --issue N --extra BRIEF`. The brief is the step before the
loop. Name the brief sections inline (Goal, Context, Mechanism, Constraints,
Tests, Acceptance, Outputs).

**Check every mechanism before prescribing it.** Each instruction
(command line, flag, value form) is checked at every call site against
tree and settings. Prescribe only where you checked it; state the goal
and constraint otherwise.

**Give the real shapes.** For every value crossing a boundary (env var,
config entry, CLI argument, agent command), Context lists the forms
(single word, command with arguments, empty, with spaces); Tests cover
each. Read the value from the live source.

**Every invariant gets a test.** Each "stays as it is" in Constraints
names the behaviour concretely (exit on failure, on timeout, fds,
retries) with a matching Acceptance row. An invariant without a test is
guidance the lane cannot honour.

**Failure paths are acceptance rows.** For a new wrapper or helper,
the brief names what the caller sees when the wrapped command fails,
times out, or goes missing. A silent failure path hides the bug.

**Size work to the attempt timeout.** Split an issue into runs that
each fit. Commit each verified item before starting the next.

**Search first.** Before briefing, search existing issues. The brief names
its issue.

**Name the wrong mechanism.** In Mechanism, write the tempting wrong
approach next to the right one and say why each fails. The lane has
only the right answer otherwise and has nothing to look out for.

## Per PR, one at a time

Finish one PR before starting the next. Take stacked PRs parent first: a fix to
the parent changes what the child is reviewed against.

`agent-fix-pr` refuses a PR whose body links no issue (`Closes #N` or
`Part of #N`) with exit 2, so check the body before the first review.

1. **Review the head.** Note the head sha first, and post the review against
   it (`commit_id`). The first round reads the whole diff; later rounds read
   `start..end` from the ledger, then check each earlier comment against the
   new code and its reply. Confirm every finding against the source before
   posting it: cite the file and line, and the concrete input that breaks.

   **Run what changed.** A new wrapper: drive it with a failing stub
   and report the output. A changed call site: run it against the
   live value, not the example.

   **Model reviewers are a second opinion.** `hermes-review-pr` runs are
   never a substitute for the coordinator's review: verify each finding
   before relaying it. A slow local profile needs `--timeout` well above
   the default.

2. **Write each comment as a brief.** Severity (`[P1]` breaks a user-visible
   path, `[P2]` a real defect off the main path, `[P3]` a latent risk), the
   failure, then the mechanism to fix it, and the tempting wrong mechanism by
   name. A lane left to choose its mechanism picks wrong about a third of the
   time. For a change to identity, state or concurrency, include the table of
   input × existing state → outcome; a lane tests only the rows it is given.
3. **Decide.** Nothing worth changing: the content is agreed. Dispatch the
   finishing run (see Lane-side merge work), then approve at its final sha.
   Otherwise post a `COMMENT` review and keep its id.
4. **Dispatch the round:**

   ```bash
   ssh <agent-host> '<runtime settings> <tooling>/bin/agent-fix-pr \
     --repo OWNER/NAME --pr N --review ID --profile <profile> --detach'
   ```

   The runtime settings are the operator's, kept on the agent host rather than
   in this repository: `PIPELINE_IMPLEMENTER` (the agent's driver, with any
   options it needs to load the `swe-review-fix` skill) and, for an agent that
   can fall back to another model, `AGENT_FIX_PR_MODELS`. Before dispatching,
   count the profile's live sessions: a hosted model has a concurrency limit,
   and a session past it may be answered by a fallback.

   **Cross-repo PRs need `--worktree`.** Until `agent-fix-pr` picks the clone
   per repository, a PR on a repo other than the one the operator's settings
   name needs `--worktree`.
5. **Wait** with a background poll on `agent-fix-pr --status --pr N`, which
   exits 5 while a round runs. Rounds take tens of minutes; do not poll in the
   foreground.

   **Name the poll pattern carefully.** A background poll matching processes
   by name must not match its own command line. `'[a]gent-fix-pr'` matches
   the process name but not the poll's own command line. Or poll
   `--status` or the round's pid.
6. **Read the round.** The ledger's last line has `verdict`, `gate`, `pushed`,
   `replied` and `models`. A model in `models` other than the profile's own
   means the round ran on a fallback: judge its work as you would a weaker
   model's. Then read the diff `start..end`: grep it for `***`, which some
   agents write over secret-shaped text in the source, and check each fix used
   the mechanism the comment named.

   A round that committed and was not pushed for a reason outside its work,
   such as a gate log showing its tools could not start, is fixed on the host
   and taken up with `agent-fix-pr --resume`, which does not spend a round.
   A gate that ran and failed on the lane's code is a finished round: review
   what it left, and say what failed in the next review. So is `answered`: the
   lane pushed back on every comment and committed nothing, and its replies are
   on the threads. Rule on each one. A round that did not push still counts.
7. Back to step 1.

## The cap

`agent-fix-pr` refuses a sixth round per PR (`--max-rounds`). When a PR
reaches the cap without an approval, **halt the whole run**: dispatch nothing
further on any PR, and report to the user:

- each round's review id, verdict, and what the lane did with each comment;
- which findings recurred, and whether the brief or the lane is at fault: a
  finding that comes back in new words usually means the mechanism was never
  stated;
- what you would change before running the loop again.

The cap counts rounds, not findings. Five rounds that each fix their comments
but surface new ones is a design problem in the PR, which is the user's call,
not another round's.

## Merge readiness

Approval means the reviewer has no change to ask for. It comes after `pr-ready` has taken the PR out of draft and after the patch-id check, at the final sha. It does not merge. Say in the approval what the reviewer did not check, such as a merge order two PRs share.

## Lane-side merge work

The coordinator never pushes a PR branch. Implementation, the first
push, opening the draft, rebasing, squashing and reauthoring are lane
work. The coordinator reads, reviews, approves and merges. Pushing the
head it approves is approving its own work.

When the content is agreed, the lane runs these in order, and only these:

1. `condense-prose` over the PR's own lines and the PR body. Lane profiles
   ship without the skill; the brief points at
   `skills/condense-prose/SKILL.md`.
2. The squash, with the coordinator's subject and body. The lane
   commits under its own configured git identity. The brief never sets
   one (`-c user.name`, `--author`).
3. The push.
4. `pr-ready N <worktree>`. Takes the PR out of draft when CI, the
   gates and the prose pass.

The coordinator then reviews the change since its last review (it should
be prose only), checks the patch-id against the approved head, and approves
at the final sha. A draft is never approved, and approval comes last.

When the lane reports the branch is ready, the coordinator compares the
PR's whole diff against main before and after:

```bash
git diff origin/main...<approved-sha> | git patch-id --stable
git diff origin/main...<lane-sha>     | git patch-id --stable
```

The first fields must match. If the base moved, compare `git diff <old-base>..<approved-sha>` with `git diff <new-base>..<lane-sha>` instead, then approve again at the new sha.

Squash order follows merge order, parents first. Until `agent-fix-pr`
has a finishing mode, run steps 1–4 as one `hermes-implement` brief
naming the PR, the approved sha, the subject and body, and the
patch-id check.

## Skill freshness

The coordinator loads this skill from a local checkout of the tooling
repo. After a merge to `skills/`, update it to current main before the
next round.

When reporting a round, name its commit: a reviewer reading a finding
against a skill they cannot see is reading the wrong report.

## Interim notes

These are how the loop runs while their fixes have not landed. Each is
written so it can be deleted when its fix does.

- Review items go in line comments, not in the review body, until the
  review driver answers body items.
- Two model reviewers on one PR need separate clones. The review branch
  is named per PR; the second fails on a branch that already exists.
