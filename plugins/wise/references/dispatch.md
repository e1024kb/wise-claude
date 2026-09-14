# dispatch — run a skill's procedure on any harness, as a subagent

Before model-backed work, follow [model fallback](workflow-host-control.md#model-fallback).
Unavailable models or delegation routes require a main-harness GUI/TUI selection,
including in autonomous paths. Preserve the procedure's other gates and limits.

Before collecting user input, follow the [question lifecycle](workflow-host-control.md#keep-asynchronous-questions-open).
A display acknowledgement is not an answer; keep asynchronous prompts open.
This does not add prompts to autonomous paths.

Shared routine for the skills that accept `--on`: instead of executing
their procedure in this conversation, hand it to a headless child of
any workflow harness (`claude`, `codex`, `cursor`, `gemini`, `grok`) at a model
and effort the user picks. The child runs under the user's own CLI
login through the engine's adapters — the same path workflow `agent`
steps take — so permissions, clean env and usage accounting behave
exactly like an engine child.

## Inputs the caller sets

- `SKILL_MD` — absolute path to the SKILL.md whose procedure the child
  runs. An interactive skill dispatches its `-auto` twin's SKILL.md
  (a headless child cannot answer `AskUserQuestion`); the caller names
  which.
- `SKILL_ARGS` — the caller's `$ARGUMENTS` with every `--on` token
  removed; passed to the child verbatim as the skill's arguments.
- `TIMEOUT_S` — **optional** child timeout in seconds (default 3600).
  A skill whose procedure has its own wall-clock budget (the PR watch
  loop's `--minutes`) sets this above that budget, so the dispatcher
  never kills a run that is about to emit its verdict.

## 1. Parse `--on`

Grammar, anywhere in `$ARGUMENTS`:

```
--on <harness>[:<model>[:<effort>]]
--on ask
--on
```

- `<harness>` is one of `claude`, `codex`, `cursor`, `gemini`, `grok`.
- `<model>` is a catalog id (or Claude alias); omitted: the harness's
  first catalog entry.
- `<effort>` is `low|medium|high|xhigh|max`; omitted: no effort flag.
- `--on ask` or a bare `--on`: pick interactively (§3). This holds in
  the `-auto` skills too — the pick happens at invocation time, before
  any child spawns, so it does not break their no-prompts contract;
  only the dispatched child itself stays prompt-free.
- A malformed spec (unknown harness, bad effort word) stops before any
  child spawns:

  ```
  Unknown --on value: <value>
  Usage: --on <harness>[:<model>[:<effort>]] | --on ask
  ```

No `--on` in `$ARGUMENTS`: this reference does not apply; the caller
runs its own procedure locally as always.

## 2. Probe the harness

```bash
bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh auth <harness> --json
```

`installed` false or `login` not `ok`: show the readiness failure and its
`login_cmd`. Before starting any child, offer the shared model-fallback picker
for an executable current-harness route. Do not change harnesses silently or
start the unavailable provider. Permission denials still stop rather than
being routed around.

## 3. Resolve model and effort

Catalog:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh models <harness>
```

- Spec gave model / effort: validate against the catalog rows
  (`dispatch` re-validates and errors on an effort the model does not
  list). Malformed input stops. A valid but unavailable requested model/effort
  enters model fallback, with actual alternatives from the intended route.
- `--on ask` (any caller, `-auto` skills included): one composite
  `AskUserQuestion` — harness (the `auth --json` rows with
  `login: ok`, `claude` first), then model (that harness's catalog,
  first entry first, label + description), then effort (the chosen
  model's `efforts`, or skip the question when the list is empty).

## 4. Compose the child prompt

If model fallback selected a native child in the current harness, use its real
spawn schema with the prompt below and the approved model, not the unavailable
engine CLI route in §5. If an inline route was explicitly approved and preserves
the task's guarantees, execute the same procedure here with the stripped
arguments. Do not recursively invoke the skill or select another provider.

Write one file to the scratchpad (or `/tmp` when no scratchpad):

```markdown
Execute the following skill procedure end to end, autonomously.

- Skill file: <SKILL_MD>            # read it first, follow it exactly
- Arguments ($ARGUMENTS): <SKILL_ARGS or "(none)">
- Working tree: <git toplevel of the caller's cwd>
- Follow every applicable CLAUDE.md and AGENTS.md, regardless of harness;
  the engine supplies their contents as the repository instruction contract.
- Pass that complete contract recursively to every subagent you create.
- You are a headless child: never open UI or prompt the user directly.
  Use autonomous paths for routine choices. When the skill requires user
  input or consent, call `wise_ask` and wait for the main harness's answer.
  Pass this relay rule recursively to your own children. Never assume consent.
- The skill's guardrails apply unchanged (no force-push, no amend,
  no AI attribution, refusal rules).

End your final message with the skill's documented final line so the
caller can parse the outcome.
```

## 5. Dispatch

```bash
bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh dispatch --relay \
  --harness <harness> [--model <id>] [--effort <e>] \
  --mode full-access --cwd <git toplevel> \
  --timeout-s <TIMEOUT_S, default 3600> --prompt-file <the file>
```

`--relay` starts one daemon-managed child and immediately returns its `run_id`.
Follow that run with `wise_wait`, or the launcher's non-interactive `wait`
command when MCP is unavailable. Relay progress and handle every returned
gate in the main harness using the shared native-first question lifecycle.
Submit only the actual user answer through `wise_answer` or `answer`.
An unanswered gate remains pending, including across a text-fallback turn.
Explicit cancellation calls `wise_cancel` or `cancel`; never answer on the
user's behalf, launch a terminal, or use `--follow` stdin prompting here.
Without the relay capability, stop before starting a child and report the
setup error. Do not retry with bare `dispatch`.

`--mode full-access` because the dispatched skills
edit, commit and push; a read-only caller may pass `--mode auto`
instead when its skill never writes.

## 6. Relay

When `wise_wait` reports `done`, fetch `wise_status {run_id}` (or `status`).
Its `dispatch_result` contains the original provider result: `exit`, `text`,
`usage`, and optional `error` / `warnings`. Keep launch warnings too.
Run status is authoritative: a cancelled or failed run never counts as success,
even if the provider returned partial successful output during shutdown.

- Completed run and `exit: ok` - print the skill's final line found in `text` (its
  documented `PR-CREATE:` / `COMMIT:` / verdict line), one line
  `on <harness> <model>[ <effort>] — <input+output tokens> tokens`,
  and any warnings.
- Otherwise - print run status, `exit` and `error` when available, plus the tail of `text` when
  it helps; suggest rerunning locally (no `--on`) or on another
  harness. Never auto-retry on a different harness.

## Guardrails

- One child per invocation; the child never gets `--on` in its
  arguments (no recursive dispatch).
- Never lower the calling skill's own refusal rules; the child prompt
  carries them via the SKILL.md read.
- Model / effort come from the engine catalog (`models` command) —
  never hardcode a model list in a skill.
- A failed child is reported, not silently retried elsewhere.
