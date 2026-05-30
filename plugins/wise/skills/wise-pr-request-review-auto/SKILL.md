---
name: wise-pr-request-review-auto
description: >-
  Autonomous variant of `/wise-pr-add-reviewers` — attach Copilot code
  review to the current branch's PR with NO prompts. It does not
  enumerate or ask for human reviewers; CodeRabbit needs no attach (it
  is a GitHub App that auto-reviews every push). Idempotent and
  best-effort — a Copilot-attach failure never blocks. Built for
  unattended ticket→PR runs. Invoked as `/wise-pr-request-review-auto`
  (bare alias) or `/wise:wise-pr-request-review-auto` (canonical). Use
  when the user says "request review without asking", "auto-attach
  Copilot", or types `/wise-pr-request-review-auto`. For the interactive
  version (human-reviewer picker) use `/wise-pr-add-reviewers`.
argument-hint: ""
allowed-tools: Read, Bash(git:*), Bash(gh:*), Bash(cd:*), Bash(bash:*)
---

# /wise-pr-request-review-auto — attach Copilot review, autonomously

## Why this skill exists

`/wise-pr-add-reviewers` asks the user whether to add human reviewers
and which ones. An unattended ticket→PR pipeline can't stop to ask.
`/wise-pr-request-review-auto` attaches Copilot code review and
nothing else — no prompts. It is also the reusable building block the
`ticket-auto` workflow's request-review step follows.

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

Read `${CLAUDE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/request-review-auto.md`
and follow it with `pr_number` and `project.path` (the toplevel). It
attaches Copilot (CLI shorthand, GraphQL fallback), idempotently and
best-effort.

### 3. Relay the result

The fragment's final line is `REVIEW-REQUEST: copilot=<attached|already|unavailable>`.
Report it and stop. Suggest `/wise-pr-watch-auto` as the next step.

## Guardrails

- Never call `AskUserQuestion`.
- Never block on a Copilot-attach failure — best-effort by design.
- Never enumerate or attach human reviewers.
- Never invoke another wise action skill.
