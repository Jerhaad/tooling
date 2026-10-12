---
name: pr-review-coordinator
description: Drive pull requests to mergeable from the reviewer's seat - review as the reviewer account, hand each review to an agent lane on the agent host with agent-fix-pr, check what comes back, and repeat until approved, with a hard round cap. Use when asked to "address the review with lanes", "iterate until mergeable", "kick off fix lanes", or to run the review loop across a set of PRs.
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
2. **Write each comment as a brief.** Severity (`[P1]` breaks a user-visible
   path, `[P2]` a real defect off the main path, `[P3]` a latent risk), the
   failure, then the mechanism to fix it, and the tempting wrong mechanism by
   name. A lane left to choose its mechanism picks wrong about a third of the
   time. For a change to identity, state or concurrency, include the table of
   input × existing state → outcome; a lane tests only the rows it is given.
3. **Decide.** Nothing worth changing: post an `APPROVE` review and move to the
   next PR. Otherwise post a `COMMENT` review and keep its id.
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
5. **Wait** with a background poll on `agent-fix-pr --status --pr N`, which
   exits 5 while a round runs. Rounds take tens of minutes; do not poll in the
   foreground.
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

Approval means the reviewer has no change to ask for. It does not merge, and
it does not take a draft out of draft: the author's side does that with
`pr-ready`, whose checks (CI, gates, prose) are separate from the review. Say
in the approval what the reviewer did not check, such as a merge order two PRs
share.
