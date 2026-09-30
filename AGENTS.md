# What a change here must hold

These tools sit between agents, git and remote builders, and each of these has
shipped wrong while every test passed.

- **A wrapper can die without its child.** A signal to the wrapper alone,
  TERM, INT or HUP, leaves a foreground child running unless the wrapper
  forwards it. A remote task then keeps its lock until its own timeout.
- **A push's source can be `HEAD` or a SHA.** A pre-push hook sees
  `HEAD:refs/heads/x` as often as `x:x`. Classify an update by its destination
  ref and inspect the commit by its SHA.
- **A reused worktree or cache can hold a different head from the one
  reported.** A head can move forward, be rewritten, or move back to an
  ancestor. Record the commit something was built from, compare it exactly,
  and report that commit.
- **A reviewer's JSON can carry any type in any field.** One malformed entry
  must come back annotated in its place and never abort the batch around it.
