# Workflow host control

Use the installed Wise launcher for engine shell commands on Claude Code, Codex,
Cursor and Grok. MCP child environment variables do not define variables in the
conductor's shell. Never send a literal `${CLAUDE_PLUGIN_ROOT}` as a generic host
command or infer an installation by choosing the newest cached version.

## Resolve the loaded installation

The host's loaded skill location identifies this installation: from
`<plugin>/skills/<skill>/SKILL.md`, use `<plugin>`. If the host supplies only a
resource URI, resolve it through that host's skill metadata first. Do not guess a
filesystem path from the plugin name. `WISE_PLUGIN_ROOT` is an explicit override
for an installation the user selected.

Init prepares dependencies from that root and runs:

```bash
bash "/absolute/loaded/plugin/engine/engine.sh" setup-host \
  --host codex --plugin-root "/absolute/loaded/plugin"
```

Replace `codex` with the actual conductor host: `claude`, `codex`, `cursor`, or
`grok`. Review the returned Wise entry and changed file paths, then run the same
command with `--apply` when setup is authorized. The preview never prints other
servers, credentials or settings. `--config /absolute/config` selects a custom
profile; pass it when the host's custom location is not already selected by
`CODEX_HOME` or `CLAUDE_CONFIG_DIR`. Retain it for init registry checks.

The stable launcher is `$HOME/.local/share/wise/bin/wise-engine`. Registration
stores that resolved absolute path, an argument array containing
`--wise-host <host> mcp`, and the selected interpreter as `WISE_PYTHON` plus the conductor as `WISE_HOST`.
Cursor and Codex can filter the parent environment, so interpreter selection must
be present in registration rather than assumed to propagate. The launcher itself and its registry are outside the
plugin cache. It accepts spaces and Unicode in paths without a shell command
string. Existing unrelated JSON/JSONC members and TOML settings/comments remain
in place. A transaction path is returned on changes; `host-rollback <transaction>`
restores exact previous bytes only if no file changed afterward.

Each host has its own installation binding in
`$HOME/.local/share/wise/installations.json`. At the start of each workflow
skill, resolve the loaded skill root and run its engine's `refresh-host --host
<host> --plugin-root <loaded-root>`. This refreshes an existing owned registration
after a plugin upgrade or relocation, including versioned Codex, Cursor and Grok
cache paths. No path edits are required. A successful old registration alone does
not prove it selects the loaded installation. Refresh preserves unrelated host
settings and refuses a missing or externally modified Wise entry; use explicit
init setup to review and repair those cases. Recheck init after refresh and
reconnect session tools if the selected installation changed. Symlink bindings follow the
selected symlink, without scanning other caches. Claude may instead pass
`--source '{"path":"/absolute/installed_plugins.json","key":"wise@marketplace","scope":"user"}'`
with the exact active plugin key and scope. A project binding also requires its
exact `project_path`. Ambiguous or removed entries fail with a repair message.
Do not apply the Claude metadata format to another host's registry.

## Use the stable command

Before using the stable command, refresh from this skill's loaded installation:

```bash
bash "$WISE_PLUGIN_ROOT/engine/engine.sh" refresh-host --host "$WISE_HOST" --plugin-root "$WISE_PLUGIN_ROOT"
```

This uses the existing setup authorization and only changes a previously owned
registration. First setup still goes through init. If a custom configuration was
selected, retain that same profile rather than writing the default host config.

Set the actual host explicitly on every skill shell invocation, for example:

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host codex install-root
"$HOME/.local/share/wise/bin/wise-engine" --wise-host codex definition-roots
"$HOME/.local/share/wise/bin/wise-engine" --wise-host codex preflight workflow-name
```

`install-root` returns the selected install for reading bundled references and
scripts. `WISE_HOST` provides the same selection when an explicit argument is
inconvenient; `--wise-host` takes precedence. Do not rely on the last initialized
host when several hosts use different installations. If the launcher is missing
or its root no longer exists, use the loaded init skill to repair it; that repair
does not require working MCP.

## Prove the conductor route

Keep these checks separate: Python and dependencies, launch registration, native
host connection, available tools, daemon response, interactive choices, and
selected provider authentication. `host-doctor --host <host>` verifies local
registration and launcher state only; it always reports `host_verified: false`.
A direct engine subprocess or fixture handshake does not prove the active host
session's tools or picker. Wise uses managed manual registration as its sole transport. The bundled
`.mcp.json` is empty, so loading the plugin does not start a duplicate server.
For upgrades from an older session that still has a plugin-owned server, reload
or restart the host and verify that only the managed Wise server remains.

