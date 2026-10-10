---
name: wise-code-review
description: >-
  Review local changes or a pull request with wise's 3-lens code review
  team (correctness, security, tests) on the harness, model and effort
  you pick per role: conducts the bundled `code-review` workflow, whose
  pre-flight asks harness, model and effort for each reviewer, the
  curator, the optional verifier and the fixer. Targets: the branch's
  commits over the base, the branch plus uncommitted work, or a PR by
  number or URL. Modes: apply the kept findings, report them to a file,
  or post them as one PR comment review. Never requests a remote review
  bot (Copilot, CodeRabbit) and never pushes. Invoked as
  `/wise-code-review` (bare alias) or `/wise:wise-code-review`
  (canonical). Use when the user says "review my changes", "code review
  this branch", "review PR 123", "review the uncommitted changes", or
  types `/wise-code-review`.
argument-hint: "[working | branch | <pr-number> | <pr-url>] [--base <branch>] [--apply | --report | --comment]"
allowed-tools: Read, Write, AskUserQuestion, TodoWrite, Bash(git:*), Bash(gh:*), Bash(bash:*), Bash(cat:*), Bash(mkdir:*), Bash(test:*)
---

# /wise-code-review - conduct the `code-review` workflow

This skill is a thin conductor: it starts the bundled `code-review`
workflow on the wise engine and follows the run. It has no model
preference of its own - pre-flight asks harness, model and effort per
role - so the
[model fallback](../../references/workflow-host-control.md#model-fallback)
contract applies only to what the engine's pre-flight offers.

At every skill start, identify your main/child role and the current client
and GUI/TUI question tools, then read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions beyond the engine's pre-flight and the run's own gates.

## Why this skill exists

Many repositories run no review bot, and wise never requests one. The
review wise runs instead is its own 3-lens team, the procedure in
[`code-review-pass.md`](../../references/code-review-pass.md): three
read-only reviewers (correctness, security, tests), a curator, an
optional adversarial verifier and a bounded fixer. The workflows run
the same panel on their `review` tuning group (before the first push
and on each new PR head). This skill runs it on demand, on local work
or on any PR of the repository, with the harness, model and effort of
every role chosen at pre-flight.

## Arguments

Read `$ARGUMENTS` and split into whitespace-separated tokens:

- Target, at most one positional:
  - `branch` - the branch's commits over the base (`origin/<base>..HEAD`).
  - `working` - those commits plus uncommitted and untracked changes.
  - `<pr-number>` or a PR URL of this repository - the PR's head. It is
    reviewed in place when that head is checked out here, otherwise
    fetched read-only into a temporary worktree.
- `--base <branch>` - the base to diff against. Default: the PR's base
  for a PR target, else the repo's default branch.
- Mode, at most one: `--apply` (fix the kept findings; one commit, or
  uncommitted edits for the `working` target), `--report` (findings
  file only) or `--comment` (post the findings as one comment review on
  the PR target; needs a PR target).
- Anything else, a second target or mode, or `--base` without a value
  is an error - stop before the run:

  ```
  Unknown argument(s): <the extra tokens>
  Usage: /wise-code-review [working | branch | <pr-number> | <pr-url>] [--base <branch>] [--apply | --report | --comment]
  ```

A given argument is passed as the matching workflow input (`target`,
`base`, `mode`) and its pre-flight question is not asked; an omitted
one is asked. Harness, model and effort are never arguments: pre-flight
asks them.

## Procedure

### 0. Remote check for PR work

When the target is a PR or the mode is `--comment`, read
`${CLAUDE_PLUGIN_ROOT}/references/pr/github-remote.md` and run its
check first. On `none` or `other`, say that PR reviews need a GitHub
`origin` and stop; local targets with `--apply` or `--report` need no
remote check.

### 1. Read the state

```bash
git rev-parse --show-toplevel
git status --porcelain --untracked-files=all
```

When no target was given and the tree has uncommitted changes, say so
before pre-flight: the default `branch` target ignores them; `working`
includes them. Never stash, commit or discard anything here.

### 2. Conduct the `code-review` workflow

Read `${CLAUDE_PLUGIN_ROOT}/skills/wise-workflow-run/SKILL.md` and
follow its §1 (init check), §2 (pre-flight), §3 (start), §4 (wait
loop) and §5 (final report) with:

- `workflow` = `code-review`, `cwd` = the git toplevel.
- `answers` seeded with `input.target`, `input.base` and `input.mode`
  for the arguments given; everything else comes from the staged
  pre-flight, put to the user exactly as that skill prescribes: the
  remaining inputs, the optional `verify` pass, then
  `harness.<group>`, `permissions.<harness>`, `model.<group>` and
  `effort.<group>` for the `correctness`, `security`, `tests`,
  `curate`, `verify` and `fix` groups.
- `context` = `{}`, or the guidance the conversation already holds.

`--comment` publishes the findings on the PR: the user typing it (or
picking `comment` at pre-flight) is the consent. Never pick it for them.

### 3. Relay the result

The `finalize` step prints one line:
`code-review: target=<t> base=<b> changes=<n> findings=<n> kept=<n> refuted=<n> applied=<n> skipped=<n> committed=<yes|no> commented=<yes> file=<path>`.
Summarise the kept findings by severity (critical and warning first),
what was applied or posted, and where the findings file is. For the
`working` target with `--apply`, say the fixes are left uncommitted next
to the user's own changes. A failed lens is reported as an incomplete
review, never as a clean one.

## Guardrails

- Never request or trigger a remote review bot (Copilot code review,
  CodeRabbit): no reviewer request, no `@coderabbitai` comment.
- Never push, never force, never `--no-verify`; the fixer commits at
  most once and never for the `working` target.
- Post on a PR only in `comment` mode, once, as a comment review: never
  an approval or a change request.
- The only questions are the engine's pre-flight and a gate the run
  opens (a failed lens); never answer one yourself.
- Never execute a workflow step here: the engine's provider children run
  the reviewers, curator, verifier and fixer.
- Never invoke another wise action skill; the `wise-workflow-run`
  procedure is read as the conductor routine, not invoked as a skill.
