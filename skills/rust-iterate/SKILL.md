---
name: rust-iterate
description: Edit Rust locally, compile and unit-test it in a container on the builder over SSH, and push only once it is green. Use when implementing or fixing a Rust change, especially with no local `cargo`.
version: 1.0.0
metadata:
  hermes:
    tags: [software-engineering, rust, build, iterative-development]
    category: software-engineering
---

# Rust iterate (`rust-iterate`)

Write Rust, compile it on the builder, read the compiler, fix, repeat — then push
once it builds and its tests pass. This host has no `cargo`, so guessing at an
API and pushing to find out wastes CI runs. The compiler is the authority;
reach it locally.

The build host is whatever `$HOST_BUILDER` names -- an ssh target set in
`~/.config/agent-tools/env`. It has a native Rust toolchain
(`cargo`/`rustc`, stable via rustup) and the horsepower to compile the workspace.
You run `cargo` there over SSH; its crate cache lives in `~/.cargo` and the build
output under the synced tree's `target/`, so repeat builds are incremental.

## The loop

Work inside a git worktree on a feature branch (a pushed feature branch does not
trigger CI — only `main` pushes and open PRs do — so iterate freely).
`WT` is the absolute path of your worktree; `CRATE` is the crate you changed
(e.g. `db`, `api-rest`).

**1. Edit** the source in `WT`.

**2. Sync to the builder** (rsync over SSH — only changed files move, so this is
near-instant after the first pass):

```bash
rsync -a --delete --exclude=.git --exclude=target \
  -e "ssh -o BatchMode=yes" \
  "$WT/" "$HOST_BUILDER":/tmp/hermes-build/
```

The trailing slash on `"$WT/"` copies the tree's contents into the target.
`--delete` removes files on the builder that you have deleted locally, so a renamed
or removed module cannot linger and mask an error. Excluding `target` protects
the remote build cache: `--delete` never touches an excluded path, so the
incremental `target/` survives between iterations while everything else mirrors.

**3. Compile** on the builder and read the errors:

```bash
ssh -o BatchMode=yes "$HOST_BUILDER" \
  'cd /tmp/hermes-build && SQLX_OFFLINE=true \
     cargo check --workspace --all-targets 2>&1 | tail -40'
```

The first run compiles the whole dependency graph (minutes); every run after is
incremental and fast. If there are `error[...]` lines, fix them in `WT` and go
back to step 2. Do not proceed until this is clean.

**4. Unit-test** the crate once it compiles (offline, no database needed):

```bash
ssh -o BatchMode=yes "$HOST_BUILDER" \
  "cd /tmp/hermes-build && SQLX_OFFLINE=true cargo test -p $CRATE --lib 2>&1 | tail -30"
```

`--lib` runs the crate's unit tests; the `#[sqlx::test]` integration tests need a
live database and are left for CI. A failing assertion means the code is wrong,
not the test — fix and return to step 2.

**5. Ship** only when steps 3 and 4 are both green: commit, `git push origin
<branch>`, and open/update the PR. Now CI runs — and it runs on code you have
already compiled and tested, so it should pass the first time.

## Known API traps (these compile in your head, not in rustc)

These are real mistakes that have shipped from here; the container catches them,
but avoiding them saves a round:

- `sqlx::PgPoolOptions` setters are **unprefixed**: `.max_connections(n)`, not
  `.with_max_connections(n)`.
- `PgPoolOptions::connect_lazy_with(opts)` returns a `Pool` **directly**, not a
  `Result` — do not `?` or `.map_err()` it. `connect_lazy(url)` (parsing a URL)
  does return a `Result`.
- Check a pool's health with `sqlx::query("SELECT 1").execute(&pool)`, not a
  non-existent `pool.connection()`.
- Build a pool from a URL with `PgPoolOptions::new().connect_lazy(url)`; the old
  `PgPool::builder().database_url(...)` was removed in sqlx 0.4.
- A `String` error does not satisfy `?` into `anyhow::Result`; convert it with
  `.map_err(anyhow::Error::msg)`.
- Axum handlers take state as `State(x): State<Arc<AppState>>` (destructured), so
  `x.pool` resolves; `state: State<...>` then `state.pool` does not.

## Housekeeping
- One iterative build at a time — the loop reuses `/tmp/hermes-build`.
- `target/` is owned by your SSH user (no container), so a plain
  `rm -rf /tmp/hermes-build` on the builder reclaims the space when you are done.

## Cap the loop
Give a fix a few attempts, not infinite. If the same error resists three edits,
stop and report the exact compiler output and what you tried — a wrong guess
pushed as "done" is worse than an honest stuck.
