---
name: wise-exec-on-harness
description: >-
  Execute one free-form prompt as a headless child on any supported
  harness (`claude`, `codex`, `cursor`, `gemini`, `grok`) at a model,
  effort and permission mode you choose. Every option omitted from the
  invocation is asked through the main harness's GUI/TUI picker
  (`--on`, `--model`, `--effort`, `--mode`); the prompt itself is the
  trailing free text or the value of `--p` / `--prompt`. The child runs
  through the engine's `dispatch --relay` route under your own CLI
  login, so permissions, gates and usage accounting behave exactly like
  a workflow child. Invoked as `/wise-exec-on-harness` (bare alias) or
  `/wise:wise-exec-on-harness` (canonical). Use when the user says "run
  this on codex", "ask grok to …", "execute this prompt on another
  harness", "exec on gemini", or types `/wise-exec-on-harness`.
argument-hint: "[--on <harness>|ask] [--model <id>|ask] [--effort <e>|ask] [--mode ask|auto|full] [--p|--prompt] <prompt>"
allowed-tools: Read, Write, ToolSearch, AskUserQuestion, Bash(bash:*), Bash(git:*), Bash(mktemp:*), Bash(cat:*)
---

# /wise-exec-on-harness — run one prompt on any harness

Before executing, follow [model fallback](../../references/workflow-host-control.md#model-fallback)
for unavailable models or delegation routes, including in autonomous procedures.

At every skill start, identify your main/child role and the current client
and GUI/TUI question tools, then read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered.

First read [host control](../../references/workflow-host-control.md). Resolve
the loaded installation, set `WISE_HOST` to this conductor and
`WISE_PLUGIN_ROOT` to that installation, and use its managed launcher for
shell commands. Conductor host and child harness are independent.

## Why this skill exists

The `--on` routine in [`dispatch.md`](../../references/dispatch.md) runs a
*skill procedure* on another harness. This skill is the free-form twin: it
takes any prompt, resolves harness / model / effort / permission mode (asking
for whatever was not given), and runs that prompt once as a headless engine
child. Nothing about the prompt is interpreted here; the child does the work.

## Invocation

```
/wise-exec-on-harness --on codex --model gpt-6-astra --effort high --mode auto summarize the failing tests
/wise-exec-on-harness --on grok --p explain the retry policy in src/net
/wise-exec-on-harness explain the retry policy in src/net       # every option asked
/wise-exec-on-harness --on ask --mode full --prompt refactor the parser and run the tests
```

## Procedure

### 1. Parse `$ARGUMENTS`

Tokenize on whitespace. Read leading option pairs left to right:

| option | values | omitted / `ask` |
|---|---|---|
| `--on <harness>` | `claude`, `codex`, `cursor`, `gemini`, `grok` | picker (§3) |
| `--model <id>` | a catalog id of the chosen harness (Claude aliases accepted) | picker (§4) |
| `--effort <e>` | `low`, `medium`, `high`, `xhigh`, `max` | picker (§4) |
| `--mode <m>` | `ask` → `approval-required`, `auto` → `auto`, `full` → `full-access` | picker (§5) |
| `--p <text…>` / `--prompt <text…>` | the rest of the line, verbatim | — |

Also accept the `--key=value` spelling. `--mode ask` selects the
approval-required permission mode, it does not mean "ask me" - only omitting
`--mode` opens the picker.

The prompt is:

- everything after `--p` / `--prompt`, when present, or
- everything from the first token that is not one of the options above (or
  its value) to the end of the line.

Reject these calls before any probe or picker - print the message and stop:

| condition | message |
|---|---|
| `$ARGUMENTS` empty or whitespace-only | `Usage: /wise-exec-on-harness [--on <harness>\|ask] [--model <id>\|ask] [--effort <e>\|ask] [--mode ask\|auto\|full] [--p\|--prompt] <prompt>` |
| no prompt text remains (line ends on an option or its value, `--p` with nothing after it) | `Rejected: the last argument must be the prompt.` + usage |
| an option name (`--on`, `--model`, `--effort`, `--mode`, `--p`, `--prompt`) appears inside the prompt text | `Rejected: options must precede the prompt; put free text last or after --p.` + usage |
| unknown `--on` harness | `Unknown --on value: <value>` + usage |
| effort outside the five words | `Unknown --effort value: <value>` + usage |
| mode outside `ask\|auto\|full` | `Unknown --mode value: <value>` + usage |
| any other `--option` before the prompt | `Unknown option: <token>` + usage |

Never guess a prompt from the conversation and never trim, rewrite or
"improve" it: the child receives the text exactly as typed.

### 2. Probe readiness

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" auth --json
```

One row per harness: `installed`, `login`, `login_cmd`. A harness is
*ready* when `installed` is true and `login` is `ok`.

- `--on <harness>` given and not ready: print the row's readiness failure and
  its `login_cmd`, then enter the shared model-fallback picker for an
  executable route (another ready harness, or `Stop`). Never start the
  unavailable provider and never switch harness silently.
- No harness ready at all: print every `login_cmd` and stop.

### 3. Harness picker (`--on` omitted or `ask`)

Load `AskUserQuestion` via `ToolSearch` when it is not already available.
Ask ONE single-choice question:

- question: `Which harness should run this prompt?`
- header: `Harness`
- options: every *ready* harness from §2, `claude` first, each labelled with
  the harness name and described by its default catalog model. Harnesses
  that are installed but logged out are listed in the question text with
  their `login_cmd`, not as options. When only one harness is ready, offer
  `Use <harness>` and `Cancel`.

Cancellation stops the skill with nothing dispatched.

### 4. Model and effort

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" models <harness>
```

Rows: `id`, `label`, `efforts`, `description`. Never hardcode a model list.

- `--model` given: match it against the catalog rows (`dispatch` accepts an
  uncatalogued id and warns; keep that warning for the final report). A
  catalogued model that the harness cannot currently run enters model
  fallback with the actual alternatives from this catalog.
- `--model` omitted or `ask`: single-choice picker `Which model on
  <harness>?`, header `Model`, options in catalog order (first entry first),
  label + description; paginate past the host's option limit.
- `--effort` given: must be in the chosen model's `efforts`; otherwise print
  `Model <id> takes <efforts>, not <effort>.` and ask the effort picker below
  restricted to that model's list. Never silently clamp.
- `--effort` omitted or `ask`: when the model's `efforts` list is empty, skip
  the question and pass no effort flag; otherwise single-choice picker
  `Effort for <model>?`, header `Effort`, one option per listed effort.

Ask model and effort as two questions in one `AskUserQuestion` call when both
are open and the host renders multi-question forms; otherwise sequentially.

### 5. Permission mode (`--mode` omitted)

Single-choice picker `Permission mode for the child?`, header `Mode`:

1. `auto (Recommended)` — "Edits inside the working tree without asking;
   destructive or out-of-tree actions are refused." → `auto`
2. `ask` — "Every tool call that needs approval is relayed to you here as a
   gate." → `approval-required`
3. `full` — "No permission gates. Use for trusted, self-contained tasks." →
   `full-access`

### 6. Write the prompt file and dispatch

Write one file (scratchpad when available, else `mktemp`) containing:

```markdown
You are a headless child started by /wise-exec-on-harness.

- Working tree: <git toplevel of the current cwd, or the cwd when not a repo>
- Follow every applicable CLAUDE.md and AGENTS.md; the engine supplies their
  contents as the repository instruction contract. Pass it to any subagent you create.
- Never open UI or prompt the user directly. When you need an answer or consent,
  call `wise_ask` and wait for the main harness's reply. Never assume consent.
- Do exactly the task below; do not widen it.

## Task

<the prompt from §1, verbatim>
```

Then:

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" dispatch --relay \
  --harness <harness> --model <id> [--effort <e>] \
  --mode <approval-required|auto|full-access> --cwd <working tree> \
  --timeout-s 3600 --prompt-file <the file>
```

Print `Dispatched on <harness> <model>[ <effort>] (<mode>) — run <run_id>.`
When the relay capability is missing, report the setup error and stop. Never
retry with bare `dispatch`, never `--follow`, never a terminal.

### 7. Relay

Follow the run with `wise_wait {run_id, after}` (or the launcher's
non-interactive `wait` when MCP is unavailable), `after` = last `seq` seen.
Print one line per event as the conductor does (`step.progress` lines as is,
never raw child output). Every `gate` goes to the user through the shared
native-first question lifecycle; submit only the user's actual answer via
`wise_answer`. An unanswered gate stays pending. Explicit cancellation calls
`wise_cancel`; never answer on the user's behalf.

When `done`, read `wise_status {run_id}` → `dispatch_result` (`exit`, `text`,
`usage`, optional `error` / `warnings`). Run status is authoritative: a
cancelled or failed run is never a success, even with partial `text`.

### 8. Report

- Completed run, `exit: ok`: print the child's `text` in full (that is the
  deliverable), then one line
  `on <harness> <model>[ <effort>] <mode> — <input+output tokens> tokens`
  and any warnings.
- Otherwise: run status, `exit`, `error` when present, the tail of `text`
  when it helps. Suggest rerunning with another `--on`; never auto-retry
  elsewhere.

Your response's FINAL line MUST be exactly, on its own line:

```
EXEC: <ok|failed|cancelled> harness=<harness> model=<id> mode=<mode> run=<run_id>
```

Use `run=-` when the call was rejected before dispatch.

## Guardrails

- One child per invocation; the child never re-invokes this skill.
- Harness, model and effort come from `auth` and `models`; never hardcode.
- The prompt goes to the child verbatim; this conversation never executes it.
- A rejected call (§1) prints its message and the usage line, nothing else.
- Never invoke another wise action skill from here.
