# Checked development admission

This implementation adds opt-in, board-local admission and deterministic preparation to the existing Kanban dispatcher. Installing the code does not enable enforcement: a trusted supervisor must configure the board policy and a check route.

## Supported scope

Automatic preparation handles assigned `ready` or `todo` tasks with a nonproject Git worktree workspace, the configured source repository, and a local-only completion contract. Existing dependency gating still determines whether a prepared task can run. Preparation does not clear manual blocks or create an implementation run. Dispatch dry runs do not prepare tasks.

A board route selects check IDs from a trusted external catalog. Task descriptions cannot supply executable check commands. Preparation validates the catalog, creates or recovers the expected worktree, and enrolls a frozen contract before implementation claims. Preparation has durable bounded retries; implementation attempt accounting is retained separately.

Admission modes are `observe`, `enforce`, and `paused`. With no configured policy, legacy admission remains unchanged. Initial policy configuration preserves existing cards. Transitions out of observe preserve exact in-flight runs rather than granting a general exemption to future runs on the same card.

Check receipts are bound to the task run and source. Protected check artifacts are revalidated before execution and completion, including completion using a cached receipt. Existing dependency, reviewer ownership and PR acceptance checks remain in force.

## Trust boundary

The supervisor selects the catalog and policy. Catalog files and protected check artifacts must be outside the worker workspace; symlink paths and malformed catalog inputs are rejected. Same-identity workers are trusted. These checks are not an operating-system sandbox and do not defend against an unrestricted host administrator.

The checker execution path requires the supported POSIX process-group facilities. Unsupported hosts fail before starting a checker; this document does not claim cross-platform checker execution.

## Not included

- Model-backed preparation or provider calls.
- Automatic routing of project-backed workspaces or PR outcomes.
- Automatic triage/specification or universal enrollment of arbitrary intake.
- A shared attempt budget across replacement cards.
- Tool-level confinement for unrelated direct-chat actions.
- Production activation, an update scheduler, or automatic deployment.

These limitations are not satisfied by passing the deterministic routing tests.

## Updates and portability

Policy data and trusted catalog/check artifacts can be maintained outside the Hermes checkout. The admission, dispatch and completion integration still changes Hermes core. This is a maintained source patch, not a standalone portable plugin.

Keep the patch committed on a dedicated branch or maintained fork. Do not depend on uncommitted edits in the installed runtime: desktop updates can stash them without restoring them, and an ordinary update can switch away from an unmerged feature branch. Hermes documents `updates.parked_branch_strategy: update_in_place` for a deliberately maintained custom branch. That setting is not enabled by this feature, does not eliminate merge conflicts, and does not prove compatibility.

Before updating an installation that uses this patch:

1. Record the installed revision, local changes, effective update channel and active board policies. Keep a private database backup and a rollback copy of the running code.
2. Integrate the intended Hermes update into a separate checkout. Preserve the admission integration and the trusted checks; do not resolve conflicts by dropping guards.
3. Run `scripts/check --base <integration-base>` with the repository-pinned tools, plus the canonical development-admission, preparation, catalog, workspace, contract and affected Kanban lifecycle/dispatch tests.
4. In a disposable board, verify that an unenrolled governed task cannot be claimed, a valid prepared task can run and complete, changed protected checks reject completion, and pause transitions preserve only the intended in-flight run.
5. Review any schema or policy changes. Obtain installation and restart approval separately, then read back the installed revision and effective policy and repeat safe behavior probes through the installed runtime.
6. If integration or verification fails, retain the currently working version. Do not silently disable enforcement to make an update proceed.

A rollback must account for board state as well as source: older code may ignore the added admission tables, so restoring old binaries alone does not establish that enforcement remains active. Keep governed dispatch paused or otherwise stopped under the approved recovery procedure until the selected runtime is verified.

The long-term portability path is supported upstream admission/lifecycle hooks, with workflow policy supplied externally. This implementation does not assert that current plugin hooks provide those guarantees.

Reference: https://hermes-agent.nousresearch.com/docs/getting-started/updating
