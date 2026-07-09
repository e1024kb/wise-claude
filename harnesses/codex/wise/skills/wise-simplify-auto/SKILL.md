---
name: wise-simplify-auto
description: >-
  Autonomously simplify recently-modified code and commit it — dispatches
  the `code-simplifier` agent over the working tree (cleanup only,
  behaviour preserved: clarity, consistency, dead-code/redundancy
  removal), then drafts a Conventional-Commits subject and commits. The
  lightweight per-commit tier of the plugin's two-tier quality model, as a
  standalone decision-free building block. NO prompts, never pushes.
  Invoked as `/wise-simplify-auto`. Use when the user says "simplify and
  commit", "clean up and commit", "run a simplify pass", or types
  `/wise-simplify-auto`.
---

## Harness adaptation note

This skill was authored for Claude Code and adapted for OpenAI Codex CLI. Where the steps below reference Claude-specific tools, substitute:

- **Task / subagent dispatch (`subagent_type: wise:<role>`)** — spawn a subagent with the role card at `${WISE_PLUGIN_ROOT}/agents/<role>.md` if this harness supports subagents; otherwise adopt that role yourself and perform the steps sequentially.
- **Shared files (`${WISE_PLUGIN_ROOT}`)** — defaults to `${WISE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/wise}/harness/codex`, where `./install.sh codex` puts this pack; export `WISE_PLUGIN_ROOT` only to override.


# /wise-simplify-auto — simplify recently-modified code and commit

## Why this skill exists

The simplify pass (the `code-simplifier` agent) is the plugin's
lightweight per-commit cleanup; it is wired into the commit routine
(`/wise-commit` runs it before staging) and into the implement phase
(per task). This skill exposes it as a **standalone, decision-free
building block**: run the pass, then commit the result — no prompts. The
heavier multi-agent code-review branch gate is the other tier
(`/wise-code-review-auto`).

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

## Procedure

### 1. Simplify recently-modified code

Run the simplify pass per
`${WISE_PLUGIN_ROOT}/references/simplify-pass.md` — dispatch the
`code-simplifier` agent (a `Task` subagent) over the working tree's
recently-modified code. Surface its summary verbatim. On a simplify
failure, follow that reference's failure policy and stop with
`SIMPLIFY: failed reason="<one-line>"`.

### 2. Commit the result

Follow `${WISE_PLUGIN_ROOT}/skills/wise-commit/commit-routine.md` with
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

- Never call `AskUserQuestion` — the only stop is the argument error above.
- One simplify pass — never re-dispatch the agent to iterate-to-clean.
- Never `git push` — use `/wise-commit-push` for that.
- All of `commit-routine.md`'s guardrails apply (no `--amend` /
  `--no-verify` / `--force`, no AI-attribution trailer, no retry on
  failure).
- Never invoke another wise action skill.
