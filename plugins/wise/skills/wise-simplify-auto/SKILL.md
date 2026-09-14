---
name: wise-simplify-auto
description: >-
  Autonomously simplify recently-modified code and commit it — dispatches
  the `code-simplifier` agent over the working tree (cleanup only,
  behaviour preserved: clarity, consistency, dead-code/redundancy
  removal), then drafts a Conventional-Commits subject and commits. The
  lightweight per-commit tier of the plugin's two-tier quality model, as a
  standalone building block. No routine prompts, never pushes.
  Invoked as `/wise-simplify-auto` (bare alias) or
  `/wise:wise-simplify-auto` (canonical). Use when the user says "simplify
  and commit", "clean up and commit", "run a simplify pass", or types
  `/wise-simplify-auto`.
argument-hint: "[--on <harness>[:<model>[:<effort>]] | --on ask]"
allowed-tools: Task, Read, Bash(git:*), Bash(bash:*), AskUserQuestion
---

# /wise-simplify-auto — simplify recently-modified code and commit

Before executing, follow [model fallback](../../references/workflow-host-control.md#model-fallback)
for unavailable models or delegation routes, including in autonomous procedures.

At every skill start, identify your main/child role and the current client
and GUI/TUI question tools, then read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered; this rule does not authorize
questions in autonomous or otherwise prompt-free procedures.

## Why this skill exists

The simplify pass (the `code-simplifier` agent) is the plugin's
lightweight per-commit cleanup; it is wired into the commit routine
(`/wise-commit` runs it before staging) and into the implement phase
(per task). This skill exposes it as a **standalone, decision-free
building block**: run the pass, then commit the result — no prompts. The
heavier multi-agent code-review branch gate is the other tier
(the `code-review` workflow: `/wise-workflow-run code-review`).

(Close cousin of `/wise-commit`, which also simplifies-then-commits via
its `SIMPLIFY=yes` default; this skill makes the simplify step the
explicit headline and is the autonomous building block workflows reuse.)

## Invocation

```
/wise-simplify-auto
/wise:wise-simplify-auto              # canonical namespaced form
```

No positionals, no flags. If the argument string contains anything other
than optional whitespace, stop with:

```
Unknown argument(s): <the extra tokens>
Usage: /wise-simplify-auto
```

## Run on another harness (`--on`)

If `$ARGUMENTS` contains `--on <harness>[:<model>[:<effort>]]` (or
`--on ask` / a bare `--on`), do
NOT run the procedure below in this conversation. Strip the `--on`
tokens (everything left is `SKILL_ARGS`), then read
`${CLAUDE_PLUGIN_ROOT}/references/dispatch.md` and follow it with:

- `SKILL_MD` = `${CLAUDE_PLUGIN_ROOT}/skills/wise-simplify-auto/SKILL.md`
- `SKILL_ARGS` = the remaining tokens

`--on ask` (or a bare `--on`) picks harness, model and effort through
one composite `AskUserQuestion` before any child spawns. Unavailable execution
models additionally require the shared model-fallback picker; routine work
remains decision-free.
The reference probes the harness login, validates model and effort
against the engine catalog, and runs the procedure as a headless child
via `engine.sh dispatch --relay`. Follow its run, handle any required gates
in the main harness, and relay the final result as specified by the shared
dispatch reference. This does not add routine prompts to autonomous paths.
Without `--on`, this section does not apply.

## Procedure

### 1. Simplify recently-modified code

Run the simplify pass per
`${CLAUDE_PLUGIN_ROOT}/references/simplify-pass.md` — dispatch the
`code-simplifier` agent (a `Task` subagent) over the working tree's
recently-modified code. Surface its summary verbatim. On a simplify
failure, follow that reference's failure policy and stop with
`SIMPLIFY: failed reason="<one-line>"`. An unavailable model or named agent
uses the reference's GUI/TUI model-fallback gate before any replacement runs.

### 2. Commit the result

Follow `${CLAUDE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md` with
`SIMPLIFY=no PUSH=no` (the pass already ran in §1 — `SIMPLIFY=no` avoids
a redundant second pass; `PUSH=no` because this skill never pushes). The
routine stages, drafts a Conventional-Commits subject, and commits.

### 3. Final line

Relay the routine's final `COMMIT:` line verbatim:

```
COMMIT: ok subject="<subject>" pushed=no
COMMIT: skip reason="nothing to commit"
COMMIT: failed reason="<verbatim error>"
```

## Guardrails

- No routine mid-run questions. The `--on ask` picker and required model-fallback
  gate are explicit exceptions, rendered only by the main harness.
- One simplify pass — never re-dispatch the agent to iterate-to-clean.
- Never `git push` — use `/wise-commit-push` for that.
- All of `commit-routine.md`'s guardrails apply (no `--amend` /
  `--no-verify` / `--force`, no AI-attribution trailer, no retry on
  failure).
- Never invoke another wise action skill.
- Profile-insensitive by design: the session token-budget profile (`/wise-profile`) does not change this skill — one cheap behavior-preserving pass; scaling it would risk the behaviour-preserved guarantee.
