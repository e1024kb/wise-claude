---
name: wise-pr-request-review-auto
description: >-
  Autonomous variant of `/wise-pr-add-reviewers` — request the bot
  reviews on the current branch's PR with NO prompts: attach Copilot
  code review and trigger CodeRabbit (`@coderabbitai review`). It does
  not enumerate or ask for human reviewers. Idempotent and best-effort —
  a request failure never blocks. Built for unattended ticket→PR runs.
  Invoked as `/wise-pr-request-review-auto` (bare alias) or
  `/wise:wise-pr-request-review-auto` (canonical). Use when the user says
  "request review without asking", "auto-attach Copilot", or types
  `/wise-pr-request-review-auto`. For the interactive version
  (human-reviewer picker) use `/wise-pr-add-reviewers`.
argument-hint: "[--on <harness>[:<model>[:<effort>]] | --on ask]"
allowed-tools: Read, Bash(git:*), Bash(gh:*), Bash(cd:*), Bash(bash:*), AskUserQuestion
---

# /wise-pr-request-review-auto — request bot review, autonomously

Before asking any user question, read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions in autonomous or otherwise prompt-free procedures.

## Why this skill exists

`/wise-pr-add-reviewers` asks the user whether to add human reviewers
and which ones. An unattended ticket→PR pipeline can't stop to ask.
`/wise-pr-request-review-auto` requests the bot reviews — attaches
Copilot and triggers CodeRabbit — and nothing else, no prompts. It is
also the reusable building block the `ticket-auto` workflow's
request-review step follows.

## Arguments

This skill takes no arguments. Ignore anything the user types beyond
the skill name.

## Run on another harness (`--on`)

If `$ARGUMENTS` contains `--on <harness>[:<model>[:<effort>]]` (or
`--on ask` / a bare `--on`), do
NOT run the procedure below in this conversation. Strip the `--on`
tokens (everything left is `SKILL_ARGS`), then read
`${CLAUDE_PLUGIN_ROOT}/references/dispatch.md` and follow it with:

- `SKILL_MD` = `${CLAUDE_PLUGIN_ROOT}/skills/wise-pr-request-review-auto/SKILL.md`
- `SKILL_ARGS` = the remaining tokens

`--on ask` (or a bare `--on`) picks harness, model and effort through
one composite `AskUserQuestion` before any child spawns — the ONE
sanctioned prompt in this skill: it happens at invocation time, so the
dispatched run itself stays decision-free.
The reference probes the harness login, validates model and effort
against the engine catalog, and runs the procedure as a headless child
via `engine.sh dispatch`; you only relay its result. Without `--on`,
this section does not apply.

## Procedure

### 1. Verify a PR exists for the current branch

```bash
git rev-parse --show-toplevel
gh pr view --json number,url
```

If `gh pr view` fails (no PR for this branch), stop with a clear
message pointing at `/wise-pr-create-auto`.

### 2. Follow the shared fragment

Read `${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/request-review-auto.md`
and follow it with `pr_number` and `project.path` (the toplevel). It
attaches Copilot (CLI shorthand, GraphQL fallback) and triggers
CodeRabbit (`@coderabbitai review`), idempotently and best-effort.

### 3. Relay the result

The fragment's final line is
`REVIEW-REQUEST: copilot=<attached|already|unavailable> coderabbit=<present|triggered|error>`.
Report it and stop. Suggest `/wise-pr-watch-auto` as the next step — it
confirms each bot actually reviewed and handles CodeRabbit's
out-of-credits / rate-limit states.

## Guardrails

- Never call `AskUserQuestion` mid-run — the one exception is the
  `--on ask` harness/model/effort pick, before dispatch.
- Never block on a Copilot-attach or CodeRabbit-trigger failure —
  best-effort by design.
- Never enumerate or attach human reviewers.
- Never invoke another wise action skill.