- Claude Code: `claude mcp get wise-engine` checks a manual registration. Plugin
  servers have plugin-qualified names. `/reload-plugins` refreshes plugin MCP in
  interactive sessions; noninteractive sessions need a new session. Bare
  `elicitation: {}` is legacy form support and the Python SDK accepts it.
- Codex: `codex mcp get wise-engine --json` reads config, not connectivity. The
  native app-server can initialize, list MCP tools and call a selected tool with
  no model turn. Direct app-server tool calls may decline elicitation; do not
  count that control route as proof of a picker. Reconnect the host session after
  registration changes, or use its supported MCP reload control and verify again.
- Cursor: the tested CLI requires native server approval as well as registration.
  During authorized init, run `cursor-agent mcp enable wise-engine`, then
  `cursor-agent mcp list-tools wise-engine` to start and list the server's tools.
  An unapproved-server error requires this native enable step, not another config
  rewrite. The tested CLI advertises form elicitation. Restart its session after
  config repair and verify the actual conversation's tool availability. Use the
  same canonical workspace path for native approval and execution; symlink aliases
  can select different approval records. Cursor Ask mode also requires an explicit
  tool permission for MCP calls, even for status queries. A narrowly scoped
  `Mcp(wise-engine:wise_status)` allow rule permits status without granting other
  workflow actions; use native approval for the actions the user actually requests.
- Grok: `grok mcp doctor wise-engine --json` checks launch, handshake and tools.
  Its tested doctor connection does not advertise elicitation. `/mcps` opens
  native MCP controls; `r` refreshes config in the TUI. A healthy doctor result
  does not prove the conversational picker.

Prefer supported MCP form elicitation. Otherwise render the engine's current
questionary with the host's native structured picker. If that host cannot provide
one, run an explicit interactive CLI flow: show each engine-provided question,
wait for the user's answer, and submit only those answers with `--answers` or
`answer`. Preserve stage ordering and report pending questions; never submit UI
defaults, synthesize approval, or invent harness/model choices. On cancellation,
stop collecting answers and preserve the engine's resumable state. Provider
installation and login are checked only for providers required by the selected
workflow steps.

### Keep asynchronous questions open

This section applies to every Wise skill and shared routine that collects user
input, including setup, discovery, document wizards, confirmations, dispatch
selection, preflight, and workflow gates. Read this section alone for non-workflow
skills; it does not require engine setup. Existing no-prompt rules and prior user
decisions still apply. Do not add a question merely to use this procedure.

Treat `AskUserQuestion` in skill prose as the host's supported question mechanism,
not a guarantee of a blocking tool. Use only tools available in the current mode.
Choose the control from the question's meaning and the live tool schema, not from
the provider name. Claude, Codex, Cursor, and Grok sessions can expose different
controls depending on their client and mode. Never invent a `multiSelect` field
when the available tool does not declare it.

| Question | Preferred control | When that control is unavailable |
|---|---|---|
| Exactly one known value (`kind: choice`, a mode, model, approval or confirmation) | Single-choice picker with every allowed option | Paginate options if the host limits their count; text only when no usable picker exists |
| Any allowed subset (`kind: multi`, optional stages or several reviewers) | Native multi-select picker or MCP array-enum form | Use the single-choice sequence below; do not require typed lists |
| Open-ended content (`kind: text`, a ticket URL, path, explanation or comments) | Free-text input | Ask the same open question in chat if no input tool exists |
| Known choices plus an explicitly allowed custom answer (`allow_text`) | Picker with the known options and the host's custom-answer affordance | Keep the known choices clickable and collect custom text only when selected |

Supply choices in the tool's **options field**, not merely in the question title.
For example, a host exposing `request_user_input_async` with string options gets
`{"questions":[{"title":"Review the plan before setup?","options":["Accept automatically","Ask for review"]}]}`.
Map those labels back to the declared values (`auto` / `ask`) after the response.
Omitting `options` creates a text-only prompt even if the title lists choices.
For Claude-style tools use the declared option objects and multi-select flag;
for other tools use their actual schema. Preserve labels, descriptions, stable
values, and cardinality. Defaults may be highlighted, never silently submitted.

