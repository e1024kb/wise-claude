# PLAN — wise Harness Engine

Master plan for replacing the Python + prose workflow engine with a TypeScript harness-adapter engine. Design record and decision log: [docs/wise/research-ts-engine.md](../wise/research-ts-engine.md) (D1-D14, E-rules, P1-P8, R1-R8). Status values: TODO → IN PROGRESS → DONE → DROPPED. Update this file first, then republish the artifact.

Created 2026-09-05 on branch `research/ts-engine-ai-sdk` at df467c6. Each milestone ends with a gate; a gate that fails stops the next milestone until the plan is revised.

## Summary

Steps run by spawning vendor CLIs headless (`claude -p`, `codex exec`, `gemini -p`, `grok -p`) under the user's subscription login or API key. A detached daemon owns runs and talks JSON-RPC to the harness through an MCP server. Orchestrator loops move from prose into engine code. TypeScript with erasable syntax runs as source on bun (preferred) or Node 24; tsgo, oxlint, oxfmt in `just check`; `node:test` suite.

## Milestones

| # | Milestone | SP | Depends on | Gate | Status |
|---|---|---|---|---|---|
| M0 | Spike | 8 | — | all probes pass, transport confirmed | DONE |
| M1 | Engine core (library + CLI, no execution) | 17 | M0 | ported tests green on bun and node | IN PROGRESS |
| M2 | Execution: claude adapter, daemon, MCP | 16 | M1 | `wise_run` of example-workflow completes via MCP from Claude Code | TODO |
| M3 | Conductor + ticket-plan end to end | 7 | M2 | ticket-plan on a real ticket, baseline recorded, Python deleted | TODO |
| M4 | `units.ts`: ticket-auto and impl-plan-auto | 12 | M3 | ticket-auto merges one ticket all-Claude | TODO |
| M5 | Harnesses: codex, grok, gemini, fallback | 9 | M2 | cross-harness ticket-auto run | TODO |
| M6 | Product: cost, profiles, docs, migrate, release | 9 | M4, M5 | v5.0.0 tagged | TODO |

Total 78 SP.

## M0 — Spike

Goal: settle the last unknowns before writing engine code. Every task records its result in the design doc § Spike answers.

| Id | Task | SP | Deps | Acceptance | Status |
|---|---|---|---|---|---|
| M0.1 | `claude auth login` in a terminal (user), then rerun the child probe: `claude -p --output-format stream-json --json-schema … --model haiku --effort low` with a clean env | 1 | — | Result has `is_error: false`, `structured_output` matching the schema, non-empty `modelUsage`, `total_cost_usd`; `system/init` size and first-turn `input_tokens` recorded (Q6) | DONE |
| M0.2 | `--input-format stream-json` with `-p`: keep stdin open, send a second user message mid-run, confirm it works together with `--json-schema` | 1 | M0.1 | Second message is answered in the same session; final result still schema-valid | DONE |
| M0.3 | `codex exec --json --output-schema --config model_reasoning_effort=high -s workspace-write` on bun under ChatGPT login, then resume the thread with a second call | 1 | — | JSON events parsed, schema output valid, no API key set, resume reuses the thread id | DONE |
| M0.4 | `gemini -p --output-format json` and `grok -p --output-format json --always-approve --no-auto-update` under cached logins; check `-m`, `--effort`, usage fields | 1 | — | Both return JSON; table of supported flags and usage fields written to the design doc | DONE |
| M0.5 | Minimal stdio MCP server in erasable TS, loaded into Claude Code with `--plugin-dir` via `.mcp.json`, one tool that blocks 4 minutes then returns | 2 | — | Tool call returns after 4 min; `MCP_TOOL_TIMEOUT` ceiling measured; result visible to the model | DONE |
| M0.6 | Child MCP injection: `claude -p --mcp-config <stdio server>`; child calls a `wise_report` tool during its turn | 1 | M0.1 | Daemon-side log shows the call with the step token; child's final result unaffected | DONE |
| M0.7 | Record results, decide go / no-go per transport, and confirm or drop P8 (child channel) | 0.5 | M0.1-M0.6 | Design doc updated; plan revised if any probe failed | DONE |

Gate M0: M0.1, M0.3, M0.5 pass. M0.2 and M0.6 failing downgrades P8 to answer-via-tool-result only.

2026-09-05: gate passed. All probes pass except the Gemini half of M0.4 (CLI installed, user login broken, probe moved to M5.3). Results and decisions D15-D18 in the design doc § Spike answers › M0 results. `wise_wait` default becomes 110 s (D17), P8 confirmed (D16), `--strict-mcp-config` on every Claude child (D18).

