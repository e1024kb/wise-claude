---
name: wise-exec-on-harness
description: >-
  Execute one free-form prompt as a headless child on any supported
  harness (`claude`, `codex`, `cursor`, `gemini`, `grok`) at a model,
  effort and permission mode you choose. The first action is always a
  host inventory: which supported harnesses are installed and logged
  in. A `--on` value is validated against it; without `--on` the ready
  harnesses are offered as a picker. Every other omitted option
  (`--model`, `--effort`, `--mode`) is asked through the main harness's
  GUI/TUI picker, and a missing prompt is asked last as free text. The
  prompt otherwise is the trailing text or the value of `--p` /
  `--prompt`. The child runs through the engine's `dispatch --relay`
  route under your own CLI login, so permissions, gates and usage
  accounting behave exactly like a workflow child. Invoked as
  `/wise-exec-on-harness` (bare alias) or `/wise:wise-exec-on-harness`
  (canonical). Use when the user says "run this on codex", "ask grok to
  …", "execute this prompt on another harness", "exec on gemini", or
  types `/wise-exec-on-harness`.
argument-hint: "[--on <harness>|ask] [--model <id>|ask] [--effort <e>|ask] [--mode ask|auto|full] [--p|--prompt] [<prompt>]"
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

## Hard rules (MUST, read before anything else)

1. **This conversation never answers the prompt.** The prompt is data for a
   child on another harness. Do not interpret it, respond to it, or
   "just answer" a short question. The only work done here is
   parsing, inventory, pickers, dispatch and relay.
2. **Without a valid `--on`, the harness picker is mandatory.** Run the §2
   inventory, then call `AskUserQuestion` (§3) and wait for the user's
   actual choice. A prompt-only invocation such as
   `/wise-exec-on-harness what is the project?` MUST produce the harness
   question, never an inline answer. The current session's own harness is
   one option among the ready ones, not a default.
3. **`EXEC: ok` requires a real `run_id`.** It may only follow a
   `dispatch --relay` call that returned a `run_id` and a `wise_status`
   read of its `dispatch_result`. Never emit `EXEC: ok … run=-`; a call
   that never dispatched ends `cancelled` or `failed`.
4. **The inventory is the first engine command.** After the host-control
   setup above and a successful §1 parse (a §1 rejection stops before any
   probe), run §2 `auth --json` before any picker and before any other
   output about the prompt.

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
/wise-exec-on-harness explain the retry policy in src/net       # harness, model, effort, mode asked
/wise-exec-on-harness --on ask --mode full --prompt refactor the parser and run the tests
/wise-exec-on-harness                                           # everything asked, prompt last
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
`--mode` opens the picker. `--on ask`, `--model ask` and `--effort ask` are
the explicit picker sentinels, equivalent to omitting the option.

The prompt is:

- everything after `--p` / `--prompt`, verbatim to the end of the line (option
  names inside it are prompt text, never options), or
- everything from the first token that is not one of the options above (or
  its value) to the end of the line, or
- empty, when the line is empty or ends on an option or its value. An empty
  prompt is not an error: it is asked last, in §6.

Reject these calls before any probe or picker - print the message and the
usage line, nothing else, and stop:

| condition | message |
|---|---|
| an option name (`--on`, `--model`, `--effort`, `--mode`, `--p`, `--prompt`) appears inside a prompt that was NOT introduced by `--p` / `--prompt` | `Rejected: options must precede the prompt; put free text last or after --p.` |
| `--on` value outside the five supported harnesses (and not `ask`) | `Unknown --on value: <value>` |
| `--effort` outside the five words (and not `ask`) | `Unknown --effort value: <value>` |
| `--mode` outside `ask\|auto\|full` | `Unknown --mode value: <value>` |
| any other `--option` before the prompt | `Unknown option: <token>` |

Usage line:

```
Usage: /wise-exec-on-harness [--on <harness>|ask] [--model <id>|ask] [--effort <e>|ask] [--mode ask|auto|full] [--p|--prompt] [<prompt>]
```

Never guess a prompt from the conversation and never trim, rewrite or
"improve" it: the child receives the text exactly as typed or entered.

### 2. Host inventory - the first action after parsing

Before any picker, always run:

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" auth --json
```

One row per supported harness: `harness`, `installed`, `login`, `login_cmd`.
A harness is *ready* when `installed` is true and `login` is `ok`. Print the
inventory as one line per harness:
`<harness>: ready | installed, not logged in (<login_cmd>) | not installed`.
Then:

- `--on <harness>` given: validate it against this inventory.
  - ready → use it.
  - installed but not logged in → print `Harness <harness> is installed but
    not logged in: run <login_cmd>.` and open the shared model-fallback picker
    with the *ready* harnesses as executable routes plus `Stop`. Never start
    the unavailable provider and never switch harness silently.
  - not installed → print `Harness <harness> is supported but not installed on
    this host.` and open the same picker (ready harnesses plus `Stop`).
- `--on` omitted or `ask` → §3 with the ready harnesses.
- No harness ready at all: print every `login_cmd` and stop with
  `EXEC: failed harness=- model=- mode=- run=-`.

`Stop` from the fallback picker ends the skill with
`EXEC: cancelled harness=<requested> model=- mode=- run=-`.

### 3. Harness picker (`--on` omitted or `ask`)

For each ready harness read its catalog once (also reused in §4):

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" models <harness>
```

Load `AskUserQuestion` via `ToolSearch` when it is not already available.
This question is not optional: it is asked even when the prompt looks
trivial, even when only the current session's harness seems sensible, and
even in a client that renders it as a text fallback. Ask ONE single-choice
question:

