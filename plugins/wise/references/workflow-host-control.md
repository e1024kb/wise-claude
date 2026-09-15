# Workflow host control

Use the installed Wise launcher for engine shell commands from Claude Code,
Claude Desktop, Codex, Cursor, Grok, and T3 Code. T3 Code uses the underlying
provider harness's Wise host identity. MCP child environment variables do not
define variables in the conductor's shell. Never send a literal
`${CLAUDE_PLUGIN_ROOT}` as a generic host command or infer an installation by
choosing the newest cached version.

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

Replace `codex` with the underlying provider harness: `claude`, `codex`,
`cursor`, or `grok`. In T3 Code, use the provider selected for the main
conversation. Review the returned Wise entry and changed file paths, then run
the same command with `--apply` when setup is authorized. The preview never prints other
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
  count that control route as proof of a picker. Codex Desktop can also advertise
  elicitation in a model-driven turn but immediately return `decline` without
  rendering a form. Prefer the Desktop main harness's native inline picker when
  it is available. Treat an observed unrendered automatic decline as an unusable
  transport route, not as a user decision. Codex CLI currently drops MCP
  array-enum fields, so Wise encodes each multi-select option as a required
  boolean form field. Reconnect the host session after
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

Prefer a structured picker owned by the main harness when one is available in
the current client and mode. Otherwise use supported MCP form elicitation. If
neither route is usable, use the main-harness text fallback below. Do not open a terminal
from a GUI or CLI skill. For an explicitly requested standalone CLI test only:

```bash
"$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" \
  preflight <workflow> --cwd <absolute-cwd> --interactive
```

The command collects every staged answer and returns them without starting the
run. Do not dump the raw questionary into chat. Preserve stage ordering and report pending
questions; never submit UI defaults, synthesize approval, or invent harness/model
choices. On cancellation, stop collecting answers and preserve the engine's
resumable state. Provider
installation and login are checked only for providers required by the selected
workflow steps.

## Model fallback

Read this contract at every skill start and before dispatching a model-backed
task, including shared procedures and children. It governs unavailable model,
effort, named-agent and native delegation routes. A procedure that declares no
model preference and its own inline route runs inline on the current model when
its named agent or its subagent tool is absent; it never enters this picker.
The simplify pass (`simplify-pass.md`), the implement phase
(`implement-plan.md`), the code-review pass (`code-review-pass.md`) and the PR
watcher's `dispatch_mode=task` handlers (`watch-pipelines-auto.md`) are such
procedures: they pin no model, a Claude-native subagent inherits the current
model, and a child on any harness without a subagent tool runs the same work
sequentially inline on the model its tuning group selected at pre-flight.
Model names in skill bodies
are preferences, not proof that a client can run them. Keep portable skill
frontmatter free of provider-specific model pins so the body can load and ask.
This contract takes precedence over a routine's automatic inline/skip/abort
behavior for capability unavailability and its routine no-prompt policy. It
does not override higher-priority host restrictions or authorize other prompts.

### Establish executable choices

1. Identify the main client, underlying provider, current model (or an opaque
   `Current session model` when its ID is not exposed), and the intended route:
   current conversation, native child, or engine provider child. These are
   separate capabilities. T3 Code's provider catalog is not its GUI identity.
2. Inspect live session metadata and the actual tool schemas/model listing for
   that route. Native child options must be accepted by the native spawn tool;
   include inherited/current-model execution only when that route supports it.
   For engine children use the selected provider's `models <harness>` catalog
   and readiness check. An installed CLI or engine catalog does not establish
   which models the main client or its native children can select. Never invent
   IDs, cross-map vendor aliases, or claim to switch the current conversation
   merely by mentioning a model in a prompt.
3. Use the requested route when it is supported. A clear unavailable-model or
   unavailable-agent result before execution enters the picker below. Do not
   treat malformed arguments, policy denial, failed tests, timeouts or failures
   after work began as permission to try another model. Inspect completed side
   effects and preserve the caller's failure/recovery contract in those cases.

### Ask in the main harness