## M1 — Engine core

Goal: `plugins/wise/engine/` as a library plus a non-executing CLI, with the Python helper's behaviour ported and its 240 tests re-expressed in `node:test`. Nothing spawns a model yet.

| Id | Task | SP | Deps | Acceptance | Status |
|---|---|---|---|---|---|
| M1.1 | Scaffold: `package.json` (bun and node scripts), `tsconfig` with `erasableSyntaxOnly`, `allowImportingTsExtensions`, `verbatimModuleSyntax`, `noEmit`; `tsgo`, `oxlint`, `oxfmt` as dev deps; `just check` runs typecheck, lint, format check, tests; `engine.sh` picks bun else node; one smoke test | 2 | M0 | `just check` green on bun and on node 24 from a clean clone; no `dist/` | DONE |
| M1.2 | `defs`: YAML v2 loader, validator, locate with user-root shadowing, folder and flat form; v1 detection with migration hints per P2 | 3 | M1.1 | Bundled workflows fail validation with precise v1 hints until migrated; unit tests for every P2 field | IN PROGRESS |
| M1.3 | `resolve`: model families, capability and policy effort clamps, `WISE_EFFORT_CEILING`, retired-id swap, low-profile Opus rule, team resolution, per-harness effort map (P6) | 3 | M1.1 | `test_effort_ceiling`, `test_low_profile_model`, `test_tuning` ported 1:1 and green | IN PROGRESS |
| M1.4 | `scheduler`: waves from `depends_on`, all five trigger rules, `when:` as a real expression evaluator (`==`, `!=`, `&&`, `\|\|`, parentheses, unset handling) | 2 | M1.1 | `test_scheduler` ported; new tests for compound expressions | IN PROGRESS |
| M1.5 | `ledger`: run dir resolution (XDG), `state.json` atomic write, `events.jsonl` with `seq`, `units/*.json`, history cap prune, worktree-include copy | 2 | M1.1 | `test_state_lifecycle`, `test_prune_runs`, `test_worktree_include` ported | IN PROGRESS |
| M1.6 | `preflight`: questionary spec (`Question[]`, defaults) from profiles, tuning groups, step-select, inputs with `from-context` | 2 | M1.2 | `test_profile`, remaining `test_tuning` cases ported; spec snapshot test for ticket-plan | IN PROGRESS |
| M1.7 | `render`: `{{…}}` templating with `project.*`, `run.*`, `workflow.dir`, outputs | 1 | M1.5 | `test_render` ported | IN PROGRESS |
| M1.8 | CLI skeleton: `wise-engine preflight <wf>`, `compile-check <wf>`, `migrate <yaml>` (dry run) | 1 | M1.2, M1.6 | Commands run on bun and node; JSON output validated against P1 types | TODO |
| M1.9 | Parity report: table Python test → node:test, all 240 accounted for (ported, merged, or dropped with reason) | 1 | M1.2-M1.8 | Report in the design doc; `test_hook_contract`, `test_neutralization`, `test_robustness` decisions listed | TODO |

Gate M1: `just check` green on both runtimes, parity report complete.

2026-09-05: M1.1 scaffold landed at `plugins/wise/engine/` (bun 1.4.1 and node 24.18 both green via the npm scripts; `just` itself is not installed on the dev machine, root `justfile` gained `engine-check`).

## M2 — Execution

Goal: run a workflow through the daemon from Claude Code via MCP, Claude harness only.

