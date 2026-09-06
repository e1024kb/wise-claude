# dispatch — run a skill's procedure on any harness, as a subagent

Shared routine for the skills that accept `--on`: instead of executing
their procedure in this conversation, hand it to a headless child of
any workflow harness (`claude`, `codex`, `grok`, `gemini`) at a model
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
- `INTERACTIVE` — `yes` when the calling skill may use
  `AskUserQuestion` (the non-`-auto` skills), else `no`.

## 1. Parse `--on`

Grammar, anywhere in `$ARGUMENTS`:

```
--on <harness>[:<model>[:<effort>]]
--on ask
--on
```

- `<harness>` is one of `claude`, `codex`, `grok`, `gemini`.
- `<model>` is a catalog id (or Claude alias); omitted: the harness's
  first catalog entry.
- `<effort>` is `low|medium|high|xhigh|max`; omitted: no effort flag.
- `--on ask` or a bare `--on`: pick interactively (§3). When
  `INTERACTIVE=no`, that is an error — stop with
  `--on needs <harness>[:<model>[:<effort>]] in an -auto skill`.
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

`installed` false or `login` not `ok`: stop and print the row's
`login_cmd` — never fall back to a different harness silently.

## 3. Resolve model and effort

Catalog:

```bash
bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh models <harness>
```

- Spec gave model / effort: validate against the catalog rows
  (`dispatch` re-validates and errors on an effort the model does not
  list; surface that error as is).
- `--on ask` (interactive skills only): one composite
  `AskUserQuestion` — harness (the `auth --json` rows with
  `login: ok`, `claude` first), then model (that harness's catalog,
  first entry first, label + description), then effort (the chosen
  model's `efforts`, or skip the question when the list is empty).

## 4. Compose the child prompt

Write one file to the scratchpad (or `/tmp` when no scratchpad):

```markdown
Execute the following skill procedure end to end, autonomously.

- Skill file: <SKILL_MD>            # read it first, follow it exactly
- Arguments ($ARGUMENTS): <SKILL_ARGS or "(none)">
- Working tree: <git toplevel of the caller's cwd>
- You are a headless child: never prompt, never wait for a human;
  where the skill offers an interactive path, take its autonomous one.
- The skill's guardrails apply unchanged (no force-push, no amend,
  no AI attribution, refusal rules).

End your final message with the skill's documented final line so the
caller can parse the outcome.
```

## 5. Dispatch

```bash
bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh dispatch \
  --harness <harness> [--model <id>] [--effort <e>] \
  --mode full-access --cwd <git toplevel> \
  --timeout-s 3600 --prompt-file <the file>
```

Long procedures (a PR watch loop) go through the Bash tool in the
background; relay `started on <harness> <model>[ <effort>]` and poll
the task result. `--mode full-access` because the dispatched skills
edit, commit and push; a read-only caller may pass `--mode auto`
instead when its skill never writes.

## 6. Relay

`dispatch` prints one JSON object: `{ok, exit, harness, model, effort,
verdict, text, usage, error?, warnings}`.

- `ok: true` — print the skill's final line found in `text` (its
  documented `PR-CREATE:` / `COMMIT:` / verdict line), one line
  `on <harness> <model>[ <effort>] — <input+output tokens> tokens`,
  and any warnings.
- `ok: false` — print `exit` and `error`, plus the tail of `text` when
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
