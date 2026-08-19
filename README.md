# hermes-tools

A verification harness for handing coding work to a local LLM agent and finding
out whether what came back is real.

The agent is [hermes](https://github.com/) running its `swe-updater` skill; the
tools are what stand between its output and a pull request. They exist because
an agent's own report of success is worth very little: across roughly twenty
dispatched issues, its diagnosis was usually right and its choice of mechanism
was wrong about a third of the time, and neither a careful read nor a green
compiler distinguished the two.

## The idea

**A gate the agent can run itself is worth more than a review it cannot.** Given
no verification command, it commits code that has never compiled. Given one, it
reads the failure and fixes it unprompted.

**A gate must exercise the thing that changed.** Every rule in `hypatia-verify`
was written after something shipped green: a Helm template approved by a Rust
test run, a `docker compose --no-build up` with the flag in the wrong position
approved by a checker running its own spelling of the command, an entity with
routes and a repository and no test approved by a suite that never ran.

So `hypatia-verify` picks its gates from the diff rather than from whoever wrote
the dispatch, and the gates read their commands out of the project's own CI
configuration rather than keeping a copy that can drift.

**Draft is the interlock between agent work and human review.** A pull request
stays draft for as long as an agent is still working it, so a branch whose
findings are still open cannot be merged by someone who did not know they were
open. `hypatia-pr-ready` owns that transition and refuses it while the tree is
dirty, the head is unpushed, CI is red or unfinished, or `hypatia-verify` fails
— each of which has promoted a branch that then broke.

**Review, triage and implement are one pipeline, not three jobs.** Review finds,
triage verifies against the tree, implement fixes; each phase hands the next a
file rather than a conclusion. Findings that survive triage become issues, and
the branch that fixes one opens a draft PR saying `Closes #N`, so the issue
burns down when a human merges it and not before.

**A phase does the deciding; the model does the judging.** Every phase is a
script that establishes state, decides whether there is work, and hands the model
exactly that work. Left to the agent, "has anything changed?" produced five
duplicate reviews out of fourteen and three stray clones of the repository
inside its own working directory. Judging code is the only part that needs a
model.

## Layout

    bin/      the gates, the driver that dispatches an issue to the agent, and
              the `pve-*` monitors
    pipeline/ the phases, run by any scheduler: review -> triage -> implement
              overnight, cleanup at midday. Configured entirely from
              `pipeline.env.example`; they name no project and no path.
    skills/   the agent-side procedures the driver invokes
    install/  unit files and the privileged steps a script cannot take

`~/.hermes/scripts` is a symlink to `pipeline/`, so the scheduled jobs run the
reviewed copy. It has to be the directory, not per-file links: `hermes cron`
rejects a script whose path resolves outside that directory. That symlink is
why the scripts resolve their own location with `cd -P`; a logical `cd` walks
`../bin` relative to the link and lands outside the checkout.

The phases take the repository, the gate, the agent and the label vocabulary
from the environment. `bin/` is where this checkout's own answers to those live
-- `hypatia-verify` is one project's gate, not the pipeline's.

## What you have to wire in

`pipeline/pipeline.env.example` is the full list. Four of them are yours to
supply and nothing works without them:

- **a gate**: one command taking a worktree, non-zero when the tree is not fit
  to push. Everything the pipeline claims about correctness comes from it, so
  make it the command your CI runs.
- **an agent** that takes a prompt and may edit a working tree.
- **a triager** and an **implementer** driving that agent for one finding and
  one issue respectively.
- **a small, fast model as the dedup judge**. The reviewer rewords findings it
  carries forward, so the same defect arrives written differently each night.
  Fingerprints and word overlap settle the obvious cases for free; the judge is
  asked only where two sentences share a quarter of their words and may or may
  not mean the same thing. It never sees code, so use the smallest model you
  have, and not the one doing the reviewing.

Leaving the judge unset is supported: matching stays mechanical and files a
duplicate when the wording drifts far enough. Every other omission fails loudly
at startup.

Start at `bin/hypatia-verify`: it decides which gates a branch needs. The rest
of the `hypatia-*` names are the gates it calls, except `hypatia-pr-ready`,
which calls it.

`mm-monitor` watches the MagicMirror from here rather than from the mirror. A
monitor sharing a host with the thing it monitors can report degradation but
never death: the box loses power and simply stops alerting, and silence reads
the same as health. Reachability is the first check because it is the one the
old arrangement could not make.

The `pve-*` pair is unrelated to any of that: `pve-monitor` reports on a Proxmox
cluster's backups and Ceph state, and `pve-notify-setup` configures where its
alerts go. See `install/pve-monitor/README.md`.

## Setup

    cp hosts.env.example ~/.config/hermes-tools/env   # then edit
    cp bin/* ~/bin/                                   # or add bin/ to PATH

The tools need ssh access to a build host and a cluster host; the file above is
where those addresses live. Each tool sources it directly, so cron jobs and
agent subprocesses get the values without an interactive shell.

## What is not generic

The `hypatia-*` names are honest. These gates assume the shape of one project: a
Rust workspace, `migrations/` as the schema source, a Helm chart under
`deploy/charts`, terraform under `deploy/terraform`, and a GitHub Actions
workflow whose job names they parse. Host addresses, repository names and home
directories are all configuration — the project's *shape* is not.

Read them as a worked example of the argument above rather than as something to
drop into a different repository unchanged.