| Id | Task | SP | Deps | Acceptance | Status |
|---|---|---|---|---|---|
| M2.1 | Adapter contract (P6) and `claude` adapter: clean-env spawn, `stream-json` parsing to normalised events, `--json-schema`, usage extraction, cursor (`session_id`), exit classification (`ok`, `error`, `rate_limited`, `auth`, `timeout`, `max_turns`), raw NDJSON log | 3 | M1 | Adapter tests with recorded fixtures from M0.1; live smoke on haiku | TODO |
| M2.2 | Daemon `wise-engined`: socket JSON-RPC server (P1 methods), flock single instance, version handshake, idle exit, detached start, pgid tracking, crash recovery resetting `running` → `pending` | 3 | M1.5 | Two clients cannot start two daemons; kill -9 then restart resumes a run from the ledger | TODO |
| M2.3 | Run executor inside the daemon: scheduler loop, step types `agent`, `bash`, `approval`, `ask`; gates park the run as `gated`; caps (`max_turns`, tokens); rate-limit backoff and `fallback` harness list | 3 | M2.1, M2.2 | `example-workflow` (migrated) runs to completion via the CLI; a gate parks and resumes on `answer` | TODO |
| M2.4 | MCP server `wise-engine mcp`: six tools per P1, long-poll `wise_wait`, compact events only (E1); `plugins/wise/.mcp.json` declares it | 2 | M2.2 | From Claude Code: `wise_preflight` → `wise_run` → `wise_wait` loop → `wise_answer` on a gate → done, with no log text crossing the wire | TODO |
| M2.5 | CLI client: `run`, `status`, `answer`, `cancel`, `resume`, `report` over the socket; auto-starts the daemon | 1 | M2.2 | Same run driven from a terminal without Claude Code | TODO |
| M2.6 | Child channel (P8, confirmed at M0.7, D16): `wise-engine unit-mcp --token`, tools `wise_report`, `wise_ask`, `wise_context`, `wise_checkpoint`; per-step tokens; `step.progress` events throttled; stale nudge via `--input-format stream-json` for Claude, kill + cursor resume for others; harness tool `wise_nudge` | 3 | M2.3, M0.6 | Child progress visible in `wise_status`; a `wise_ask` in an interactive run surfaces as a gate; a stale child gets nudged then killed per policy | TODO |
| M2.7 | Auth probe before run (`claude auth status`), `AUTH_REQUIRED` with login command; `/wise-init` and `bootstrap-deps.sh` updated: Node 24 minimum, bun preferred, `claude` login check | 1 | M2.2 | Logged-out state produces the exact login command in the harness, no run dir created | TODO |

Gate M2: example-workflow completes from Claude Code via MCP, gate round trip works, daemon survives restart.

## M3 — Conductor and first real workflow

| Id | Task | SP | Deps | Acceptance | Status |
|---|---|---|---|---|---|
| M3.1 | Migrate `example-workflow` and `ticket-plan` to YAML v2: `prompt` → `agent`, `until` → `schema`, tuning groups with harness, `from-context` inputs | 2 | M1.2 | `compile-check` passes; questionary spec matches today's pre-flight questions | TODO |
| M3.2 | Conductor rewrite per P7: `wise-workflow-run`, `-resume`, `-status` SKILLs on the MCP tools; delete wave-loop, dispatch, roster-resolution, log-excerpt prose; `Context` built from the conversation | 2 | M2.4 | SKILL under 5 KB; runs ticket-plan from the desktop app; gates rendered via AskUserQuestion | TODO |
| M3.3 | End-to-end ticket-plan on one real ticket; record baseline per E14: harness tokens, child tokens by harness, wall clock, transcript size, compared with the prose conductor on the same ticket | 2 | M3.1, M3.2 | Numbers in the design doc; no regression in plan quality by eyeball | TODO |
| M3.4 | Delete `workflows.py`, Python tests, `engine.py`, `engine.sh` Python shim; `justfile` and `validate_repo.py` drop pytest, add `just check` in the engine dir | 1 | M3.3, M1.9 | Repo has no `.py` under `plugins/wise/scripts` except hooks that stay; validator green | TODO |

Gate M3: ticket-plan works from Claude Code on the new engine, Python removed.

## M4 — Unit pipelines

| Id | Task | SP | Deps | Acceptance | Status |
|---|---|---|---|---|---|
| M4.1 | `units.ts` skeleton and the no-model phases: `claim`, `worktree`, `push`, `pr`, `request-review`, `cleanup` as code over `git` and `gh`; `UnitLedger` per P4; idempotent claim rules | 3 | M2.3 | Unit tests with a temp git repo; second run adopts, foreign worktree is skipped | TODO |
| M4.2 | Model phases: `plan`, `implement`, `review` ↔ `fix` loop, `watch`; prompts moved from `prompts/*.md` into engine prompt templates with schemas; caps enforced by the engine (fix commits counted) | 3 | M4.1 | Each phase runs against a fixture repo on haiku; review loop converges or pushes with `converged: false` | TODO |
| M4.3 | `ticket-auto` v2 with a `units` step; delete `process-tickets.md`; README regenerated | 2 | M4.2 | `compile-check` passes; README flow diagram matches | TODO |
| M4.4 | `impl-plan-auto` v2 on the `plan` pipeline; delete `process-plans.md` | 2 | M4.2 | Same as M4.3 | TODO |
| M4.5 | End-to-end ticket-auto on one ticket, all Claude, from Claude Code; `report` step consumes `UnitRow[]` as data | 2 | M4.3, M3.2 | PR merged or left open with the correct verdict; usage by phase in the report | TODO |

Gate M4: one ticket goes ticket → merged PR on the engine.

## M5 — More harnesses

