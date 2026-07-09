---
name: wise-code-review-auto
description: >-
  Autonomously code-review the current branch at HIGH depth and apply the
  fixes — dispatches a panel of parallel reviewer subagents over
  `origin/<base>..HEAD`, applies the concrete correctness / security /
  clear-quality findings (skipping judgement-call refactors), and commits
  them. The heavyweight branch gate of the plugin's two-tier quality model
  — meant to run once over a whole branch *before it is pushed / a PR is
  opened*. NO prompts. Invoked as `/wise-code-review-auto`. Use when the
  user says "code review the branch", "review my changes before pushing",
  "run a code review pass", or types `/wise-code-review-auto`.
---

## Harness adaptation note

This skill was authored for Claude Code and adapted for OpenAI Codex CLI. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn a subagent with the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if this harness supports subagents; otherwise adopt that role yourself and perform the steps sequentially.


# /wise-code-review-auto — review a branch at high effort, autonomously

## Why this skill exists

The simplify pass is the lightweight tier — it runs before every commit.
This is the **heavyweight tier**: one **high-depth multi-agent review**
over the whole branch (a panel of parallel reviewer subagents), applied
and committed, meant to run **once before the branch reaches GitHub**. It
is the decision-free building block the `ticket-auto` workflow's review
step follows (between implement and push), and it is usable standalone
before you `git push` / open a PR.

## Arguments

Read `$ARGUMENTS`. The first whitespace-separated token, if present, is
the **base branch** to diff against. When absent, the base is detected
(repo default branch). Any further tokens are an error:

```
Unknown argument(s): <the extra tokens>
Usage: /wise-code-review-auto [<base-branch>]
```

## Procedure

### 1. Resolve the worktree + base

```bash
git rev-parse --show-toplevel
```

Use the toplevel as `worktree`. Resolve `base` from `$ARGUMENTS` (or let
the fragment detect the default branch).

### 2. Follow the shared fragment

Read `${WISE_PLUGIN_ROOT}/workflows/ticket-auto/prompts/review-branch-auto.md`
and follow it end to end with `worktree` and `base`. The fragment
reviews `origin/<base>..HEAD` at high effort per
`${WISE_PLUGIN_ROOT}/references/code-review-pass.md`, applies the
bounded findings, and commits them with `SIMPLIFY=no PUSH=no`.

### 3. Relay the result

The fragment's final line is
`REVIEW-AUTO: applied=<n> skipped=<m> committed=<yes|no>` (or
`REVIEW-AUTO: aborted reason="…"`). Summarise for the user — what was
applied, what was skipped and why, whether a fix commit landed — and
remind them nothing was pushed (push / PR is a separate step).

## Guardrails

- Never call `AskUserQuestion` — the only stop is the argument error above.
- One review pass, one fix-apply, one commit; never iterate-to-clean.
- Bounded apply — guard the change, do not redesign it.
- Never `git push` — that is the caller's / operator's step.
- Never invoke another wise action skill.