Apply this dispatch order for every question, without provider-specific exceptions:

1. Read the engine's `kind`, `options`, `default`, and constraints. They determine
   the answer shape: `choice` is one declared value, `multi` is an array of declared
   values, and `text` is a string. Do not reclassify a declared choice as text.
   A gate with options is a choice; `allow_text` adds a custom-answer route, not
   permission to hide its options. Skip already answered or locked questions.
2. Inspect the tools actually available in this session and mode. Prefer supported
   MCP form elicitation; otherwise use a native picker. In a tool with an `options`
   property, that property must be populated for a choice. A title containing a
   list of alternatives does not satisfy this requirement.
3. Use native multi-select only if the tool explicitly declares it. For example,
   the Codex asynchronous tool with only `title` and string `options` is
   single-choice. Do not send it an invented multi-select argument, and do not
   offer the original items as one single-choice question for a multi-select task.
   Instead use the sequence below. If a Claude or another host's tool explicitly
   supports multi-select, use that control directly.
4. Respect the host's question-count and option-count limits. Split batches and
   paginate without dropping choices. When only one real option exists but the
   picker requires two, offer `Use <label>` and `Cancel`; do not invent a second
   engine value. A host-provided Skip/Other control is not automatically valid.
5. Wait for an actual response using the lifecycle below. Map display labels to
   values, validate against the original question, then submit. Invalid free text,
   display acknowledgements, navigation, unanswered items, and cancellation must
   never reach the engine as completed answers.

For workflow inputs, use the engine's questionary rather than extracting choices
from prose or interpreting regexes in the conductor. The engine recognizes a
strict literal enum such as `^(auto|ask)$` without extraction as a choice; general
validation patterns and inputs requiring extraction remain text. Optional enum
inputs include a declared `Leave unset` choice; preserve it like any other option.
For non-engine skill questions, known alternatives such as
Install/Skip, Delete/Keep, or a list of reviewers use the corresponding picker.

For multi-select on a host with only single-choice pickers:

1. Offer explicit shortcuts when valid: `Use default selection (N items)` (show
   which items), `Choose individually`, and `None` only if an empty set is allowed.
   Omit unavailable shortcuts rather than inventing a default. Respect the tool's
   option-count limits.
2. On `Choose individually`, ask `Include <item label>?` with clickable `Include`
   and `Exclude` for each item. Keep the selected values locally; batch independent
   item questions only when the tool supports it. Preserve any completed choices
   when continuing an interrupted selection.
3. Submit the resulting array under the original question ID only after every
   item has an explicit answer and the selection satisfies the original constraints.
   For an invalid selection, explain the constraint and reopen the item choices.
   Shortcut labels, `Include`, `Exclude`, and navigation labels are UI controls,
   never engine values. Cancellation or an unanswered item leaves the original
   question unanswered. Never treat cancellation as an empty selection.

For a long single-choice list, use pages with clickable navigation within the
host's option-count limit. Navigation does not answer the underlying question.
When a skill asks a bounded contextual question without an engine questionary,
provide concise choices for the known alternatives; use free text only for content
that cannot reasonably be enumerated. Do not invent an exhaustive option set for
an open-ended question. Respect `allow_text: false` even if the host always exposes
an Other box: reject an out-of-set reply rather than passing it to the engine.

Example: research stages are a `multi` question with four declared option values.
On a host with multi-select, show four checkboxes. On the current Codex-style
single-choice async tool, first show clickable `Use default selection (all four)`,
`Choose individually`, and `None` if allowed. Choosing individually then shows
four Include/Exclude decisions and produces the same selected-value array as
the checkboxes. Never ask the user to type stage IDs or comma-separated names
when the single-choice picker is available.

Check the native picker's response contract before collecting answers. A blocking
picker returns the user's answer. An asynchronous picker (for example,
`request_user_input_async`) may return only `accepted: true`: that acknowledges
display, not a selection, cancellation, or approval.