| Id | Task | SP | Deps | Acceptance | Status |
|---|---|---|---|---|---|
| M5.1 | `codex` adapter: `codex exec --json`, `--output-schema`, `model_reasoning_effort`, sandbox mode from `mode`, thread resume cursor, `CODEX_HOME` passthrough | 2 | M2.1, M0.3 | Fixture and live smoke; `AUTH_REQUIRED` shows `codex login` | TODO |
| M5.2 | `grok` adapter: `grok -p --output-format json`, `--always-approve`, `--no-auto-update`, effort if confirmed, schema-by-instruction fallback | 2 | M2.1, M0.4 | Same | TODO |
| M5.3 | `gemini` adapter, best effort: `gemini -p --output-format json -m`, `--approval-mode yolo`, schema-by-instruction | 2 | M2.1, M0.4 | Same; documented as best-effort | TODO |
| M5.4 | Fallback lists end to end: force a rate-limit classification on claude, observe routing to codex after first backoff, event `warn` emitted | 1 | M5.1 | Test with a fake adapter; live check once | TODO |
| M5.5 | Cross-harness ticket-auto: plan on claude, implement on codex, review on claude, through one worktree | 2 | M4.5, M5.1 | Ticket reaches a PR; cursors and worktree handoff correct | TODO |
| M5.6 | `claude-session` adapter via the native Workflow tool (policy hedge, D4) | 3 | M2.3 | Postponed unless R2 materialises | DROPPED unless needed |

Gate M5: cross-harness run succeeds.

## M6 — Product

| Id | Task | SP | Deps | Acceptance | Status |
|---|---|---|---|---|---|
| M6.1 | Usage accounting: per step, phase, pool, harness in the ledger; api-key runs priced via LiteLLM table; `/wise-report` and the workflow `report` step show totals | 2 | M4.5 | Report shows tokens by pool and harness for a real run | TODO |
| M6.2 | Profile rule: `low` refuses `auth: api-key` steps unless `allow-api: true`; per-run token ceiling from `caps.tokens` parks the run at a gate | 1 | M2.3 | Tests; live check with a tiny ceiling | TODO |
| M6.3 | Docs sync: `docs/wise/workflows.md` v2 schema, harness field, gate protocol, effort mapping; `skills-authoring.md`; workflow READMEs; `AGENTS.md`; validator runs `compile-check` on every bundled workflow | 3 | M4.4 | `validate_repo.py` green with the new gate | TODO |
| M6.4 | `wise-engine migrate` for user-authored v1 workflows: `prompt` → `agent`, plain-enum `until` → `schema`, warnings otherwise | 2 | M1.2 | Round-trips the three bundled v1 files to the hand-migrated v2 result | TODO |
| M6.5 | Release v5.0.0: `plugin.json` bump, changelog, prose-conductor fallback behind a flag for one release, removal ticket filed | 1 | M6.1-M6.4 | Tag pushed; marketplace pin updated | TODO |

Gate M6: v5.0.0 released.

## Decisions carried into the plan

D1 harness adapters on unmodified vendor CLIs · D2 no AI SDK · D3 `claude -p`, never `--bare`, never a re-implemented OAuth client · D4 `claude-session` as policy hedge only · D5 native headless mode, not ACP · D6 t3code as plumbing reference · D7 bun preferred, Node 24 supported · D8 erasable TS as source, no build · D9 tsgo, oxlint, oxfmt · D10 `node:test` · D11 engine emits questionary, harness asks · D12 per tuning group + profile · D13 daemon + MCP JSON-RPC · D14 orchestrators into engine · D15 three transports go, Gemini best effort · D16 P8 confirmed · D17 `wise_wait` default 110 s · D18 `--strict-mcp-config` per child, further trimming measured in M2.1 · E1, E5, E7, E8, E9, E11, E12, E14 tokenomics rules. Pending: none after M0.7.

## Stop conditions

- A spike probe in M0 contradicts a decision (subscription auth fails for `claude -p` after login, MCP tool timeout below 60 s, child MCP injection impossible): stop, revise the design doc, re-plan M2.
- Anthropic policy text changes to forbid the unmodified-binary carve-out: stop M2.1, promote M5.6 to the primary Claude adapter.
- Parity report in M1.9 finds Python behaviour the engine cannot reproduce: stop M3.4 until resolved or explicitly dropped.

## Validation

- `cd plugins/wise/engine && just check` → typecheck, lint, format, tests all green on bun and on node 24
- `python3 scripts/validate_repo.py` → all OK lines, including `compile-check` per bundled workflow from M6.3 on
- `wise-engine compile-check plugins/wise/workflows/*/workflow.yaml` → exit 0
- From Claude Code: `/wise-workflow-run ticket-plan <ticket>` → run completes via `wise_wait`, no step log text in the transcript
