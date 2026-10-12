---
name: swe-review-fix
description: Answers one posted code review on a pull request branch the caller has checked out — fixes what each comment asks, adds the test that would have caught it, commits on top of the branch, and writes a reply per comment for the caller to post. Never pushes, rewrites history, or answers on GitHub itself.
version: 1.0.0
---

# Review round (`swe-review-fix`)

The branch already implements its issue; a reviewer has read it and left
comments. This round turns those comments into commits the reviewer can check
one by one. The caller pushes, and posts your replies, only after its own gate
passes, so a reply naming a commit is a claim the caller will test.

## What each comment gets

Every comment in the brief is headed `### comment <id> at <path>:<line>`.
Answer every one, in one of two ways:

- **Fix it.** Make the change the comment asks for, by the mechanism it names
  when it names one. Add or change a test that fails without the fix, at the
  layer the comment talks about: a route's response is tested through the
  route, not through the repo function under it. Commit the fix on its own,
  so the reply can name one sha.
- **Push back.** When the comment is wrong about the code, say why with the
  evidence: the file and line that shows it, or the command and its output.
  Change nothing for it. A reviewer would rather read a correct refusal than
  a fix for a bug that is not there.

A comment marked P3 can be answered with a reason it is left alone, when fixing
it would widen the PR.

## The rules of the branch

- New commits on top of the branch's head. Never amend, rebase, reset or
  squash: the caller refuses to push a branch whose history moved, and the
  reviewer reads this round as the commits added since their review.
- Never push, never open or edit a pull request, never post on GitHub.
- Commit subjects say what the code now does. No review numbers, round
  numbers, or reviewer names in commits or source.
- Run the verification command the brief gives until it passes, as in
  `swe-updater`'s Phase 4.

## Replies

Write the replies file the brief names, as a JSON array with one object per
comment:

```json
[
  {"comment_id": 123, "body": "Fixed in 1a2b3c4: the retry now stops after the first refused connection, and `refused_connection_returns_at_once` covers it."},
  {"comment_id": 124, "body": "Left as is: the caller already validates this field (`handler.rs:72`), so a second check would only repeat it."}
]
```

`comment_id` is the number from the comment's heading. Name the short sha of
the commit that fixes it. A reply naming a commit the branch does not have is
not posted. The file is posted even when you commit nothing, so a round that
only pushes back still answers. One or two sentences: the reviewer has the diff.

Then write HANDOVER.md as `swe-updater` describes, listing each comment id
with fixed or pushed back.