Before substituting, the main harness presents a native GUI/TUI single-choice
picker with populated options, or a permitted MCP form actually rendered in
that same client. Follow [the question lifecycle](#keep-asynchronous-questions-open),
including asynchronous waits, pagination and real-answer validation. For this
model-change gate, do not use plain-text fallback, a system dialog or a new
terminal. No usable GUI/TUI channel means `model-fallback-ui-unavailable` and
the affected operation remains unstarted. Cancellation means
`model-fallback-declined`; never select the highlighted default automatically.

Show the unavailable requested model/role, reason, task scope, and execution
mode. Offer `Use current session model` first when executable, other verified
models available through the current harness's intended route, and `Stop`.
Label each option with the actual model ID when known and whether it runs as a
fresh child or inline. Paginate large catalogs without hiding models. If only
inheritance is supported, offer current-model inheritance and Stop, not guessed
alternatives. Collect supported effort separately if the requested effort is
unavailable; never silently clamp a user-selected effort or budget constraint.

Children send the same question, verified choices, route and scope through
`wise_ask` or their parent. Only the main harness renders it and returns the
actual answer. If the child's relay cannot reach the main GUI/TUI, stop with
`model-fallback-ui-unavailable`. Pass this contract and the accepted selection
recursively. A display acknowledgement is not approval.

### Execute only the approved substitution

- Record requested and selected model/effort, execution mode and covered task
  set in the invocation context, not global host settings or installed files.
  A picker may explicitly cover equivalent remaining handlers in this invocation;
  reuse that selection only for the displayed scope and unchanged capabilities.
  A new invocation or a different role requirement needs a new choice.
  A skill with no model preference inherits normally without a fallback prompt.
  If the preferred model cannot be selected for the current conversation but
  is available only for children, do not silently turn the whole skill into a
  child task; offer the current conversation model or a clearly labelled child
  route whose permissions and guarantees match the skill.
- Prefer a fresh native child on the selected model. When a named role is not
  registered, read its available role card and pass its full task contract to a
  generic native child with matching tools and permissions. Preserve all
  applicable CLAUDE.md/AGENTS.md, read-only restrictions, output format, context
  isolation, review lenses, timeout and supervision requirements. A model choice
  cannot supply missing tools, credentials or a missing specialist procedure.
- If no child route exists, offer inline current-model execution only for a
  procedure whose guarantees can be preserved inline, or one explicitly allowing
  reduced-depth inline review. Disclose that reduction in the option. Never
  simulate an independent panel or supervised workers in one context. Otherwise
  report `model-fallback-capability-unavailable` without doing that task.
- Resume only the unstarted task using the selected route. A rejected alternate
  does not authorize cycling through the remaining models. Report the failure
  and obtain another explicit selection if a different valid option remains.
  Keep commit/push retry limits and all action permissions unchanged.
- Substitute-review consent remains a separate, per-head gate. Selecting a model
  does not approve a review, edits, push or merge. When both gates are required,
  collect both actual answers and recheck the PR head before execution. Report
  the model and `child`/`inline` route actually used, never the preferred model.
- Engine workflows still execute through the engine. This skill fallback does
  not move their DAG into the main conversation or rewrite a running definition.
  For an unavailable engine model, collect replacement tuning through preflight
  before starting. For a failed run, preserve state and use only supported
  recovery operations after inspecting side effects; do not blindly rerun it.

### Keep asynchronous questions open

At every skill or workflow start, establish the interaction context before any
procedure: your role (`main` or `child`), the user-facing client (including T3
Code), its surface (`GUI`, `TUI`, or `unknown`), and the actual permitted question
tool in this session and mode. Use live session metadata and tool schemas. Do
not infer the surface from the provider name, installed CLIs, shell access, or
MCP registration. A Codex provider inside T3 Code uses T3 Code's question UI.
Recheck after a client or mode change; never reuse another session's UI verdict.

The main harness owns all user interaction. Main GUI sessions use their inline
GUI controls; main terminal sessions use their native TUI controls. A native
TUI question tool is not a subprocess terminal. Inspect every available permitted
structured question tool before concluding none exists, including asynchronous
tools when a blocking tool is unavailable in this mode. Use native controls
first, then MCP forms only when the current client actually renders them.
Never open Terminal.app, a system dialog, a new shell, or a tool-owned PTY to
replace the main client's question UI. The engine's standalone terminal TUI is
available only for an explicitly requested standalone CLI session, not as an
automatic fallback from a skill. GUI and TUI sessions follow the same ownership
and answer rules. If no permitted control exists, explain the missing capability
and use the main-harness text fallback below, including when Codex's
`default_mode_request_user_input` is disabled. Ordinary workflows never change
global client settings; offer that optional repair during `wise-init` only.
Instructions cannot create a question tool the client does not expose.

Children never collect answers themselves, even if they expose GUI/TUI tools.
Use `wise_ask` for workflow questions and let the main conductor answer through
`wise_answer`. A nested or standalone subagent without `wise_ask` sends its
question, options, value mapping, and constraints to its parent, which relays
them to the main harness. Wait for the real answer. Pass this ownership contract
recursively with every delegation. Autonomous no-question rules still apply.

This section applies to every Wise skill and shared routine that collects user
input, including setup, discovery, document wizards, confirmations, dispatch
selection, preflight, and workflow gates. Read this section alone for non-workflow
skills; it does not require engine setup. Existing no-prompt rules and prior user
decisions still apply. Do not add a question merely to use this procedure.

#### Main-harness text fallback

Use this only when no permitted native question tool or rendered MCP form is
usable for the question. This applies equally to Claude, Codex, Grok, Cursor,
T3 Code and other clients. The underlying provider name does not prove UI support.
An MCP decline with confirmed absent rendering is a transport failure, not a
user cancellation. If visibility is unknown, ask in the main conversation whether
the user cancelled or wants to continue in text; do not silently retry. A confirmed
user cancellation always stops the operation, never triggers fallback.

The main harness explains that it is using text fallback and asks one current
question in readable prose, with all allowed labels and relevant descriptions.
For choices, accept an unambiguous label or numbered option and map it to the
declared value. For multi-select, accept an explicit set of labels/numbers, or
explicit `None`/`Use defaults` only when valid, and produce the original array.
For text, collect a string and preserve its content. Respect cardinality,
validation and `allow_text: false`; text is a transport, not permission to add
out-of-catalog choices. Ask for clarification on an ambiguous or invalid answer.
Never print raw JSON, assume a default, infer consent from silence, or skip
worktree, permission, harness, model or effort questions.

End the turn with the text question and resume on the user's reply, retaining
cumulative answers in the conversation. The keep-turn-open rule below applies
to active asynchronous GUI prompts, not plain chat. Re-call non-interactive
preflight with cumulative answers after each valid response. No workflow starts
until all stages are answered and validated. Gates still use `wise_answer` only
after an actual answer. Children continue to relay through Wise/the parent and
never collect text answers themselves. Existing no-prompt policies remain intact.

#### Native controls and answer mapping

Treat `AskUserQuestion` in skill prose as the host's supported question mechanism,
not a guarantee of a blocking tool. Use only tools available in the current mode.
Choose the control from the question's meaning and the live tool schema, not from
the provider name. Claude, Codex, Cursor, and Grok sessions can expose different
controls depending on their client and mode. Never invent a `multiSelect` field
when the available tool does not declare it.

| Question | Preferred control | When that control is unavailable |
|---|---|---|
| Exactly one known value (`kind: choice`, a mode, model, approval or confirmation) | Single-choice picker with every allowed option | Paginate options; main-harness text fallback if no permitted control exists |
| Any allowed subset (`kind: multi`, optional stages or several reviewers) | Native multi-select picker or MCP array-enum form | Use the single-choice sequence below; do not require typed lists |
| Open-ended content (`kind: text`, a ticket URL, path, explanation or comments) | Native free-text input | Main-harness text fallback if no permitted input control exists |
| Known choices plus an explicitly allowed custom answer (`allow_text`) | Picker with the known options and the host's custom-answer affordance | Keep the known choices clickable and collect custom text only when selected |

Supply choices in the tool's **options field**, not merely in the question title.
For example, a host exposing `request_user_input_async` with string options gets
`{"questions":[{"title":"Review the plan before setup?","options":["Accept automatically","Ask for review"]}]}`.
Map those labels back to the declared values (`auto` / `ask`) after the response.
Omitting `options` creates a text-only prompt even if the title lists choices.
For Claude-style tools use the declared option objects and multi-select flag;
for other tools use their actual schema. Preserve labels, descriptions, stable
values, and cardinality. Defaults may be highlighted, never silently submitted.

Permission-mode questions (`permissions.<harness>`) remain `kind: choice`, with
`auto`, `approval-required`, and `full-access` as the engine values. Their subject
does not by itself require a chat reply. Inspect each available tool's own rules:
a restriction on `request_user_input` does not automatically restrict
`request_user_input_async`, MCP elicitation, or another native question tool.
If the blocking tool forbids permission/approval questions but the asynchronous
tool explicitly supports them, use the asynchronous picker with populated options.
Do not call the prohibited tool, rename the question to evade a restriction, or
use a workflow answer to bypass the host's own tool-execution approval system.
Higher-priority instructions that require chat for this question still take
precedence; if no permitted structured route exists, explain that limitation.

For a host exposing the current `request_user_input_async` string-options schema,
render the permission question as:

```json
{"questions":[{"title":"Which minimum permission mode should Claude use for supporting workflow steps?","options":["Auto (recommended): workspace-scoped execution","Approval required: headless requests may be denied","Bypass permissions: no provider permission checks or sandbox"]}]}
```

Repeat the full picker for every unanswered `permissions.<harness>` returned by
the engine, including supporting and fallback providers. A selection for Claude
does not answer Codex or Cursor. Do not reduce the next provider to a chat-only
"Use Auto too?" confirmation or remove its other allowed choices. Preserve a
prior answer only for its matching provider; honor an explicit user instruction
that already names several providers without asking those questions again.

Map the three labels to `auto`, `approval-required`, and `full-access`, respectively.
Use the actual provider name and the engine's current labels/descriptions. Await
the user's real selection; this example does not authorize any mode. Apply the
same per-tool capability check to all other approval and confirmation questions.

Apply this dispatch order for every question, without provider-specific exceptions:

1. Read the engine's `kind`, `options`, `default`, and constraints. They determine
   the answer shape: `choice` is one declared value, `multi` is an array of declared
   values, and `text` is a string. Do not reclassify a declared choice as text.
   A gate with options is a choice; `allow_text` adds a custom-answer route, not
   permission to hide its options. Skip already answered or locked questions.
2. Inspect the tools actually available in this session and mode. Prefer supported
   native GUI/TUI question tools; otherwise use a rendered MCP form, then the
   main-harness text fallback if neither is usable. In a tool with an `options`
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

Model questions (`model.<group>`, the `models` command, any model picker) are
the usual long list. The engine returns every option: the predefined catalog
first (`source: catalog`), then the models the installed harness reported
(`source: harness`). Render them all, in the engine's order, with the engine's
labels and descriptions, never with invented ones. Never reorder the list,
never pick a "representative" subset, and never drop an entry silently. The
catalog order is the page order: on a host that caps a question at four
options (Claude Code's `AskUserQuestion`), the first page is the first four
engine entries exactly (for claude: Fable 5.1, Opus 5, Opus 4.8, Sonnet 5).
When more entries remain: on a host whose picker renders a custom-answer box
(Claude Code), name the remaining ids in the question text so that box reaches
them; on an option-only host (T3 Code among them) that box does not exist, so
pagination is mandatory: the fourth slot becomes `More models…` and every
remaining entry is a clickable option on a later page with `Back`. Never make
`Other` the only route to an engine option on a host that cannot render it.
The highlighted default stays on the first page. Mention the source in
the description when the host shows one (`reported by the cursor harness`),
not in the value.
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
picker or rendered MCP form, use the main-harness text fallback. Missing native
multi-select alone is not a reason to abandon the single-choice sequence.
Apply this same lifecycle to setup, non-preflight skill questions and workflow
approval/ask gates. Use chat only through the fallback above, never a new terminal.

Pass this section's instructions to delegated wizards; only the main harness
renders their questions. A headless
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

Shell access provides non-interactive engine control, not a user input surface.
Use the main client's native GUI/TUI for questions. The interactive commands
below apply only when the user explicitly requested standalone terminal use.

Use the same host-selected launcher above. For standalone terminal preflight, call
`preflight <workflow> --cwd <project> --context '<context JSON>' --interactive`.
The blocking terminal TUI collects every staged answer and returns them with an
empty `questions` list. Only then run
`run <workflow> --cwd <project> --answers '<collected JSON>' --context '<context JSON>'`.
Passing a JSON argument requires proper shell quoting; use a shell argument array
when available. Never interpolate user text as shell code.

| MCP operation | CLI command after the launcher and host arguments |
|---|---|
| `wise_status` | `status [run_id]` |
| `wise_preflight` | `preflight <workflow> --cwd <project> --context <JSON>`; render returned questions in the main client's native controls |
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
