# Research: TypeScript workflow engine, multi-harness, subscription or API auth

Date: 2026-09-04. Branch: `research/ts-engine-ai-sdk`. Status: research + implementation plan, nothing built.

Requirement as stated: user drives workflows from Claude Code (CLI or desktop). Claude delegates the run to a TypeScript engine on bun. Every step declares provider, model, and effort explicitly. Each provider must work with the user's consumer subscription (OAuth login: Claude, ChatGPT, SuperGrok, Google) or with an API key. Results flow back into the Claude Code session.

## Verdict

D1. Build the engine as a harness-adapter runtime, not on the Claude Agent SDK and not with the Vercel AI SDK as the core. A step runs by spawning the vendor's own unmodified CLI in headless JSON mode (`claude -p`, `codex exec`, `cursor-agent --print`, `gemini -p`, `grok -p`), which inherits that vendor's cached subscription login or an env API key. This is the only pattern that has survived vendor enforcement (Vibe Kanban, Symphony survived; OpenCode and OpenClaw, which reimplemented Anthropic's OAuth client, were cut off).

D2. No Vercel AI SDK anywhere (confirmed 2026-09-04). The API-key path is the same vendor CLIs with the vendor's key env set (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` / `CODEX_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`). One adapter per vendor, two auth modes each, no second toolchain.

D3. Claude steps run as `claude -p --output-format stream-json` subprocesses on the unmodified binary, never `--bare`, never a re-implemented OAuth client. Anthropic's policy text sends Agent SDK products to API keys; the end user's own login on the unmodified binary is the carve-out. Note: t3code drives Claude through the Agent SDK `query()` on that same binary with the CLI's cached login and is not blocked, so the SDK is the same auth surface. Not needed for v1; `-p` with `--resume` covers multi-turn units.

D4. Keep Claude Code's native `Workflow` tool as the contingency adapter for Claude steps (`claude-session`). It runs in-conversation with per-agent `model`, `effort`, `agentType`, and schema, and needs no subprocess. It is Anthropic-only and same-session, so it cannot be the engine. After the Q1 probe it is a policy hedge, not a technical fallback.

D5. Transport is each CLI's native headless mode (`claude -p`, `codex exec`, `cursor-agent --print`, `gemini -p`, `grok -p`), not ACP and not app-server protocols. ACP works for Claude, Codex, and Grok today but has no structured output, no per-step usage, undocumented model and effort option ids, and the official Claude ACP adapter wraps the Agent SDK (API key first). Revisit when ACP adds usage and output schemas.

D6. Reference implementation: [t3code](https://github.com/pingdotgg/t3code) (MIT, Node, Effect-TS). Copy its account isolation, runtime-mode mapping, resume cursors, dual event logs, and pricing approach (see § Reference: t3code). Do not copy its long-lived-process transport.

## Where we are

F1. `workflows.py` (2681 lines) never calls a model. It is a YAML, state, and DAG helper CLI with 39 subcommands. The conductor is the main Claude Code conversation reading about 400 lines of loop prose in `wise-workflow-run/SKILL.md`.

F2. Steps dispatch in-conversation through `Task`. Model is a real per-call parameter. Effort is a sentence appended to the prompt and may be ignored. The two-table effort clamp (36 tests) feeds an advisory knob.

F3. The loop has a prose-only invariant: every conductor message must end in a tool call or the run stalls silently. Thirty lines of guardrail text exist because there is no mechanism.

F4. Every step's output passes through the conductor transcript. Log excerpts are capped at first 60 / last 60 lines to save conductor tokens. A run occupies the session. `interactive` steps cannot run in parallel.

F5. The two `-auto` orchestrators (`process-tickets.md`, `process-plans.md`) are prose loops that must stay word-for-word mirrored. Compaction can orphan them mid-run.

F6. 240 tests, all on the Python helper's pure functions. Nothing tests the conductor loop.

## What the research found

Vendor auth and headless surfaces (verified 2026-09-04):

| Vendor | Headless command | Subscription login reuse | Model / effort flags | Structured output | Policy text |
|---|---|---|---|---|---|
| Anthropic | `claude -p --output-format json` | Yes, cached OAuth. Help center: `claude -p` and Agent SDK "draw from your subscription's usage limits" | `--model`, `--effort low..max` | `--json-schema` | Explicit. OAuth "exclusively for purchasers" of plans, for "ordinary use of Claude Code". Third parties may not offer claude.ai login or route through plan credentials. End user signing into the unmodified binary is allowed |
| OpenAI | `codex exec --json`, `@openai/codex-sdk` | Yes, `~/.codex/auth.json` when no API key env is set | `model`, `modelReasoningEffort` minimal..ultra | `outputSchema` / `--output-schema` | None found. Docs recommend API keys for CI. Staff declined to answer third-party questions |
| Google | `gemini -p --output-format json` | Yes, "will use your existing authentication method if cached". Docs table steers headless to API keys | `-m`; no effort flag | None found | None found |
| xAI | `grok -p --output-format streaming-json` (Grok Build CLI, Aug 2026) | Yes, SuperGrok / X Premium Plus via `grok login`; headless docs cover scripts and CI | `-m`; `--effort` per third-party cheat sheet | `json` with usage and cost; ACP | None found. 403s hit only third-party clients calling `api.x.ai` with raw OAuth tokens |

F7. Claude Agent SDK TypeScript: bun supported, spawns the CLI, `ANTHROPIC_API_KEY` only per its docs, no per-call effort, nested launch under a running session undocumented. Anthropic: "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK."

F8. AI SDK v7 (`ai@7.0.92`): stable `ToolLoopAgent`, `prepareStep`, `Output.object`, MCP client. Harness adapters run inside Vercel Sandbox with env API keys only. No subscription path, no repo toolchain. Rejected (D2).

F9. AI SDK has no durable DAG. `WorkflowAgent` + Workflow DevKit is beta with an unverified bun blocker. Mastra works on bun with build caveats. A hand-written DAG over a JSON ledger is what the Python engine already is and stays the plan.

F10. Claude Code native `Workflow` tool is GA: `agent(prompt, {model, effort, agentType, schema, isolation})`, `pipeline()`, `parallel()`, background run, `/workflows` viewer, same-session resume. Script is plain JS with no filesystem, no `Date`, agents cannot ask the user. Anthropic models only.

F11. ACP (Agent Client Protocol) is served by Grok Build, Gemini CLI, Codex (community `codex-acp`), and Claude Code (community `claude-agent-acp`, which wraps the Agent SDK). Viable transport, but no output schema and no per-step usage (Q4, D5).

## Options

| | O1 Native Workflow tool only | O2 Claude Agent SDK engine | O3 AI SDK runtime (rejected) | O4 Harness-adapter engine (bun) |
|---|---|---|---|---|
| Subscription auth per provider | Claude only | None (policy) | None | Claude, OpenAI, Google, xAI via own CLIs |
| API key per provider | No | Anthropic only | Yes | Yes, same CLIs with key env |
| Per-step model + effort | Yes | Model only | Yes | Yes where the CLI exposes it |
| Repo tools (edit, git, tests) | Claude Code's | Claude Code's | Must build | Each vendor's own harness |
| Roster agents, skills, MCP | Native | `agents` option | Rebuild | Claude via settings; others via their own configs |
| Deterministic control flow | JS script | TS | TS | TS |
| Human gates | Split at gate, ask in session | File handshake | File handshake | Exit at gate, resume |
| Results into the session | Native | Bash stdout | Bash stdout | JSONL events tailed by conductor |
| Cross-session resume | No | Own ledger | Own ledger | Own ledger |
| Policy exposure | None | High | n/a | Low if unmodified binaries; xAI allowlist risk |

O1 fails the multi-provider requirement. O2 fails the subscription requirement outright. O3 rebuilds a coding harness and still has no subscription path (rejected, D2). O4 meets every stated requirement and is the pattern the vendors have tolerated.

## Reference: t3code

t3code (`apps/server/src/`, HEAD 163d86a) runs every provider as a long-lived per-thread process: Claude via Agent SDK `query()`, Codex via `codex app-server` JSON-RPC, Grok / Cursor / Antigravity via ACP, OpenCode via `opencode serve`. It uses one-shot headless calls only for side tasks: `claude -p --output-format json --json-schema <s> --model <m> [--effort e] --dangerously-skip-permissions` and `codex exec --ephemeral --skip-git-repo-check -s read-only --model m --config model_reasoning_effort="x" --output-schema f --output-last-message f -` with the prompt on stdin. Auth is always the vendor CLI's cached login. No Gemini support, no `CLAUDECODE` sanitisation.

Patterns wise copies:

- T1. Account isolation by `CLAUDE_CONFIG_DIR` and `CODEX_HOME` per instance, never by swapping HOME (keychain breaks). wise: optional per-run `config-dir` to pick an account.
- T2. One `runtimeMode` enum (`approval-required | auto | full-access`) translated per provider: Claude `acceptEdits | auto | bypassPermissions`, Codex `untrusted/read-only | on-request/workspace-write | never/danger-full-access`, Grok `--permission-mode` or `--always-approve`. wise: `-auto` workflows run `full-access`, interactive ones `auto`.
- T3. Provider-specific opaque resume cursor persisted with the unit and validated before use (Claude `session_id` uuid + last assistant uuid, Codex `threadId`). wise: ledger stores the cursor per unit so review-fix cycles reuse a session instead of re-reading the repo.
- T4. Raw vendor events logged as NDJSON beside the normalised event stream (`session.*`, `turn.*`, `item.*`, `thread.token-usage.updated`). wise: `logs/<step>.raw.jsonl` + `events.jsonl`.
- T5. Subagent traffic keyed by `parent_tool_use_id`: drop narration, keep tool_use.
- T6. Usage from `result.usage` and `total_cost_usd`; historical usage priced via LiteLLM's `model_prices_and_context_window.json`.

Pitfalls it documents: resume handshake emits `system/init` + `result(num_turns: 0)` and must not count as a turn; `interrupt()` can ack while background tasks keep the CLI alive, so hard-close; `~` in env values is not expanded by spawn; Windows `.cmd` shims fail under spawn without shell; `git worktree add` can exceed 30 s on large repos (timeout 300 s); Codex `approvalsReviewer` is sticky across resume unless set explicitly.

## Architecture (O4)

Components, all under `plugins/wise/engine/`, TypeScript run as source on bun or Node 24 (see § Runtime and toolchain):

- `defs`: YAML load, validate, locate, user-root shadowing. Schema adds `harness: claude | codex | cursor | gemini | grok` (default `claude`) and `auth: subscription | api-key` (default `subscription`).
- `resolve`: ported model family, effort clamps, policy ceilings, retired ids, low-profile Opus rule, team resolution. Extended with per-harness effort vocabularies (Codex `minimal..ultra`, Anthropic `low..max`).
- `scheduler`: DAG waves, `trigger-rule`, `when:` as a real expression evaluator.
- `adapters`: one module per harness. Each exposes `run({prompt, model, effort, schema, cwd, mode, resume}) -> {text, json, usage, cost, cursor}` and streams normalised events. Spawns the vendor binary headless under a clean env (`env -i` plus HOME, PATH, the vendor config-dir var, and the vendor key var only when `auth: api-key`). Commands: `claude -p --output-format stream-json --json-schema --model --effort --permission-mode|--dangerously-skip-permissions [--resume]`; `codex exec --json --output-schema --model --config model_reasoning_effort= -s workspace-write|--full-auto [resume]`; `cursor-agent --print --output-format stream-json --model --mode|--force --sandbox [--resume]`; `gemini -p --output-format stream-json -m --approval-mode`; `grok -p --output-format streaming-json -m --always-approve --no-auto-update [--resume]`. Raw vendor stream logged as NDJSON (T4).
- `ledger`: run dir, `state.json`, `events.jsonl`, per-step logs, per-unit resume cursors (T3), prune, session guard. Cross-session resume.
- `gates`: `approval` and `ask` steps emit a `gate` event and park the run; the harness answers through `wise_answer` (D13).
- `daemon` + `mcp` + `cli`: `wise-engined` (socket JSON-RPC), `wise-engine mcp` (stdio MCP client to the daemon), `wise-engine run|status|answer|cancel|compile-check` (terminal client). One protocol, two transports (D13).

Conductor (`wise-workflow-run/SKILL.md`) shrinks to: `wise_preflight`, ask, `wise_run`, loop on `wise_wait`, answer gates, render the final report. The loop-discipline prose (F3) goes away because the daemon, not the model, holds the loop.

The `-auto` orchestrators become engine modules: `units.ts` runs `pipeline(units, plan, implement, reviewFixLoop, pr, watch)` with `harness` per phase, replacing two mirrored prompt files.

Billing and profiles: `usage` from every adapter lands in the ledger. `/wise-profile low` refuses `auth: api-key` steps unless the workflow sets `allow-api: true`. Subscription runs are recorded as plan usage; api-key runs as dollars via the vendor's cost field or LiteLLM pricing (T6).

## Delegation model and harness protocol

D11. The engine owns the questionary spec, the harness asks. `wise_preflight(workflow)` returns JSON questions (profile, one per tuning group with harness + model + effort, step-select, workflow inputs) with defaults from the YAML. Claude Code renders them with AskUserQuestion and passes the answers back as run parameters. The engine never talks to a terminal user itself, so any MCP-capable harness (Claude Code, Codex, Gemini CLI) can drive it.

D12. Choice granularity is per tuning group plus profile, as today: groups such as plan / implement / review / watch each get `harness`, `model`, `effort`; `low | medium | max` pre-answers them. Per-step pins stay in YAML for authors. No per-step questions at run time.

D13. Comms are JSON-RPC, carried two ways. A detached daemon `wise-engined` owns runs, ledger, and adapters and listens on a Unix socket. The harness reaches it through an MCP server `wise-engine mcp` (stdio JSON-RPC, declared in the plugin's `.mcp.json`, spawned by the harness, thin client to the daemon). Tools: `wise_preflight`, `wise_run(workflow, answers)`, `wise_wait(run, timeout)` (long-poll, returns events until gate / done / timeout), `wise_answer(run, gate, value)`, `wise_status`, `wise_cancel`. A CLI client speaks the same socket protocol for terminal use. Gates: the daemon emits a `gate` event, `wise_wait` returns it, the harness asks the user, then calls `wise_answer`. Interactive preflight uses MCP form elicitation when the client advertises it, with a terminal TUI fallback. Why not raw stdio to the harness: Claude Code's Bash tool cannot hold a child's stdin open, MCP is the one long-lived channel it keeps. Why a daemon: runs outlive the harness session, survive compaction, and no 10-minute Bash cap.

D14. The `interactive` prose orchestrators (`process-tickets.md`, `process-plans.md`) move fully into the engine as code (`units.ts`), with a harness per phase. Only true user decisions surface as gates. The harness sees events and gates, never step output.

Consequences: `plugins/wise/.mcp.json` stops being empty; `/wise-init` must run before the first workflow so bun or Node 24 exists when the harness spawns the MCP server; the conductor SKILL shrinks to "call `wise_preflight`, ask, call `wise_run`, loop on `wise_wait`, answer gates, render the final report".

## Tokenomics

Two cost pools: the harness session (the user's Claude plan, main conversation) and the child processes (each vendor's plan window, or dollars under `api-key`). Adopted rules; minimise, not paranoid.

E1. The harness never sees step output. `wise_wait` returns one verdict line plus the step's structured outputs, payload capped; logs stay in the ledger. The harness supplies the context the run needs up front: `wise_run` takes a `context` object (ticket text, operator guidance, links, decisions already made in the conversation) that the engine injects into prompts, so children do not refetch what the session already knows.

E5. Engine pre-computes, agents do not discover. Diffs, file lists, test output, CI log excerpts go into the prompt as data. Exploratory tool-call turns are the dominant agent cost.

E7. Cache-aware scheduling. Same-model steps run back to back, prompts keep a stable prefix per phase, `--system-prompt-snapshot on` where the CLI supports it.

E8. `--resume` inside a phase pair (review to fix), fresh session across phases. Turn cap per session; after N cycles start fresh to stop context growth.

E9. Structured outputs via the vendor's schema flag replace the `until:` regex plus `max_iterations` re-run pattern. A failed regex today reruns the whole step.

E11. Hard caps. Per-step `--max-turns`, `--max-budget-usd` under `api-key`, per-run token ceiling from the profile in the ledger. Exceeding a cap parks the run at a gate instead of burning on.

E12. Rate-limit-aware routing. The adapter recognises plan-window exhaustion (429 or rate-limit result) and pauses with backoff or routes to the step's fallback harness list (`harness: [claude, codex]`). Spreads load across the user's subscriptions.

E14. Measure first. Ledger records input, output, cache-read, cache-write tokens per step and phase; `/wise-report` shows them. A10 baselines the prose conductor against the engine on the same ticket before further tuning.

Considered and dropped: harness-turn minimisation and stable-prefix tricks for the conductor (E2 to E4; after E1 the conductor is glue), aggressive child system-prompt trimming with `--strict-mcp-config` and tool allowlists (E6; measure the stock cost in the spike as Q6, act only if large), deterministic replacements for LLM classification (E10) and per-lens diff slicing for the review panel (E13; over-engineering for now).

## Runtime and toolchain

D7. Bun is the preferred runtime, Node 24 LTS supported. `engine.sh` picks `bun` when on PATH, else `node`. Both execute the same `.ts` source unchanged.

D8. TypeScript with erasable syntax only, run as source. No build, no `dist/`, no CI build job; the plugin ships source. Node 24 strips types natively (unflagged since 23.6), bun always did. Rules: no `enum`, `namespace`, parameter properties, or decorators; imports carry the `.ts` extension; `tsconfig` sets `erasableSyntaxOnly`, `allowImportingTsExtensions`, `verbatimModuleSyntax`, `noEmit`.

D9. Toolchain is the oxc / native stack, wired into `just check` beside the tests: `tsgo --noEmit` (TypeScript native compiler) for type checking, `oxlint` for lint, `oxfmt` for formatting. Type checking is a lint, not a build; a check failure blocks the commit, never the runtime.

D10. Tests use `node:test` + `node --test` / `bun test` so the suite runs on both runtimes. `bun:test` is bun-only and is not used.

Consequences: `/wise-init` dependency probe raises Node minimum from 22 to 24 and prefers bun; `bootstrap-deps.sh` installs bun when missing; `.gitignore` gains nothing new because nothing is generated.

## Risks

R1. Terminal `claude` login is separate from the desktop app's host-managed token. A child `claude -p` spawned from a desktop session fails with "OAuth session expired and could not be refreshed" until the user runs `claude auth login` once in a terminal. Mitigation: `/wise-init` probes `claude auth status` and tells the user; the engine spawns with a clean env (`env -i` plus HOME and PATH) so no host-refresh vars leak in.

R2. Anthropic policy drift. Today's text carves out the unmodified binary on the user's own login and states `claude -p` draws from plan limits. The plugin is distributed, so a stricter reading is possible. Mitigation: never ship an OAuth client, never use the Agent SDK with OAuth, document the API key path, and keep `claude-session` as the zero-exposure adapter.

R3. xAI OAuth allowlist. Third-party clients were 403'd. Spawning the official `grok` binary should be inside the allowlist, unverified (Q3).

R4. Google steers headless to API keys. Cached-login reuse works today by docs' own words but is not the recommended path. Treat Gemini subscription mode as best-effort.

R5. Cost opacity for `auth: api-key` steps. Mitigation: usage per call in the ledger and in `/wise-report`; profile rule above.

R6. Effort parity. Effort vocabularies differ per vendor and Gemini has none. Mitigation: `resolve` maps wise's `low..max` to each vendor's scale and records the mapping in the step's reason line, same as today's ceiling notes.

R7. Loss of in-session tooling for Claude steps. A `claude -p` child loads the user's settings, plugins, skills, and agents, but not the parent session's MCP OAuth connectors. Mitigation: pass `--mcp-config` where needed; document.

R8. Migration scope. Two prose orchestrators become TypeScript. This is the largest item and the one that pays back most (F5).

## Implementation plan

Phase 0, spike (1 to 2 days, Q1 to Q5 largely answered below):

A1. After `claude auth login` in a terminal, rerun the Q1 probe and confirm `total_cost_usd`, `modelUsage`, and `structured_output` populate under the subscription.

A2. `codex exec --json --output-schema` on bun with ChatGPT login and `modelReasoningEffort: high`. Confirm no API key needed.

A3. `gemini -p --output-format json` and `grok -p --output-format streaming-json` with cached logins. Record which honour model flags and whether xAI accepts the official binary (Q3).

A4. `codex exec` on bun for an implement-shaped step: `-s workspace-write`, `--output-schema`, `model_reasoning_effort=high`, then a second call resuming the thread. Confirm ChatGPT login, no key.

A5. `grok -p --output-format json` with `--effort`: confirm the flag exists on the shipped binary (third-party cheat sheet only) and that `usage` and `cost` fields populate.

Phase 1, engine core (runtime and toolchain per D7 to D10):

A6. Scaffold `plugins/wise/engine/`: erasable-syntax TS, `tsconfig` per D8, `tsgo` + `oxlint` + `oxfmt` in `just check`, `node:test` suite, `engine.sh` launcher (bun else node). Modules `defs`, `resolve`, `scheduler`, `ledger`, `cli`.

A7. Port the 240 Python tests to `node:test` module by module. Delete `workflows.py` at parity.

A8. Adapter `claude` (both auth modes), runtime-mode mapping (T2), resume cursor (T3), raw + normalised logs (T4). Daemon + socket JSON-RPC, MCP server with the six tools, gate protocol (D13).

A9. Conductor rewrite on the MCP tools: preflight, ask, run, wait loop, gates, final render. `.mcp.json` declares the server. Remove loop-discipline prose.

A10. Run `ticket-plan` end to end on the new engine. Compare tokens per pool (harness, children by vendor), wall clock, and session transcript size against the prose conductor (E14 baseline).

Phase 2, harnesses:

A11. Adapters `codex`, `cursor`, `gemini`, `grok`. Effort mapping table in `resolve`.

A12. `units.ts`: port `process-tickets.md` and `process-plans.md`. Run `ticket-auto` on one ticket with plan on Claude, implement on Codex, review on Claude, to prove cross-harness handoff through the worktree.

Phase 3, product:

A13. Profiles and cost: per-adapter usage in ledger and `/wise-report`; `allow-api` rule; LiteLLM pricing table for api-key runs (T6).

A14. Docs sync: `docs/wise/workflows.md` (harness field, effort mapping, gate protocol), skill references, workflow READMEs, `/wise-init` (Node 24, bun preferred, tsgo/oxlint/oxfmt as dev deps only). Validator runs `wise-engine compile-check` per bundled workflow.

A15. Release v5.0.0. Keep the prose conductor one release as fallback behind a flag, then remove.

## Spike answers (web research + one local probe, 2026-09-04)

Q1. Nested `claude -p`. Answered. Claude Code sets `CLAUDECODE=1` and `CLAUDE_CODE_ENTRYPOINT`; docs and issue #32618 describe a nesting guard with the workaround `env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT claude -p`. On 2.1.259 from a desktop session the child started with the guard set and without it. Both failed at auth because the terminal CLI was not logged in (`claude auth status` returned `loggedIn: false`); the desktop app holds a host-managed token (`CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH=1`) that children cannot use. Each child got its own `session_id`. Help center: `claude -p` draws from the plan's usage limits. Left: rerun after login to see usage fields (A1).

Q2. Effort control. Answered. `--effort low|medium|high|xhigh|max`, env `CLAUDE_CODE_EFFORT_LEVEL`, settings key `effortLevel`. Also `--model`, `--json-schema`, `--output-format json|stream-json`, `--max-turns`, `--max-budget-usd`, `--tools`, `--allowedTools`, `--permission-mode`, `--append-system-prompt`, `--mcp-config`, `--settings`, `--plugin-dir`, `--agents`. `--bare` skips hooks, plugins, skills, keychain and accepts only `ANTHROPIC_API_KEY`, so the engine never uses it for subscription runs. The JSON result carries `session_id`, `total_cost_usd`, `usage`, `modelUsage`, `structured_output`, `permission_denials`, `num_turns`.

Q3. xAI allowlist. Answered. docs.x.ai headless page: "assumes `grok` is already authenticated locally, or `XAI_API_KEY` is set", and covers scripts, bots, CI with `--no-auto-update`. The 403 reports (Hermes, openclaw, Wayland) all came from third-party clients sending an xAI OAuth bearer straight to `api.x.ai`. The official CLI uses its own OIDC client and routes subscription traffic through `cli-chat-proxy.grok.com`. `grok agent stdio` advertises `cached_token` auth. JSON output fields: `text`, `stopReason`, `sessionId`, `usage`, `cost`. `--effort none..max` and `--max-turns` are reported by a third-party cheat sheet only (A5). Install: `curl -fsSL https://x.ai/cli/install.sh | bash`, npm `@xai-official/grok`, Homebrew cask `grok-build`; macOS arm64 supported. Repo has issues disabled.

Q4. ACP transport. Answered, not adopted (D5). Servers: `npx @agentclientprotocol/claude-agent-acp` (wraps the Agent SDK, API key first, subscription only via community CLI bridges), `npx @agentclientprotocol/codex-acp` (on `codex app-server`, ChatGPT login or key), `gemini --acp` (history of ignoring cached login in non-TTY, issues #12042 #10855 #16504), `grok agent stdio` (cached token, cleanest). Protocol has `session/set_config_option` for model and `thought_level` but no output schema and no per-step usage. `@ai-sdk/harness-acp` needs Vercel Sandbox. TypeScript SDK `@agentclientprotocol/sdk` v1 uses Web Streams; bun untested officially.

Q5. Child environment. Mostly answered. Headless without `--bare` loads user and project settings, marketplaces, plugins, skills, agents; `--plugin-dir` adds one inline. `CLAUDE_PLUGIN_ROOT` and `CLAUDE_PLUGIN_DATA` are set for plugin components. The child's `session_id` is new, so `/wise-profile` state (keyed by session id) does not carry over; the engine passes the profile as an explicit input. `CLAUDE_CODE_SESSION_ID` inheritance for the child process itself is undocumented; the engine strips it.

Remaining unverified: Claude cached-login reuse from a non-TTY child (Codex confirmed in M0.3); current Gemini ACP auth behaviour (irrelevant under D5). `grok --effort` confirmed in M0.4.

Q6 (new). Stock cost of a `claude -p` child before any work: system prompt with the user's MCP servers, plugins, and skills loaded. Read `system/init` and first-turn `input_tokens` in the A1 rerun. Decides whether E6 trimming is worth doing.

### M0 results (2026-09-05)

Environment: node 24.18; bun, gemini, tsgo, oxlint, oxfmt, just not installed; codex-cli 0.149.0 logged in via ChatGPT; grok 1.0.5 with cached login; `claude auth status` → `loggedIn: false`, so M0.1, M0.2, M0.6 wait on `claude auth login`.

M0.1 claude. Pass after `claude auth login` (auth method `claude.ai`). `env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_CODE_SESSION_ID claude -p … --output-format stream-json --verbose --json-schema … --model haiku --effort low` returned `result/success`, `is_error: false`, `structured_output` matching the schema, `total_cost_usd`, `usage`, `modelUsage` with `costUSD`, `contextWindow`, `canonicalModel`, and a fresh `session_id`. Event order: `system/hook_started` and `hook_response` for the user's three hooks, `system/init`, `system/thinking_tokens`, `assistant`, `rate_limit_event`, `user`, `result`. Q6 answered: `system/init` is 18.8 kB and lists 215 tools, 8 MCP servers, 24 plugins, 102 skills, 23 agents; the first turn writes 27,169 tokens of cache (1 h ephemeral) and the trivial three-turn haiku call cost $0.060. The user's hooks run inside every child.

M0.2 claude stdin. Pass. `claude -p --input-format stream-json --output-format stream-json --verbose --json-schema …` with no prompt argument: first user message on stdin produced `init`, `assistant`, `result#1` (schema-valid, session S); a second user message written after that produced `init` again with the same session S, then `result#2` schema-valid and referring to the first answer (`n` 42 to 43, `pong` to `gnop`); closing stdin exited 0. Each message yields its own `result` event with per-turn `num_turns` and cumulative `total_cost_usd`, so the engine can nudge a running child and still read a schema-valid final result.

M0.3 codex. Pass. `codex exec --json --output-schema schema.json -c model_reasoning_effort=low -s read-only --skip-git-repo-check -C .` with no `OPENAI_API_KEY` in env. Events: `thread.started` (`thread_id`), `turn.started`, `item.completed` with `item.type: agent_message` whose `text` is the schema-valid JSON, `turn.completed` with `usage {input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens}`. Stock input 13.7k tokens. `codex exec resume <thread_id> --json --output-schema … "n = previous n + 1"` kept the thread id, returned n+1, 13.7k cached. Stdin must be closed (`</dev/null`), otherwise the CLI prints "Reading additional input from stdin..." and waits. Spawned from node; bun rerun folds into M1.1.

M0.4 grok. Pass. Flags confirmed on the shipped 1.0.5 binary: `--json-schema <SCHEMA>` (implies json output), `--reasoning-effort <EFFORT>` with alias `--effort`, `--resume <id>`, `--continue`, `--session-id`, `--cwd`, `--permission-mode`, `--prompt-file`, `--prompt-json`, `--output-format plain | json | streaming-json (ACP session updates) | streaming-messages-json (Anthropic Messages wire NDJSON)`. JSON result: `text`, `stopReason`, `sessionId`, `requestId`, `thought`, `usage {input_tokens, cache_read_input_tokens, cache_creation_input_tokens, output_tokens, reasoning_tokens, total_tokens}`, `num_turns`, `total_cost_usd`, `modelUsage` per model, `structuredOutput`. Stock input 21.1k tokens; default model `grok-4.6-build`; resume kept the `sessionId` and honoured the schema. Closes the `grok --effort` unknown. Gemini not installed here; its probe moves to M5.3, which stays best effort.

M0.5 MCP host. Partial. Server in erasable TS on `@modelcontextprotocol/sdk` runs as `node server.ts` with no build; a stdio client lists both tools and `wise_block` returns after the requested seconds. `claude --plugin-dir <dir> mcp list` reports `plugin:wise-spike:wise-spike … ✔ Connected` from a plugin `.mcp.json` using `${CLAUDE_PLUGIN_ROOT}` and an `env` block; the server log shows the injected token, so per-step env injection works at the plugin layer too. Docs (code.claude.com/docs/en/mcp): `MCP_TOOL_TIMEOUT` defaults to about 28 h, a per-server `timeout` in ms in `.mcp.json` overrides it; a call with no response and no progress notification aborts after an idle window of 30 min for stdio servers (`CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=0` disables); a main-conversation call that runs past 2 min moves to a background task. The live 4-min block inside a Claude turn waits on login.

M0.5 live block. Pass. `claude -p --plugin-dir <spike> --permission-mode bypassPermissions --json-schema …` on haiku: the child called `wise_block seconds=240`, the server log shows start and end 240 s apart, the child received `blocked 240s` and returned it in `structured_output` (`is_error: false`, five turns, `duration_ms` 248 241, $0.033). No `MCP_TOOL_TIMEOUT` or idle abort at 4 min in headless mode. The 2-min move-to-background applies to the interactive host conversation and was not measured here.

M0.6 child MCP injection. Pass. `claude -p --mcp-config <file> --strict-mcp-config --permission-mode bypassPermissions --json-schema …` with the spike server and `WISE_STEP_TOKEN=tok-child-inject` in the server `env`: the daemon-side log shows `wise_report token=tok-child-inject status="child hello from spike"` during the turn, and the final result was `is_error: false`, `structured_output {answer: "reported", n: 1}`, four turns, $0.024 on haiku. `--strict-mcp-config` dropped the user's eight MCP servers from the child, so the engine can hand each child exactly one server. In `-p` mode MCP tools need an allow rule or a permission mode, otherwise the call lands in `permission_denials`.

M0.7 verdict (2026-09-05). Gate M0 passed: M0.1, M0.3, M0.5 pass, M0.2 and M0.6 pass too, so P8 is confirmed and M2.6 stays. Decisions added:

- D15. Go on three transports: `claude -p` (stream-json, `--json-schema`, `--input-format stream-json`), `codex exec --json --output-schema` with `resume`, `grok -p --json-schema --reasoning-effort --resume`. Gemini stays best effort in M5.3; its CLI is installed but the user's login is broken, so no probe until that is fixed.
- D16. P8 confirmed: children get one engine MCP server via `--mcp-config` + `--strict-mcp-config` (Claude) with a per-step token in `env`; nudges go through the open stdin on Claude, kill + cursor resume elsewhere.
- D17. `wise_wait` default drops from 240 s to 110 s (cap stays 600 s) so a default call returns before the host moves it to a background task at 2 min. The daemon sends MCP progress notifications every 30 s during a wait, which keeps the 30-min stdio idle abort away for long explicit waits. If a wait is moved to the background anyway, the conductor treats the task notification as the wake-up and calls `wise_wait` again.
- D18 (E6 revisited). Stock child cost is 27.2k prompt tokens with 215 tools, 24 plugins, 102 skills and three user hooks, so trimming is worth doing but stays moderate: every Claude child runs with `--strict-mcp-config` and the engine's config only (proven in M0.6, removes the user's MCP servers); `--settings` to drop plugins, skills and hooks is measured in M2.1 and adopted only if it cuts the prompt by more than a third without breaking the wise skills the steps rely on.
  - D18 revised (2026-09-07, v5.0.0-rc.3). Re-measured on Claude Code 2.1.263: a stock child costs 43.2k prompt tokens, a strict one 40.4k; the CLI now defers MCP tool schemas behind `ToolSearch`, so the inherited servers are worth ~3k tokens, not 27k. Meanwhile strict children could not reach any tracker (Jira, Linear) the conductor had, and failed twice in the field. Children now inherit the CLI's MCP servers by default (`mcp: inherit`), `mcp: engine-only` keeps the strict flag per step, the engine is the child's permission host (`--permission-prompt-tool stdio`, shape-based allow/deny in `permissions.ts`), the conductor fetches tickets before the run and the engine files them under `<run dir>/context/tickets/`, and `/wise-init probe-mcp` reports the inventory a child actually inherits.

### M1 parity report (2026-09-05)

Engine core landed in `plugins/wise/engine/` (`src/types.ts`, `paths.ts`, `defs.ts`, `preflight.ts`, `profile.ts`, `resolve.ts`, `scheduler.ts`, `ledger.ts`, `render.ts`, `cli.ts`). 276 `node:test` tests, green under `bun test` and `node --test` on node 24. Python suite: 154 test functions, 240 collected ids. Disposition of every Python test:

| Disposition | Count (functions) | Detail |
|---|---|---|
| Ported 1:1, same name | 122 | Includes the four parametrised families (`test_ceiling_table` 16, `test_trigger_rule_truth_table` 22, `test_next_wave_when_*` 12, `test_get_*_invalid` 26) as per-row tests. |
| Ported under another name or merged | 18 | `test_plugin_data_root_*` (3) into `pluginDataRoot` tests in ledger and profile; `test_session_id_*` (3) into `currentSessionId` tests in profile; `test_installed_plugins_*` (3) into two `installedPlugins` tests in defs; `test_init_state_rejects_*` (4) into `initState` id validation in ledger and `validateDef` in defs; `test_write_log_*` (3) into log path tests in ledger; `test_save_yaml_*` (2) into the atomic-write test in ledger. |
| Dropped, v2 design | 4 | `test_next_wave_rejects_*` (2): the compiler rejects bad step ids before scheduling, `nextWave` trusts a validated def. `test_list_inputs_default_must_be_an_option`, `test_list_inputs_malformed_option_rejected`: v2 has no choice inputs, `options:` is a v1 hint. |
| Kept in pytest, outside the engine | 10 | `test_hook_contract` (4, shell hook), `test_compact_ledger_*` (2, `insights.py`), `test_save_registry_*` (1, `init-registry.py`), `test_worker_heartbeat_*` (3, v1 supervised-worker watchdog; deleted with `workflows.py` at M3.4). |

Behaviour changes recorded by the port (all intended):

- Errors are thrown (`LedgerError`, `ResolveError` with `exitCode`) instead of exit codes and stderr; the CLI maps them. Notices come back as arrays.
- `when:` is a real expression (`==`, `!=`, `&&`, `||`, `!`, parentheses); unparseable expressions stay truthy with a `warnings` entry. A v1 list form is accepted and AND-ed by the scheduler, and flagged as a v1 hint by the compiler.
- `failed` runs are resumable and never pruned (`TERMINAL_RUN` = completed, cancelled), as in v1.
- Invalid preflight values are compile errors, not warn-and-fallback.
- `{{run.dir}}` renders only when the caller passes the run dir; `state.inputs` and `state.outputs` are merged for templating with outputs winning.
- Model-family tables stay Claude-specific in `resolve`; `effortFor(harness, effort)` carries the P6 mapping and adapters apply it.
- `syntheticSessionId`, XDG roots and cwd slug live once in `paths.ts`; the slug realpaths the nearest existing ancestor like Python's non-strict `Path.resolve()`.

CLI (M1.8): `wise-engine preflight <wf> [--profile] [--context <json>]` emits the P1 `{workflow, version, questions, defaults}` shape; `compile-check <wf>...` exits 1 with per-issue v1 hints (all four bundled workflows fail until M6.4 migrates them); `migrate <yaml>` is a dry run listing v1 to v2 changes; `list-defs`; `version`. `<wf>` is a name resolved through user then bundled roots, or a path.

Gate M1 passed. Open for M2: `Question.optional` added to types for skippable inputs; `State.run_dir` not added (render takes the dir as a parameter).

### M2 gate report (2026-09-05)

Execution layer landed: `src/adapters/{spawn,claude,index}.ts`, `src/{protocol,rpc,daemon,client}.ts`, `src/executor.ts`, `src/steps/{agent,bash,gate}.ts`, `src/auth.ts`, `src/channel.ts`, `src/mcp.ts`, `src/unit-mcp.ts`, `src/cli-client.ts`; `plugins/wise/.mcp.json` declares the `wise-engine` server (`bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh mcp`, per-server timeout 660 s). 426 tests green on bun and node.

Gate evidence, all on 2026-09-05 against real Claude children (haiku, sonnet) with the run data root in a scratch dir:

| Criterion | Evidence |
|---|---|
| Completes from Claude Code via MCP | `claude -p --model sonnet --effort low --strict-mcp-config --mcp-config …` given only the six `wise_*` tools drove `example-workflow.v2` to `completed`: 13 conductor turns, 26 engine events, 2 gates answered (ask `docs`, approval `approve`), $0.30 conductor cost. |
| Gate round trip | Ask gate and approval gate parked the run as `gated`, `wise_answer` resumed it, `gate.answered` and `step.done` followed within the same second. Same via `wise-engine run --follow` from a terminal, answers read from stdin. |
| Daemon survives restart | `kill -9` on `wise-engined` 4 s into a run: recovery marked the run `paused` and reset the running step; the next client call auto-started a daemon; `resume` re-ran `classify` (attempts 2) and the run finished through both gates. No orphan `claude -p` after the crash. |

Behaviour worth recording:

- A run parked at a gate holds no children, so the daemon counts it idle and exits on `daemon stop` or after the idle window; state lives on disk and the next client call restarts the daemon. Intended, matches P5.
- Child usage under subscription for the eight-step fixture: about 700 output tokens and 30k cache-read tokens per run, `cost_usd` around $0.007 reported by the children. The conductor session dominated cost by 40x, which supports E1 (compact events) and D17 (long `wise_wait`).
- Concurrency cap held at two Claude children in flight (fixture wave of three agent steps plus three bash steps).
- Haiku returned a nested JSON string for `summarize-project` once; the schema accepted it because the field is `string`. Prompt hygiene for M3.1, not an engine fault.
- Found and fixed during the gate: `wise-engine run --help` started a run (client ignored `--help`); `run` sent the located name instead of the given path, so path-form workflows failed with `WORKFLOW_NOT_FOUND`.

Deferred from M2, carried into later milestones: `--settings` trimming measurement (D18) not yet done; `--max-budget-usd` under `auth: api-key` (E11) not wired; `units` steps fail with "units steps arrive in M4"; `wise_context` resolves `state.context`, outputs and inputs only (ticket bodies arrive with M3.2's `Context`); a child-ask gate orphaned by a daemon restart returns `GATE_STALE` on `answer` and needs `resume`.

### M3 live smoke (2026-09-05)

`ticket-plan` v2 driven end to end by the engine on a synthetic ticket (WISE-1, body pasted into `context.ticket`), profile `low`, both tuning groups `economy`, step-select reduced to `analyze-related`, `implement_mode: plan-only`, cwd a scratch clone so `setup` could create its branch. Three runs, each about $2-3 of subscription usage as reported by the children:

| Run | Wall | Child output tokens | Child cost (reported) | Outcome |
|---|---|---|---|---|
| 1 | 8 m 36 s | 29.3k | $3.31 | `completed` but hollow: `codebase-audit` and `present-plan` blocked (run dir outside the cwd sandbox), `fetch-ticket` spent 32 turns and returned no file. |
| 2 | 8 m 51 s | 26.0k | $2.86 | run dir fixed via `--add-dir`; audit and plan written. `ensure-access` aborted and `fetch-ticket` empty: the child was permission-denied on `wise_context` and on `Bash`, visible now as `warn` events (`N permission denial(s)`). |
| 3 | 5 m 22 s | 22.4k | $2.09 | Clean. Ticket normalised from context, audit (9 reusable assets), `PLAN-WISE-1.md` with two waves, testing and validation sections, branch `WISE-1` created, zero denials. |

Findings and the decision they forced:

- D19. Headless children get explicit grants. `claude -p` cannot answer a permission prompt, so anything not pre-granted is silently denied and the child improvises around it. Every Claude child now runs with `--add-dir <run dir>` (the run directory is the channel between steps) and `--allowedTools mcp__<engine server>,<step allowed_tools>`. YAML v2 gains `allowed_tools: [rule, ...]` on any step (Claude permission rule syntax, e.g. `Bash(git:*)`, `WebSearch`); `mode` keeps its meaning (`auto` = acceptEdits) and grants add to it. `bypassPermissions` stays reserved for `mode: full-access`.
- D20. Usage cost source and ceiling arithmetic. A child-reported `cost_usd` always wins (`cost_source: reported`); the engine prices tokens from `src/pricing.ts` only for `auth: api-key` runs whose CLI reported none (`priced`), otherwise `none` with one warn per unknown model. The `caps.tokens` ceiling counts input + output + cache_write and excludes cache_read, because cache reads dominate a Claude child's token count (M3 smoke: about 90 percent) at a tenth of the price and would trip any sensible ceiling on the first step. M6.1 and M6.2, 2026-09-05.
- Prompt hygiene matters more than model tier: telling `ensure-access` and `fetch-ticket` to read `wise_context("ticket")` first cut those two steps from 46 turns and 2.0M cache-read tokens to 3 turns and 60k.
- Per-step `usage` folds `cache_read` correctly; the `step.progress` token figure is cumulative context, useful as a pace signal, not a cost.
- Child usage is dominated by cache reads (1.9M of 2.2M tokens in run 3), so the 27k stock prompt (D18) is paid on every turn of every child; `--settings` trimming remains the lever to measure.

Open for M3.3 proper: a real ticket from the user's tracker fetched by the desktop conductor, the prose-conductor baseline on the same ticket, and the desktop-app acceptance of the rewritten SKILLs (permission prompts for the prefixed `wise_*` tools on first use are expected).
- D21. Staged questionary from a model catalog; no budget profile for workflows. Pre-flight asks per unlocked tuning group, in order, which CLI runs it (`harness.<group>`, offered only when more than one is logged in), which model of that CLI (`model.<group>`, from the hand-picked catalog in `src/models.ts`) and which effort that model takes (`effort.<group>`); each answer unlocks the next stage, so the conductor calls `wise_preflight {workflow, cwd, answers}` until `questions` is empty and `wise_run` completes any stage still open with its default. The `profile` question and parameter are gone: workflows run at `medium`, `profiles.medium.caps` is the only part of `profiles` applied, `low` / `max` and tuning `options` parse and are ignored; `/wise-profile` stays a skill-side budget. Supersedes the profile half of D12. 2026-09-05.
- D22. Pre-flight asks everything, step-select first. Three corrections to D21 after ticket-plan and ticket-auto runs skipped model and harness questions. (1) `step-select` and the inputs come on the first call; the tuning stages wait for the `step-select` answer and are built only for the groups an enabled step binds (a group no step binds is always asked; a group only deselected steps bind keeps its declared value), because how many steps run decides how many model questions there are. (2) `harness.<group>` offers every installed CLI (adapter present, `Adapter.bin` on PATH; `installedHarnesses`), not only the logged-in ones; a logged-out CLI is flagged with its login command in the option (`loggedOutHarnesses`, probed only on a call that emits a harness question) and refused by the run's auth probe if picked. The question is skipped only when one CLI is installed. (3) `wise_run` no longer defaults a stage the conductor never reached: it walks the same staged questionary over the answers it was given and refuses with `MISSING_ANSWERS` (open questions attached) when any `step-select`, `harness.<group>`, `model.<group>` or `effort.<group>` question is unanswered. The CLI's `run` keeps filling defaults itself for scripted use. The conductor skill carries the matching MUST rule. 2026-09-09.
- D23. Pre-flight settles `when:` gates on the inputs. A step whose gate the known inputs already make false (`review_mode == 'ask' && user_comments != ''` with `review_mode` on `auto`) will not run, so its tuning group is not asked either. `evaluateWhenPartial` in `scheduler.ts` is the two-valued evaluator lifted to three values: an identifier the scope lacks is `UNKNOWN` and propagates through `!`, `==`, `!=`, `&&`, `||` unless the other operand settles the result; the strict `evaluateWhen` is the same parser with every identifier resolved. Pre-flight's scope is `{inputs, answers}` with inputs from the answer, the context pre-fill or the declared default; outputs are absent, so output gates stay open and keep their groups, and an unparseable gate never blocks a group. `ticket-plan`'s `implement` gate gained an `implement_mode != 'plan-only'` conjunct so `plan-only` settles it before `setup` maps the mode to `implement_choice`. 2026-09-10.
- D24. Cursor and per-provider permission floors. `cursor` is a fifth harness, dispatched through the explicit `cursor-agent` binary (the generic `agent` alias can collide with another provider), using `--print --output-format stream-json`, stdin prompts, `--resume`, `--model`, workspace/add-dir grants and `cursor-agent status --format json` for subscription auth. Cursor and Gemini drop wise effort; Cursor schemas travel as an instruction and its documented result event supplies the authoritative text/session id (token usage is unavailable, so the adapter reports zeros). After all `harness.<group>` questions settle, pre-flight asks `permissions.<harness>` once for each selected or declared-fallback provider: `auto` (recommended), `approval-required`, or `full-access` (labelled "Bypass permissions"). The answer is a floor: effective mode is `max(step/phase mode, provider floor)`, never a downgrade. Floors persist in `state.provider_permissions`; old `preflight.permissions` and string `answers.permissions` map onto every provider for resume compatibility. Claude's `auto` broker accepts ordinary local edits after realpath-aware workspace containment and a curated set of inspectable read-only commands. Repository-controlled task runners, shell wrappers and mutating external MCP calls require an explicit grant or full access. 2026-09-10.
- D25. Harness-independent preflight UI and Codex advisory events. `wise_preflight interactive:true` owns the staged question loop and requests one MCP form from the client per open question; clients without form elicitation must use a native structured picker or the CLI `run --interactive` TUI, never ordinary chat. Codex `item.completed` records whose item type is `error` are nonterminal notices and become run warnings after a successful `turn.completed`; only terminal error events, process failures, timeouts and missing completion fail the step. 2026-09-10.

### M4-M6 build report (2026-09-05)

All builds green on bun 1.4.1 and node 24: 587 tests, 582 pass, 5 skipped live tests. Milestones M4.1-M4.4, M5.1-M5.4, M6.1-M6.4 closed on fixtures and unit tests; M3.3, M4.5 and M5.5 wait for a real ticket from the desktop app.

- Units pipelines (M4): `src/units.ts` orchestrates the phases per unit with a `UnitLedger` in `units/<branch>.json`; no-model phases are code over an injectable command runner; model phases run through the agent step with prompt templates and schemas under `src/prompts/units/`. Two concurrent `git worktree add` raced on `.git/config` in the fixture repo, so git commands are serialised per units step. The plan schema is `{plan_path, status: ready | insufficient-context | no-access, blueprint_path?}`.
- Adapters (M5): codex needed OpenAI strict-mode schemas (`strictSchema`) after a live 400 `invalid_json_schema`; grok and codex take prompts above 100 kB on stdin or a prompt file. Gemini 0.46.0 is best effort (D15): `-p`, `--output-format stream-json`, `--approval-mode default|auto_edit|yolo`, `--include-directories`, `--resume <uuid>`, `--skip-trust`; no effort, schema or max-turns flag, so the schema travels as an instruction and the JSON is extracted leniently. The one live run on this machine fails with `IneligibleTierError` (login broken, as noted in M0.4), and the probe passes on the stale `oauth_creds.json`, so the failure surfaces at the first child, not at preflight. Fallback (M5.4): a `rate_limited` exit parks the harness with backoff 1, 2, 4, 8 min (cap 30) and re-dispatches the step on the next `fallback` harness with model `inherit`; cursors never cross harnesses.
- Usage (M6.1): one fold path, `priceUsage` then `foldUsageViews`, feeds `state.usage[pool]`, `by_harness`, `by_step` and `steps[id].usage`; unit phases add `UnitLedger.usage_by_phase`. `report` returns every view plus `usage_total`; `report --text` prints step | harness model | in | out | cache_read | cost with pool, harness and total rows; `{{usage}}` renders the totals JSON for the workflow `report` steps. Prices in `src/pricing.ts` from the vendors' public pages, read 2026-09-05 (D20).
- Profile rule and ceiling (M6.2): `run` throws `PROFILE_REFUSES_API` for `auth: api-key` steps under `low` without `allow-api: true`, before probes and before a run dir. Crossing `caps.tokens` opens an `approval` gate on the step that crossed (approve raises the ceiling by one more profile step, reject fails the run with `ceiling`); synchronous mode rejects on its own with a `warn`.
- Docs (M6.3): `docs/wise/workflows.md` rewritten for v2; the review surfaced two engine gaps, both fixed: the daemon `run` handler accepted a missing required input (only the CLI client checked), and `requires` was validated but never probed. `wise_preflight` now returns `requires_missing`, and `wise_run` refuses with `REQUIRES_MISSING` or `MISSING_ANSWERS` before any run dir.
- Staged questionary (D21): `src/models.ts` catalog, `src/preflight.ts` rewritten (`buildQuestionary(def, ctx, answers)`, `completeAnswers`), `profile` removed from `PreflightParams`, `RunParams`, the MCP tools and the CLI; `PROFILE_REFUSES_API` dropped; bundled workflows lose their `low` / `max` profiles and tuning presets. 2026-09-05.
- Open: `--settings` trimming (D18) unmeasured; `--max-budget-usd` (E11) unwired; `RunRes.model` is never populated by an adapter, so pricing uses the resolved pin; `resolveTeam` and `rosterAgents` in `src/resolve.ts` are v1 leftovers to drop with M3.4; codex and grok children have no engine MCP channel (M5.5); `preflight.worktree: new` and `project-selection: ask | none` are validated but not acted on.

## Sources

- Anthropic legal and compliance: https://code.claude.com/docs/en/legal-and-compliance
- Claude Agent SDK overview: https://code.claude.com/docs/en/agent-sdk/overview
- Claude Code headless mode: https://code.claude.com/docs/en/headless
- Agent SDK with your Claude plan (help center): https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- Third-party OAuth cutoff reporting: https://the-decoder.com/anthropic-cuts-off-third-party-tools-like-openclaw-for-claude-subscribers-citing-unsustainable-demand/
- Codex SDK README: https://github.com/openai/codex/blob/main/sdk/typescript/README.md
- Codex auth doc: https://learn.chatgpt.com/docs/auth
- Gemini CLI headless: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md
- Gemini CLI authentication: https://github.com/google-gemini/gemini-cli/blob/main/docs/get-started/authentication.mdx
- Grok Build CLI: https://x.ai/news/grok-build-cli and https://github.com/xai-org/grok-build
- xAI OAuth allowlist note: https://hermes-agent.nousresearch.com/docs/guides/xai-grok-oauth
- AI SDK 7: https://vercel.com/blog/ai-sdk-7 and https://ai-sdk.dev/docs/migration-guides/migration-guide-7-0
- AI SDK harness adapters: https://ai-sdk.dev/docs/ai-sdk-harnesses/harness-adapters
- AI SDK Anthropic provider: https://ai-sdk.dev/providers/ai-sdk-providers/anthropic
- AI SDK WorkflowAgent: https://ai-sdk.dev/docs/agents/workflow-agent
- Workflow DevKit on bun: https://community.vercel.com/t/vercel-workflows-cannot-run-on-bun-runtime/42291
- OpenCode providers (Anthropic OAuth removed): https://opencode.ai/docs/providers/
- Vibe Kanban: https://github.com/BloopAI/vibe-kanban
- Bun joins Anthropic: https://bun.com/blog/bun-joins-anthropic
- t3code: https://github.com/pingdotgg/t3code
- Claude Code CLI reference: https://code.claude.com/docs/en/cli-reference
- Claude Code model config and effort: https://code.claude.com/docs/en/model-config
- Nesting guard issue #32618: https://github.com/anthropics/claude-code/issues/32618
- Grok Build headless scripting: https://docs.x.ai/build/cli/headless-scripting
- xAI OAuth 403 in Hermes: https://github.com/NousResearch/hermes-agent/issues/26847
- ACP session config options: https://agentclientprotocol.com/protocol/session-config-options
- claude-agent-acp: https://github.com/agentclientprotocol/claude-agent-acp
- codex-acp: https://github.com/agentclientprotocol/codex-acp
- Gemini CLI ACP mode: https://geminicli.com/docs/cli/acp-mode/
- Grok Build ACP (Zed registry): https://zed.dev/acp/agent/grok-build

---

# Part 2: detailed design

Date: 2026-09-05. Builds on D1 to D14 and the E-rules above. Written as the proposal for the plan; kept in sync with the code as milestones close (see the M4-M6 build report for what differs).

## P1. Protocol

One JSON-RPC 2.0 vocabulary. The daemon serves it on a Unix socket; the MCP server exposes the same methods as tools with identical JSON shapes, so a harness call and a CLI call are indistinguishable to the daemon.

| Method / MCP tool | Params | Returns |
|---|---|---|
| `preflight` / `wise_preflight` | `{workflow, cwd}` | `{workflow, version, questions: Question[], defaults: Answers}` |
| `run` / `wise_run` | `{workflow, cwd, answers: Answers, context: Context, inputs: Record<string,string>}` | `{run_id, status: "running"}` |
| `wait` / `wise_wait` | `{run_id, after?: seq, timeout_ms}` | `{events: Event[], status, gate?: Gate, done: boolean}` |
| `answer` / `wise_answer` | `{run_id, gate_id, value}` | `{accepted: boolean}` |
| `status` / `wise_status` | `{run_id?}` | `RunSummary \| RunSummary[]` |
| `cancel` / `wise_cancel` | `{run_id, reason?}` | `{status: "cancelled"}` |
| `resume` (daemon, CLI) | `{run_id}` | `{run_id, status}` |
| `report` (daemon, CLI) | `{run_id}` | `{units: UnitRow[], usage: UsageByPool, verdicts}` |

`wise_wait` blocks until at least one new event past `after`, or a gate, or terminal status, or `timeout_ms` (cap 600 000, default 110 000 per D17; the daemon emits MCP progress notifications every 30 s while waiting). It returns compact events only (E1); logs never cross the wire.

```ts
type Question = {
  id: string;                         // "profile" | "tuning.plan" | "step-select" | "input.ticket_ids"
  kind: "choice" | "multi" | "text";
  label: string;
  options?: { value: string; label: string; description?: string }[];
  default?: string | string[];
};
type Answers = Record<string, string | string[]>;   // question id -> value
type Context = {                                    // harness-supplied (E1)
  ticket?: { ref: string; title?: string; body?: string; url?: string }[];
  guidance?: string;                                // operator free text
  decisions?: Record<string, string>;               // already settled in conversation
  links?: string[];
};
type Event = {
  seq: number; ts: string; run_id: string;
  type: "run.started" | "step.started" | "step.done" | "unit.phase" | "unit.done"
      | "usage" | "gate.opened" | "gate.answered" | "run.done" | "run.failed" | "warn";
  step?: string; unit?: string; phase?: string;
  verdict?: string;                                 // one line, <= 200 chars
  outputs?: Record<string, string | number | boolean>;
  usage?: Usage;                                    // on "usage" and "step.done"
  harness?: "claude" | "codex" | "cursor" | "gemini" | "grok"; model?: string; effort?: string;
};
type Gate = {
  gate_id: string; step: string; kind: "approval" | "ask";
  message: string;
  options?: { value: string; label: string }[];     // approval: approve/reject; ask: skip/confirm/custom
  allow_text?: boolean;
  expires_at?: string;                              // optional; run parks indefinitely when absent
};
type Usage = { input: number; output: number; cache_read: number; cache_write: number; cost_usd?: number; pool: "subscription" | "api-key" };
```

Errors follow JSON-RPC codes; domain errors carry `data.code` from a closed list: `WORKFLOW_NOT_FOUND`, `RUN_NOT_FOUND`, `GATE_STALE`, `HARNESS_UNAVAILABLE`, `AUTH_REQUIRED` (with `data.harness` and the login command to show the user), `BUDGET_EXCEEDED`, `DAEMON_VERSION_MISMATCH`.

## P2. Workflow YAML v2

`version: 2`. Additive over v1 where possible; the compiler rejects v1-only constructs with a migration hint.

```yaml
version: 2
name: ticket-auto
description: ...
project-selection: current

preflight:
  control-mode: synchronous          # gates still honoured; synchronous auto-approves `approval`
  worktree: current

tuning:                              # D12: one question per group
  groups:
    - id: plan
      label: "Plan phase"
      default: { harness: claude, model: opus, effort: high }
      fallback: [codex]              # E12: tried in order on HARNESS_UNAVAILABLE / rate limit
    - id: implement
      default: { harness: claude, model: opus, effort: high }
      fallback: [codex]
    - id: review
      default: { harness: claude, model: opus, effort: medium }
      locked: true                   # not asked; profile may still swap opus id under low
    - id: watch
      default: { harness: claude, model: sonnet, effort: low }

profiles:
  low:    { tuning: { plan: { model: claude-opus-4-8 }, implement: { model: sonnet } }, caps: { max_fix_attempts: 3, max_review_cycles: 2, tokens: 2_000_000 } }
  medium: {}
  max:    { caps: { tokens: 20_000_000 } }

inputs:
  - name: ticket_ids
    prompt: "Which tickets?"
    from-context: ticket[].ref       # E1: pre-filled from Context when present
  - name: config_prompt
    optional: true
    from-context: guidance

steps:
  - id: assemble
    type: agent                      # was `prompt`
    group: plan                      # binds harness/model/effort from the tuning answer
    prompt: ...
    schema: { type: object, properties: { team: { type: string } }, required: [team] }   # E9, replaces until/outputs
    outputs: [team]                  # names copied from the schema result into run outputs
    max_turns: 3                     # E11
  - id: process
    type: units                      # D14: engine-side loop, see P4
    pipeline: ticket                 # built-in pipeline id: ticket | plan
    items: "{{ticket_ids}}"
    groups: { plan: plan, implement: implement, review: review, fix: implement, watch: watch }
    caps: [max_fix_attempts, max_review_cycles]
    depends_on: [assemble]
  - id: report
    type: agent
    group: watch
    prompt: ...
    depends_on: [process]
    trigger-rule: all-done
```

Step types in v2: `agent` (any harness), `bash`, `approval`, `ask`, `units`. Removed: `prompt` (renamed `agent`), `interactive` and `supervised-prompt` (loops live in the engine; hang protection is the adapter's turn and wall-clock timeouts), `skill` (a Claude child loads the plugin, so `prompt: "Run /wise-commit"` covers it; the compiler offers `skill:` as sugar that emits that prompt with `harness: claude` forced). `until:` + `outputs:` regex capture is replaced by `schema:`; the compiler still accepts `until:` for one release with a deprecation warning.

Per-step overrides: `harness`, `model`, `effort`, `auth: subscription | api-key`, `fallback`, `mode: approval-required | auto | full-access` (T2), `resume: unit | fresh` (E8), `max_turns`, `timeout`.

## P3. Ledger

`~/.local/share/wise/runs/<cwd-slug>/<run-ulid>/`

```
state.json            # canonical, rewritten atomically (tmp + rename) on every change
events.jsonl          # append-only, seq-numbered; wise_wait reads from here
units/<branch>.json   # per-unit checkpoint (P4), replaces KEY=VALUE files
logs/<step>.<ulid>.raw.jsonl     # vendor stream verbatim (T4)
logs/<step>.<ulid>.log           # human-readable extract
```

```ts
type State = {
  version: 2; run_id: string; workflow: { name: string; version: number; dir: string };
  cwd: string; project: { path: string; name: string; kind: string } | null;
  harness_session?: string;                 // the driving harness's session id, advisory (was claude_session_id)
  status: "initializing" | "running" | "gated" | "paused" | "completed" | "failed" | "cancelled";
  answers: Answers; context: Context; resolved: Record<string, { harness: string; model: string; effort: string; reason?: string }>;
  caps: Record<string, number>; usage: Record<"subscription" | "api-key", Usage> & { by_harness: Record<string, Usage> };
  steps: Record<string, { status: string; step_run_id?: string; started_at?: string; completed_at?: string; verdict?: string; outputs?: Record<string, unknown>; cursor?: unknown; attempts: number }>;
  gate?: Gate; started_at: string; last_activity_at: string; completed_at?: string;
};
```

Rules kept from v1: 25-run history cap on terminal runs, run dir off the git tree, step re-run gets a fresh step ULID, resume resets `running` to `pending`. New: `gated` status; `cursor` per step for `--resume` (T3); usage totals by pool and harness (E14).

## P4. `units.ts` contract

One engine module runs the per-unit loop for both `ticket-auto` (unit = ticket) and `impl-plan-auto` (unit = plan file). Phases are the current §1 to §9, as code.

```ts
type Unit = { ref: string; branch: string; worktree: string; base: string; plan_path?: string; pr?: { number: number; url: string } };
type Phase = "claim" | "worktree" | "plan" | "implement" | "review" | "fix" | "push" | "pr" | "request-review" | "watch" | "cleanup";
type UnitLedger = {
  unit: Unit; last_phase: Phase; verdict?: "merged" | "all-green" | "blocked" | "partial" | "exhausted" | "human-intervention" | "failed";
  reason?: string; review?: { converged: boolean; cycles: number }; cleaned: boolean; blueprint?: string;
  cursors: Partial<Record<Phase, unknown>>;      // E8: review/fix share one cursor per cycle pair
  usage: Usage;
};
type PhaseFn = (u: UnitLedger, ctx: RunCtx) => Promise<PhaseResult>;
type PhaseResult = { ok: true; patch: Partial<UnitLedger> } | { ok: false; reason: string; verdict?: UnitLedger["verdict"] };
```

Phase behaviour, carried over from the prose with the engine now holding the loop:

- `claim` / `worktree`: idempotent. Ledger file present = this run owns the unit; worktree or branch on disk without a ledger = foreign, skip. Live `git` / `gh` state wins over the ledger.
- `plan`: `agent` call with the group's harness. Schema `{ plan_path, status: ready | insufficient-context | no-access, blueprint_path? }` (M4.2 tightened this from the draft `written | blocked`). `no-access` and `insufficient-context` map to `verdict: failed` with reasons `plan-no-access` or `plan-insufficient-context`.
- `implement`: schema `{ waves, tasks, done, failed }`. `done == 0` fails the unit. Parallel task executors are the child's own subagents (Claude) or sequential turns (Codex, Grok); the engine does not supervise them, it enforces `timeout` and `max_turns`.
- `review` then `fix`: cycle up to `max_review_cycles`. Reviewer returns `{ verdict: clean | issues, findings_path }`; fixer receives the findings path and the review's cursor (E8). Non-convergence pushes anyway with `review.converged = false`.
- `push`, `pr`, `request-review`: engine code around `git` and `gh`, no model. Failures are unit failures with reasons `push`, `pr`, `request-review`.
- `watch`: `agent` call on the `watch` group with `mode: full-access`; schema `{ verdict, copilot?, coderabbit?, review_fallback? }`. Engine enforces `max_fix_attempts` by counting the child's fix commits between polls, not by trusting the child.
- `cleanup`: only on `merged`. Worktree remove (force on leftovers), local branch delete, `cleaned: true`. Any other verdict keeps the worktree.

Concurrency: units run sequentially by default (context and rate-limit bounded). `units.parallel: N` allows N units at once, each in its own worktree, with the per-harness concurrency cap from P5 applied across them.

Events: `unit.phase` on every phase start with `{unit, phase, harness, model}`; `unit.done` with `{unit, verdict, reason}`; the final `report` step gets the whole `UnitRow[]` as data (E5), not as prose to reread.

## P5. Daemon lifecycle

- **Binary**: `wise-engined`, started by the MCP server or the CLI client when the socket is dead. Runs detached (`setsid`, stdio to a log file). One instance per user, guarded by `flock` on `~/.local/share/wise/engined.lock`.
- **Socket**: `$XDG_RUNTIME_DIR/wise/engined.sock`, falling back to `~/.local/share/wise/engined.sock`. Mode 0600. Version handshake on connect; mismatch returns `DAEMON_VERSION_MISMATCH` and the client asks the daemon to exit when idle, then restarts it.
- **Idle shutdown**: exit after 30 min with no active runs and no connected clients. Parked (`gated`) runs count as inactive; they resume from the ledger when a client returns.
- **Crash recovery**: ledger is truth. On start, every run with `status: running` gets its `running` steps reset to `pending` and its child processes are assumed dead (children are spawned in their own process group; the daemon records pgid in state and kills the group on cancel or restart).
- **Concurrency**: per-harness cap, default 2 concurrent children for `claude`, 1 for `codex`, `cursor`, `gemini`, `grok`, overridable in `~/.config/wise/engine.json`. Global cap 4. Queue is FIFO across runs.
- **Auth probe**: before a run starts, each harness it needs is probed (`claude auth status`, `codex login status`, `cursor-agent status --format json`, Gemini and Grok equivalents, or key env presence under `api-key`). A missing login raises `AUTH_REQUIRED` with the exact login command; nothing else starts (R1).
- **Rate limits**: a child result classified as rate-limited parks that harness's queue with exponential backoff (1, 2, 4, 8 min, cap 30) and emits `warn`; steps with a `fallback` list move to the next harness after the first backoff (E12).
- **Logs**: `~/.local/share/wise/engined.log`, rotated at 10 MB.

## P6. Adapter contract

```ts
type RunReq = { prompt: string; system?: string; model: string; effort?: Effort; schema?: object; cwd: string;
                mode: "approval-required" | "auto" | "full-access"; resume?: unknown; max_turns?: number; timeout_ms: number;
                auth: "subscription" | "api-key"; env?: Record<string, string> };
type RunRes = { text: string; json?: unknown; usage: Usage; cursor?: unknown; exit: "ok" | "error" | "rate_limited" | "auth" | "timeout" | "max_turns" };
type Adapter = {
  id: "claude" | "codex" | "cursor" | "gemini" | "grok";
  probeAuth(auth: RunReq["auth"]): Promise<{ ok: boolean; login_cmd?: string }>;
  run(req: RunReq, onEvent: (e: RawEvent) => void): Promise<RunRes>;
  effortMap(e: Effort): string | undefined;
};
```

Spawn under a clean env: `HOME`, `PATH`, `LANG`, the vendor config-dir var (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `CURSOR_CONFIG_DIR`, `GEMINI_CLI_HOME`, `GROK_HOME`) when set, the vendor key var only under `api-key`. `CLAUDECODE`, `CLAUDE_CODE_*`, `CLAUDE_*_SESSION*` are never inherited.

Effort mapping (`resolve` applies capability then policy clamps as today, then this table):

| wise | claude `--effort` | codex `model_reasoning_effort` | cursor | gemini | grok `--reasoning-effort` |
|---|---|---|---|---|---|
| low | low | low | none | none | low |
| medium | medium | medium | none | none | medium |
| high | high | high | none | none | high |
| xhigh | xhigh | xhigh | none | none | xhigh |
| max | max | max | none | none | max |

Structured output: Claude, Codex and Grok use native schema controls; Cursor and Gemini append a "reply with JSON matching this schema only" instruction and validate the extracted object. A parse failure is `exit: "error"`.

## P7. Conductor SKILL after the change

`/wise-workflow-run <name> [inputs]`:

1. Ensure `/wise-init` has run (bun or Node 24 present, `.mcp.json` server reachable). If `wise_status` fails, print the init command and stop.
2. `wise_preflight` → render questions with AskUserQuestion, one composite call for choices, defaults first. Skip questions with `locked: true` or already answered by the session profile.
3. Build `Context` from the conversation: ticket refs and bodies already fetched, operator guidance, decisions made. Call `wise_run`.
4. Loop: `wise_wait(after)`; render each event as one line; on `gate` ask the user and `wise_answer`; on `done` stop.
5. Final: `report` data rendered as the results table; usage by pool; unresolved units listed with worktree paths.

Everything else in today's SKILL (wave loop, dispatch rules, roster resolution, tuning parsing, log excerpt rules) is deleted or moves into the engine.

## P8. Bidirectional child channel (proposed, decide at M0.7)

Status: proposed, not accepted. M0.6 and M0.2 decide it.

Child to main. Every child is spawned with an MCP server injected: `wise-engine unit-mcp --token <per-step token>`, a stdio thin client to the daemon socket. Claude takes it via `--mcp-config` (user servers still load), Codex via `-c mcp_servers.wise=...`, Cursor through `.cursor/mcp.json`, Gemini via a settings override, Grok via `config.toml`. All five speak MCP, so one implementation covers them.

Tools offered to the child: `wise_report(kind: progress | blocker | decision | finding, text, data?)`, `wise_ask(question, options?, allow_text?)`, `wise_context(key)` (ticket body, plan, findings file, prior phase outputs fetched on demand instead of stuffed into the prompt), `wise_checkpoint(data)` (partial results survive a kill).

`wise_ask` blocks the child until answered. In an interactive run it becomes a gate to the harness, same `Gate` shape, resolved by `wise_answer`. In an autonomous run the daemon answers from `Context.decisions` and the workflow's policy block (max-value default, or fail the unit with `reason: needs-human`). One tool, both modes. The token is scoped to one step and unit, passed in env, over a 0600 socket, so a child cannot report into another step and there is no network surface.

Main to child. The daemon derives live status per child from the stream (current tool, turn count, tokens so far, last activity) and merges the child's own `wise_report` lines. It surfaces them as throttled `step.progress` events (one per 30 s or on state change) and in `wise_status` detail, so the harness sees "implement: turn 14, editing src/auth.ts, 48k tokens" instead of silence.

Steering is asymmetric. Claude children are spawned with `--input-format stream-json` so stdin stays open, letting the daemon inject a user message mid-run: a stale nudge, a cancel notice, or a `wise_ask` answer. Codex, Cursor, Gemini, and Grok have no open stdin in one-shot mode, so their steering is answer-via-tool-result only and a stale child is killed then rerun from its cursor (T3). A new harness tool `wise_nudge(run, step, message)` lets a human poke a running child.

This replaces `supervise-loop.md`, the `worker-heartbeat` and `stale-workers` commands, the `SUPERVISE=yes` env, and the findings-file dance (the fixer calls `wise_context("findings")`).

Cost: one extra stdio process per child and roughly 1k tokens of tool schemas in each child's context. Under E5, prompts stay pre-computed and `wise_context` is the escape hatch, not the default path.

Risk: `--input-format stream-json` combined with `-p` and `--json-schema` is documented but unproven here (M0.2). If it fails, Claude steering degrades to the same answer-only path as the other harnesses.

## Plan deltas

- A6 gains: protocol types (P1), YAML v2 compiler with v1 migration hints (P2), ledger v2 (P3).
- A8 gains: daemon per P5, adapter contract per P6.
- A12 becomes: implement `units.ts` per P4 for the `ticket` pipeline, then the `plan` pipeline; delete both prompt orchestrators.
- New A16 (see also plan M6.4): `wise-engine migrate <workflow.yaml>` rewrites v1 to v2 for user-authored workflows (`prompt` to `agent`, `until` to `schema` where the regex is a plain enum, otherwise leaves `until` with a warning).
- New A17: MCP tool descriptions and the conductor SKILL (P7) written together so tool names and event rendering match.

Implementation plan derived from this document: [docs/plans/harness-engine.md](../plans/harness-engine.md) (milestones M0-M6, task status).