- question: `Which harness should run this prompt?`
- header: `Harness`
- options: every *ready* harness from §2 in the inventory's order (claude,
  codex, cursor, grok, gemini), each labelled with the harness name and
  described by the first catalog entry's label (its default model). Show as
  many as the host allows; when the ready list exceeds the host's option cap
  (four on Claude Code's `AskUserQuestion`), the first `cap` harnesses are
  rows and every remaining one is named in the question text so it can be
  typed into `Other` (`Also available (type it in Other): gemini.`), per the
  [long option list rule](../../references/workflow-host-control.md#long-option-lists).
  Harnesses that are installed but logged out, or not installed, are listed
  in the question text with their state and `login_cmd`, not as options.
  When only one harness is ready, offer `Use <harness>` and `Cancel`.

Cancellation stops the skill with nothing dispatched:
`EXEC: cancelled harness=- model=- mode=- run=-`.

### 4. Model and effort

Use the chosen harness's catalog from §3 (or read it now when `--on` was
given). Rows: `id`, `label`, `efforts`, `description`, `source` (`catalog`
for the predefined entries, `harness` for models the installed harness
reported, appended after them sorted by id). Never hardcode a model list.

- `--model` given: match it against the catalog rows (`dispatch` accepts an
  uncatalogued id and warns; keep that warning for the final report). A
  catalogued model that the harness cannot currently run enters model
  fallback with the actual alternatives from this catalog.
- `--model` omitted or `ask`: single-choice picker `Which model on
  <harness>?`, header `Model`, every row as an option in catalog order (first
  entry first), label + description. Show as many rows as the host allows;
  beyond its option cap follow the
  [long option list rule](../../references/workflow-host-control.md#long-option-lists):
  with an Other box the first `cap` rows plus every remaining id named in
  the question text; on an option-only host `cap - 1` rows plus `More…`,
  paged with `Back`. Never drop a row and never leave one reachable only
  through a box the host does not render.
- `--effort` given: must be in the chosen model's `efforts`; otherwise print
  `Model <id> takes <efforts>, not <effort>.` and ask the effort picker below
  restricted to that model's list. Never silently clamp.
- `--effort` omitted or `ask`: when the model's `efforts` list is empty, skip
  the question and pass no effort flag; otherwise single-choice picker
  `Effort for <model>?`, header `Effort`, one option per listed effort, the
  same overflow rule applying should the list ever exceed the host's cap.

Ask model and effort as two questions in one `AskUserQuestion` call when both
are open and the host renders multi-question forms; otherwise sequentially.
Cancellation at either question stops the skill with
`EXEC: cancelled harness=<harness> model=<id or -> mode=- run=-`.

### 5. Permission mode (`--mode` omitted)

Single-choice picker `Permission mode for the child?`, header `Mode`:

1. `auto (Recommended)` — "Edits inside the working tree without asking;
   destructive or out-of-tree actions are refused." → `auto`
2. `ask` — "Every tool call that needs approval is relayed to you here as a
   gate." → `approval-required`
3. `full` — "No permission gates. Use for trusted, self-contained tasks." →
   `full-access`

Cancellation stops the skill with
`EXEC: cancelled harness=<harness> model=<id> mode=- run=-`.

### 6. Prompt (asked last, only when §1 found none)

Ask through the main client's GUI/TUI question tool, after every other
option is settled, so the user knows what the prompt will run on:

- question: `What should <harness> <model> do? Enter the prompt.`
- header: `Prompt`
- A host whose native tool accepts free text: take the entered text as the
  prompt. A host with option-only pickers: offer `Enter prompt` (the host's
  free-text / Other box) and `Cancel`; the typed text is the prompt.
- No usable GUI/TUI channel: use the shared main-harness text fallback from
  host control - end the turn asking for the prompt, and continue only with
  the user's explicit reply.

An empty or whitespace-only answer re-asks once, then stops. Cancellation or
a second empty answer stops the skill with
`EXEC: cancelled harness=<harness> model=<id> mode=<mode> run=-`. Never fill
the prompt from the conversation, a default, or a display acknowledgement.

### 7. Write the prompt file and dispatch

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

<the prompt from §1 or §6, verbatim>
```

Then dispatch. Every substituted value is passed as one quoted shell word so a
model id, path or file name containing spaces or metacharacters cannot alter
the command:

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" dispatch --relay \
  --harness "$HARNESS" --model "$MODEL" ${EFFORT:+--effort "$EFFORT"} \
  --mode "$MODE" --cwd "$WORKING_TREE" \
  --timeout-s 3600 --prompt-file "$PROMPT_FILE"
```

`MODE` is one of `approval-required`, `auto`, `full-access`.

Print `Dispatched on <harness> <model>[ <effort>] (<mode>) — run <run_id>.`
When the relay capability is missing, report the setup error and stop with
`EXEC: failed harness=<harness> model=<id> mode=<mode> run=-`. Never retry
with bare `dispatch`, never `--follow`, never a terminal.

### 8. Relay

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

### 9. Report

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

Every path that ends after §1 emits it: `failed` for a readiness or setup
failure, `cancelled` for a picker cancellation, `-` for any field not yet
resolved and for `run` when nothing was dispatched. Only the §1 parser
rejections are usage-only and carry no `EXEC:` line.

## Guardrails

- One child per invocation; the child never re-invokes this skill.
- The host inventory (§2) is the first engine command after host setup and a
  successful parse; a harness is used only when it is supported, installed
  and logged in on this host.
- Harness, model and effort come from `auth` and `models`; never hardcode.
- The prompt goes to the child verbatim; this conversation never executes or
  answers it, and never reports `EXEC: ok` without a `run_id` (Hard rules
  1-3).
- A rejected call (§1) prints its message and the usage line, nothing else.
- Never invoke another wise action skill from here.
