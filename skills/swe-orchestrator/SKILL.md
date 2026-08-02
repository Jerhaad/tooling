---
name: swe-orchestrator
description: Top-level autonomous manager skill that orchestrates the daily multi-agent loop, handling git sandboxing, reviewer instantiation, and sequential updater execution.
version: 1.0.0
metadata:
  hermes:
    tags: [software-engineering, orchestration, multi-agent, automation, workflow]
    category: software-engineering
---

# Software Engineering Orchestrator (`swe-orchestrator`)

The project manager and core coordinator for the autonomous development lifecycle. This skill isolates the repository environment, schedules execution phases, ensures branch tracking invariants, and transitions states programmatically between discovery (`swe-reviewer`) and execution (`swe-updater`).

## When to Use
Use this skill inside automated headless environments (such as a system `cron` daemon or a CI/CD workflow pipeline) to invoke the entire multi-agent engineering lifecycle as a single unified token execution window.

## Procedure

### Phase 1: Environment Invariant Check & Synchronization
Before spawning sub-agents, prepare the local workspace to prevent branch conflicts or race conditions on the local host machine:
1. Identify the current system calendar date in `YYYY-MM-DD` formatting.
2. Flush the local cache and fetch tracking objects: `git fetch origin`.
3. Check out the clean primary trunk and update it: `git checkout main && git pull origin main`.

### Phase 2: Execution Matrix Management
Execute the full multi-agent development loop by systematically invoking system runtime environments. You may leverage an optimized internal runner script (e.g., `orchestrator.py`) to manage the precise tool calls:

* **Execution Sequence Pattern:**
  1. Spawn `swe-reviewer` on branch `reviewer-<date>` to analyze code and register `REVIEW_NOTES.md`.
  2. Spawn `swe-updater` on branch `updater-<date>` to pull review notes, run local validation matrices, and execute patches.

1. **Invoke Step 1 (The Reviewer Loop):**
   * Dynamically build a local git branch named `reviewer-<YYYY-MM-DD>`.
   * Clear old identical local references if they exist: `git branch -D reviewer-<YYYY-MM-DD>`.
   * Check out the clean review branch.
   * Run the reviewer daemon headless: 
     `hermes run --skill ~/.hermes/skills/software-engineering/swe-reviewer.md --non-interactive`
   * Confirm the tracking artifact `REVIEW_NOTES.md` is successfully compiled, committed, and synced upstream.

2. **Invoke Step 2 (The Updater Loop):**
   * Return the workspace context cleanly to the main baseline: `git checkout main`.
   * Dynamically build a clean execution branch named `updater-<YYYY-MM-DD>`.
   * Run the updater daemon headless to systematically consume the review notes and apply the patches within the 12-hour timebox:
     `hermes run --skill ~/.hermes/skills/software-engineering/swe-updater.md --non-interactive`

### Phase 3: Session Serialization & Handover Reporting
1. Audit the final git tracking layout. Ensure no artifacts leak or cross-contaminate the upstream production staging branches.
2. Print out a comprehensive, top-level execution matrix summarizing:
   * Verification Status of both daily branches.
   * Total atomic code adjustments generated.
   * Handover telemetry highlighting items requiring human review or further evaluation.

## Pitfalls
* **State Leakage:** Failing to return to `main` before checking out the `updater-<date>` branch will cause code validation anomalies. Keep the branch parents strictly separated.
* **Orphaned Sessions:** If an unhandled termination occurs inside a sub-agent execution, the orchestrator must capture the exception, cleanly log the state to an output tracker, and safely clean up the repository lockfiles.

## Verification
Confirm the entire autonomous orchestration lifecycle has completed successfully:
1. Verify that `git branch -r` shows both `origin/reviewer-<date>` and `origin/updater-<date>` matching the exact date signature.
2. Run a post-execution sanity check showing that your working directory is clean and returned back to the tracking target state.

