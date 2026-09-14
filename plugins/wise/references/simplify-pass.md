# simplify-pass - the canonical per-commit simplify pass

Single source of truth for **how** the plugin runs its lightweight
per-commit cleanup. Read by:

- `skills/wise-commit/commit-routine.md` §2 - the per-commit pass every
  `/wise-commit` / `/wise-commit-push` and autofix commit runs.
- `workflows/ticket-auto/prompts/implement-plan.md` - once per task,
  scoped to that task's files, before its atomic commit.
- `skills/wise-simplify-auto/SKILL.md` - the standalone building block.

This is the **lightweight tier** of the plugin's two-tier quality model:
it runs as the last step before *every* commit. The heavier code-review
branch gate is the other tier - see [`code-review-pass.md`](./code-review-pass.md).

The pass has **no model preference and no required tool**: it runs on
whatever model the current session or child already has, so the
[model fallback](workflow-host-control.md#model-fallback) contract never
opens a picker for it. It must never block a workflow. The only thing
that stops a caller is a pass that ran and broke the tree (below).

## What the pass does

Behaviour-preserving cleanup of recently modified code: clarity,
consistency, dead-code and redundancy removal, no scope widening. The
full contract is [`simplify-instructions.md`](./simplify-instructions.md).
Every route below applies exactly that contract.

## Pick the route

Decide once per pass, in this order. A route is available only when the
session actually exposes it; do not probe by trial dispatch more than once.

1. **`code-simplifier` agent (Claude Code only).** When the session lists
   the `code-simplifier` agent (bare or plugin-qualified
   `code-simplifier:code-simplifier`) and can dispatch a `Task` / `Agent`
   subagent, dispatch one subagent with that type. Its prompt: simplify
   the recently modified working-tree code in place, preserving
   behaviour, plus the scope line below when the caller scopes the pass.
   The agent ships with the optional `code-simplifier@claude-plugins-official`
   plugin, which wise does not declare as a dependency (CONTRIBUTING §2.3);
   a session without it is a normal environment.
2. **Inline on the current model (every other case).** Codex, Cursor,
   Gemini and Grok children, Claude sessions or children without the
   agent, and any context whose delegation tool rejects the agent type:
   read `simplify-instructions.md` and apply it yourself in the current
   context, on the current model. Do not open the model-fallback picker,
   do not ask the user which route to use, do not try to install the
   plugin, and do not resolve a slash command (`/simplify`) as a
   substitute. A rejected dispatch in route 1 falls through here at once;
   the working tree is untouched because the agent never ran.

Report which route ran (`simplify: code-simplifier agent` or
`simplify: inline on <model>`) together with the pass summary. Surface
the summary to the user; it is mid-flight diagnostics, not a stopping
point - continue to the caller's stage step. Do not wait for input.

A clean working tree has no cleanup work: report `simplify: nothing to
do` and continue.

## Scoping the pass

By default the pass covers all recently modified code. When a caller
needs it confined to a specific file set - one task's files in a
parallel implement wave, so its cleanup does not bleed into a sibling
task's commit - say so explicitly (route 1: in the agent prompt; route 2:
as the file list in step 1 of the instructions):

> "Only refine these files, nothing else: `<space-separated paths>`."

Scoping is an optimisation, not a correctness requirement - a caller
that stages per-file (`git add -- <paths>`) still commits only its own
files even if the pass touched more.

## On failure

Only one failure class exists: **the pass ran and broke the tree**. A
route that never started cannot have touched the working tree, and the
route selection above never fails (route 2 is always available).

If the dispatched agent errors mid-flight, or the inline pass or the
agent leaves the working tree in a state `git status` (or a syntax
check) reports as broken (for example an invalid source file), treat it
as a **hard failure**: do **not** retry, do **not** stage what was
already changed, do **not** invent a recovery.

Surface a one-line `simplify errored: <summary>` and let the **caller**
map it to its own abort contract - the commit routine stops with
`COMMIT: failed reason="simplify errored: ..."`; a standalone caller emits
its own final line. One pass - never re-run it to iterate-to-clean; the
project's pre-commit hook / CI is the final guard.
