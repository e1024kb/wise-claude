---
name: wise-pr-request-review-auto
description: >-
  Autonomous variant of `/wise-pr-add-reviewers` — request the bot reviews
  on the current branch's PR with NO prompts: attach Copilot code review
  and trigger CodeRabbit (`@coderabbitai review`). It does not enumerate
  or ask for human reviewers. Idempotent and best-effort — a request
  failure never blocks. Built for unattended ticket→PR runs. Invoked as
  `/wise-pr-request-review-auto`. Use when the user says "request review
  without asking", "auto-attach Copilot", or types
  `/wise-pr-request-review-auto`. For the interactive version
  (human-reviewer picker) use `/wise-pr-add-reviewers`.
---

# /wise-pr-request-review-auto — request bot review, autonomously

> **Shared-file resolution:** `${WISE_PLUGIN_ROOT}` defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/pi` — where `./install.sh pi` puts this pack. Export `WISE_PLUGIN_ROOT` only to override. If neither the env var nor the default path exists, use the pack this skill ships in — two levels up from the directory containing this SKILL.md, i.e. `../../` from it (a `pi install`ed pack stays intact).

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

## Procedure

### 1. Verify a PR exists for the current branch

```bash
git rev-parse --show-toplevel
gh pr view --json number,url
```

If `gh pr view` fails (no PR for this branch), stop with a clear
message pointing at `/wise-pr-create-auto`.

### 2. Follow the shared fragment

Read `${WISE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/request-review-auto.md`
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

- Never call `AskUserQuestion`.
- Never block on a Copilot-attach or CodeRabbit-trigger failure —
  best-effort by design.
- Never enumerate or attach human reviewers.
- Never invoke another wise action skill.
