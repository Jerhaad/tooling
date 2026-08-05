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

## Layout

    bin/      the gates, the driver that dispatches an issue to the agent, and
              the `pve-*` monitors
    skills/   the agent-side procedures the driver invokes
    install/  unit files and the privileged steps a script cannot take

Start at `bin/hypatia-verify`: it decides which gates a branch needs. The rest
of the `hypatia-*` names are the gates it calls.

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
