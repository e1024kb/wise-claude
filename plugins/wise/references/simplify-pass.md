# simplify-pass — the canonical per-commit simplify pass

Single source of truth for **how** the plugin runs its lightweight
per-commit cleanup. Read by:

- `skills/wise-commit/commit-routine.md` §2 — the per-commit pass every
  `/wise-commit` / `/wise-commit-push` and autofix commit runs.
- `workflows/ticket-auto/prompts/implement-plan.md` — once per task,
  scoped to that task's files, before its atomic commit.
- `skills/wise-simplify-auto/SKILL.md` — the standalone building block.

This is the **lightweight tier** of the plugin's two-tier quality model:
it runs as the last step before *every* commit. The heavier code-review
branch gate is the other tier — see [`code-review-pass.md`](./code-review-pass.md).

## The mechanism — the `code-simplifier` agent (NOT a slash command)

Cleanup is done by Anthropic's **`code-simplifier` agent**, dispatched as
a `Task` subagent. It refines recently-modified code in place — clarity,
consistency, dead-code/redundancy removal — while **preserving
behaviour**, then returns.

> Why an agent and not `Skill({ skill: "simplify" })`: `/simplify` is a
> slash command, and an autonomous workflow / skill cannot type a slash
> command (there is no SlashCommand tool). `code-simplifier` ships as a
> registered **agent**, which IS invocable from a workflow via `Task`.
> A bare `Skill({ skill: "simplify" })` is unreliable — in this
> marketplace `Skill({ skill: "code-review" })` resolves to CodeRabbit's
> skill, so name-based skill resolution is not trusted here.

Dispatch one `Task` subagent:

- `subagent_type`: `code-simplifier` (plugin-qualified
  `code-simplifier:code-simplifier` if the bare name is ambiguous).
- `prompt`: tell it to simplify the recently-modified working-tree code
  and edit in place, preserving behaviour. **Scope it** when the caller
  needs it confined (below).

The agent edits files directly and returns a short summary. Surface that
summary to the user; it is mid-flight diagnostics, not a stopping
point — continue to the caller's stage step. Do not wait for input.

## Scoping the pass

By default the agent focuses on all recently-modified code. When a
caller needs the pass confined to a specific file set — e.g. one task's
files in a parallel implement wave, so its cleanup does not bleed into a
sibling task's commit — say so explicitly in the prompt:

> "Only refine these files, nothing else: `<space-separated paths>`."

The agent honours an explicit scope ("Focuses on recently modified code
*unless instructed otherwise*"). Scoping is an optimisation, not a
correctness requirement — a caller that stages per-file
(`git add -- <paths>`) still commits only its own files even if the pass
touched more.

## On failure

If the `Task` errors, or the pass leaves the working tree in a state
`git status` (or a syntax check) reports as broken (e.g. an invalid
JS/TS source file), treat it as a **hard failure**: do **not** retry, do
**not** stage what was already changed, do **not** invent a recovery.

Surface a one-line `simplify errored: <summary>` and let the **caller**
map it to its own abort contract — the commit routine stops with
`COMMIT: failed reason="simplify errored: …"`; a standalone caller emits
its own final line. One pass — never re-dispatch the agent to
iterate-to-clean; the project's pre-commit hook / CI is the final guard.