After opening an asynchronous question, keep the asking agent's turn active until
an actual user response arrives. Use the host's interruptible wait or yield facility
in intervals of at most 60 seconds, handling incoming messages between waits.
Do not send a final response such as "awaiting your selection" while the GUI
question is pending: hosts may dismiss it when the turn ends. Do not open duplicate
questions while the original is active, interpret elapsed time as an answer, or
advance preflight on an acknowledgement. A repeated workflow invocation without
a choice is not an answer.
On an explicit cancellation, stop the pending operation without treating it as
permission to proceed. A user message that replaces the task or cancels it ends the
old question; a status request does not answer it. Follow host instructions for
progress updates while waiting, without replacing the pending question.

If a prior turn ended and its unanswered picker is no longer active, re-present
that question when the user resumes the operation. Keep already supplied answers
and do not merely report that the vanished question is still awaiting a selection.

If the host cannot keep an asynchronous question alive and offers no blocking
picker, use a plain-text question and wait for the user's next message. Missing
native multi-select alone is not a reason to fall back to text. Apply this
same lifecycle to preflight selections and workflow approval/ask gates.

Pass this section's instructions to delegated interactive wizards. A headless
workflow child must use the engine's blocking `wise_ask` channel when its workflow
permits questions, not a host GUI tool; return `needs-human` when that channel
cannot obtain an answer. Autonomous children keep their existing no-prompt policy.
The conductor collects a real answer before calling `wise_answer`; a gate ID or
question-display acknowledgement is never an answer.

Official host references: [Claude MCP](https://code.claude.com/docs/en/mcp),
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[Cursor MCP](https://cursor.com/docs/mcp),
[Grok MCP](https://docs.x.ai/build/features/mcp-servers).

## CLI control when session tools are unavailable

Use the same host-selected launcher above. For preflight, call
`preflight <workflow> --answers '<answers-so-far JSON>'`; present the returned
questions one at a time and call again after each explicit answer. Continue until
`questions` is empty. Only then run
`run <workflow> --cwd <project> --answers '<collected JSON>' --context '<context JSON>'`.
Passing a JSON argument requires proper shell quoting; use a shell argument array
when available. Never interpolate user text as shell code.

| MCP operation | CLI command after the launcher and host arguments |
|---|---|
| `wise_status` | `status [run_id]` |
| `wise_preflight` | `preflight <workflow> --answers <JSON>` |
| `wise_run` | `run <workflow> --cwd <project> --answers <JSON> --context <JSON>` |
| `wise_wait` | `wait <run_id> --after <seq> --timeout-ms <milliseconds>` |
| `wise_answer` | `answer <run_id> <gate_id> <value>` |
| `wise_cancel` | `cancel <run_id> --reason <text>` |
| `wise_nudge` | `nudge <run_id> <step> <message>` |
| `wise_resume` | `resume <run_id>` |

`resume` resets interrupted running steps, not already-failed steps. It can
therefore return to failed status when no runnable pending work remains. Review
completed side effects before starting a new run to retry a failed step.

For a terminal controlled by the user, `run <workflow> --interactive --follow
--text` collects staged answers and gates from stdin. Closing stdin preserves an
unanswered gate. In a conductor session, use separate commands so the user can
answer through that session's supported input control. Preserve the run id and
last event sequence when switching between MCP and CLI.

## Diagnose the failing stage

1. Missing launcher, unresolved installation or literal plugin-root placeholder:
   resolve the loaded skill root and run init registration. Reloading cannot repair
   a missing file or a literal placeholder.
2. Startup exit or dependency failure: run the loaded installation's `engine.sh
   version`; inspect stderr and Python availability. Retry managed bootstrap only
   after correcting its reported failure. Do not install system Python packages.
3. Registration valid but native handshake fails: use that host's native MCP check
   above and inspect its server startup error. A successful direct CLI command only
   proves the engine can start outside that host.
4. Handshake succeeds but tools are missing: verify the selected installation and
   eight parent tool names. Wrong inventory indicates stale or duplicate registration.
5. Native checks succeed but this session has no tools: reconnect or reload that
   host session, or use the explicit CLI route. Record session tools as unavailable
   until an actual call succeeds.
6. Tools exist but `DAEMON_UNAVAILABLE` or `DAEMON_VERSION_MISMATCH` returns: inspect
   `daemon status` and its log path. Never delete a live socket/lock or kill active
   children to force an upgrade. Let active work finish or follow an explicit cancel
   request; replace an idle old daemon through the supported daemon control.
7. Engine responds but `AUTH_REQUIRED` returns: show the selected child provider's
   login command. Conductor registration and optional connector skips remain valid.
