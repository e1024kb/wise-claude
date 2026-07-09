---
name: wise-pr-create-auto
description: >-
  Autonomous variant of `/wise-pr-create` — detect the PR state for the
  current branch and create or refresh the PR with NO prompts. The body is
  drafted from the project's PR template; the base branch is chosen
  autonomously (the repo's default branch) instead of asked. Built for
  unattended ticket→PR runs. Invoked as `/wise-pr-create-auto`. Use when
  the user says "create the PR without asking", "auto-create a PR", or
  types `/wise-pr-create-auto`. For the interactive version (base-branch
  picker) use `/wise-pr-create`.
---

# /wise-pr-create-auto — create or refresh a PR, autonomously

> **Shared-file resolution:** `${WISE_PLUGIN_ROOT}` defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/cursor` — where `./install.sh cursor` puts this pack. Export `WISE_PLUGIN_ROOT` only to override.

## Why this skill exists

`/wise-pr-create` is interactive — it asks the user to pick the base
branch. An unattended ticket→PR pipeline can't stop to ask.
`/wise-pr-create-auto` is the decision-free variant: it picks the
repo's default branch as the base and never calls `AskUserQuestion`.
It is also the reusable building block the `ticket-auto` workflow's
PR-create step follows.

## Arguments

This skill takes no arguments. Ignore anything the user types beyond
the skill name.

## Procedure

### 1. Resolve the project + branch

```bash
git rev-parse --show-toplevel
git rev-parse --abbrev-ref HEAD
```

Use the toplevel as `project.path` and the branch as
`current_branch`.

### 2. Follow the shared fragment

Read `${WISE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/ensure-pr-auto.md`
and follow it end to end with `current_branch` and `project.path`.
The fragment detects PR state, drafts the body (via
`references/pr/draft-body.md`), and creates or refreshes the
PR with the repo default branch as base.

### 3. Relay the result

The fragment's final line is `PR-CREATE: number=<n> url=<url>`.
Surface the PR url to the user and stop. Suggest `/wise-pr-watch-auto`
as the next step.

## Guardrails

- Never call `AskUserQuestion` — this skill is the autonomous variant
  by definition.
- Never force-push, amend, rebase, or retarget an existing PR's base.
- Never invoke another wise action skill.
- Refuse to create a PR from a protected branch (`main` / `master` /
  `release*`) that has no PR — stop with a clear message.
