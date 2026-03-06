# Meta-Governance and Confirm Gates

This document defines how the `Meta-Governor` approval policy is enforced in runtime.

## Approval Mechanism

1. Sensitive tool call is intercepted by confirm-gate.
2. Tool returns `CONFIRM_REQUIRED` and emits a `request_id` (format: `cfm-xxxxxxxx`).
3. Owner approves in Telegram: `/approve <request_id>`.
4. Agent retries the blocked action.

Useful command:

- `/approvals` — show currently pending confirmations.

## Risk Categories (Current Runtime Mapping)

1. `mission_policy_change`
2. `external_publication`
3. `credentials_or_access`
4. `budget_risk`

## Tool-Level Rules (Current Version)

Confirm-gate blocks these actions until approval:

1. `repo_commit_push`
2. `repo_write_commit` (with elevated policy sensitivity for mission files)
3. `create_github_issue`
4. `comment_on_issue`
5. `close_github_issue`
6. `toggle_evolution(enabled=true)`
7. `run_shell` when command matches sensitive credential/access patterns

## Scope and Limitations

1. Approvals are one-time: a consumed approval must be re-requested for the next risky action.
2. Approvals expire automatically after TTL.
3. Pattern-based detection for shell commands is conservative and may require periodic tuning.

## Future Hardening

1. Add structured policy manifests for fine-grained per-tool/per-path controls.
2. Add explicit budget threshold approval (USD estimate gate).
3. Add privacy labels for data flows and mandatory approval when crossing trust boundaries.
