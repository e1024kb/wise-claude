# Plan: Python workflow engine and removal of legacy execution

Status: IN PROGRESS. P0 baseline capture is complete; P1 is underway on `feat/python-workflow-engine`.

Prepared 2026-09-11 from checkout `b1b5dee` (plugin `5.0.0-rc.6`). This plan supersedes the implementation direction in [harness-engine.md](harness-engine.md). The existing TypeScript implementation is the behavioral baseline; older milestone statuses are not evidence of current completion.

## Outcome and scope

Deliver two ordered workstreams:

1. Migrate the v2 engine to Python and remove TypeScript and legacy execution (P0-P5).
2. Make startup, initialization, and workflow control harness-agnostic for Codex, Claude, Cursor, and Grok (P6), then validate the combined release (P7).

The second workstream is required migration scope, not a follow-up enhancement. Shared engine logic remains identical across hosts; only registration and interaction adapters may differ.

Port the current v2 harness-adapter engine to Python, retain its daemon, MCP interfaces, workflow semantics, and CLI integrations, then make it the only workflow execution engine. Remove the TypeScript implementation, its Bun/Node tooling, and the v1 Python/prose conductor. This is not a return to the old Python scheduler.

The engine, plugin bootstrap, validation, and tests must work without `bun`, `node`, `npm`, `npx`, or TypeScript tooling. External harness executables and user-authored workflow commands remain external dependencies: a selected vendor CLI or project command might itself require Node. Removing those requirements would require changing providers or user projects and is outside this migration. Wise must not require Node globally or install it for its engine.

This document is the only change in the planning stage. No runtime changes, dependency installation, live workflow runs, data migration, commits, or releases are part of this stage.

## Findings from the current checkout

| Area | Evidence and migration consequence |
|---|---|
| SDK premise | `engine/package.json` declares three runtime packages: `@modelcontextprotocol/sdk`, `yaml`, and `zod`. The MCP SDK is actively imported by `src/mcp.ts` and `src/unit-mcp.ts`. There is no model-provider SDK dependency; all five adapters invoke CLI processes. Python removes the TypeScript SDK requirement, not MCP itself. |
| New engine | `plugins/wise/engine/src/` contains 49 TypeScript files. It includes validation, staged preflight, scheduling, ledger, daemon/RPC, MCP servers, adapters, units, phases, permissions, and reporting. This is a substantial behavioral port. |
| Tests | `engine/test/` contains 33 `*.test.ts` files plus fixtures. These are the primary regression inventory. Existing pytest coverage also contains shared plugin behavior that must survive removal of the old engine. File counts are not test-case counts. |
| Bundled definitions | All five bundled workflows already declare `version: 2`: `ticket-plan`, `ticket-auto`, `impl-plan-auto`, `example-workflow`, and `code-review`. They do not need another schema rewrite. |
| Runtime launch | `.mcp.json` invokes `engine/engine.sh mcp`. That wrapper installs runtime packages into the plugin tree and chooses Bun or Node 24+. Daemon and child MCP launch paths must also change, not only this wrapper. |
| Host launch failure | Operator-provided Codex diagnosis: `.mcp.json` passes literal `${CLAUDE_PLUGIN_ROOT}/engine/engine.sh` to Bash; startup fails before MCP initialization. Using the actual install path reportedly completed a handshake and exposed eight tools. A restart cannot fix an unresolved path. Reproduce this in P6 rather than treating the earlier restart-only advice as a remedy. |
| Legacy execution | `wise-workflow-run` falls back to `references/legacy-conductor/run.md` for v1 definitions. Resume/status retain legacy state paths. `scripts/workflows.py` still provides a second state machine and scheduler. |
| Shared legacy helpers | Profiles, session discovery, reports, standalone supervision, insights path lookup, workflow creation/listing, and validation still reference `workflows.py`. Deleting the file first would break unrelated public skills. |
| Other engine-named files | `scripts/engine.py` and `scripts/engine.sh` emit the skill catalog. They are not another workflow executor and should remain unless a direct integration change is required. |
| Toolchain | Root `just check` includes `engine-check`; CI installs Node 24; `scripts/validate_repo.py` invokes v2 compile-check but also imports the old Python engine. Bootstrap and init contain Node/Bun probes and registry fields. |
| Documentation drift | `docs/wise/workflows.md` describes the new engine, while parts of `AGENTS.md`, `CONTRIBUTING.md`, and plugin guidance still describe the old in-conversation-only conductor. Reconcile these during cutover. |

Relevant code: [engine sources](../../plugins/wise/engine/src/), [legacy helper](../../plugins/wise/scripts/workflows.py), [workflow reference](../wise/workflows.md), [validator](../../scripts/validate_repo.py), and [previous research](../wise/research-ts-engine.md).

## Target architecture and dependencies

Keep `plugins/wise/engine/engine.sh` as the public entry point. Replace its implementation with a Python launcher. Put the package at `plugins/wise/engine/wise_engine/`, with `__main__.py` and modules grouped around the existing responsibilities: definitions/preflight, scheduler/rendering, ledger, protocol/daemon/client, MCP, adapters, and units/phases. Keep prompts as package resources. Do not translate the old monolithic `workflows.py` into the new package wholesale.

Use Python 3.11+ as the initial minimum, matching current CI. Use `asyncio` for Unix sockets, subprocess streams, long polling, cancellation, timers, and concurrency limits. Use standard-library JSON, paths, atomic replacement, process groups, and CLI parsing where appropriate. Target macOS and Linux, matching the current Unix-socket architecture; adding Windows support is outside scope.

Dependency decisions to lock in P1:

- Replace the TypeScript MCP SDK with the official Python `mcp` package. Preserve explicit tool schemas and result envelopes rather than accepting framework-generated interface changes. Select and pin a stable release after the MCP spike; do not automatically adopt a prerelease or hand-write MCP transport. [Official Python SDK](https://github.com/modelcontextprotocol/python-sdk).
- Use `ruamel.yaml` for YAML 1.2 loading and the existing comment-preserving definition migration command. Its round-trip loader preserves comments; prove behavior on this repository's fixtures. Do not assume PyYAML's scalar coercion matches the current `yaml` package. Existing PyYAML use in the separate skill catalog can remain. [Official YAML API](https://yaml.dev/doc/ruamel.yaml/api/).
- Use typed dataclasses/TypedDicts and explicit validators for engine domain objects. Use the MCP SDK's schema facilities where needed, without changing unknown-key warnings, strictness, or absent/null handling. Do not expand accepted workflow schemas as a side effect of the port.
- Retain `python-ulid` if needed to preserve current run identifiers. Audit actual imports before removing `typing_extensions` or other existing Python dependencies.
- Development tools: pytest, Ruff, and mypy. Avoid introducing a Node-backed type checker. Add async test support only where tests need it.

Declare Python dependencies in `engine/pyproject.toml` and commit reproducible runtime/development dependency locks or hashed requirements consumed by pip. Use a plugin-managed virtual environment under plugin data, keyed by Python version and dependency lock hash. Keep the entry point usable from any cwd. No global pip mutation, mandatory uv installation, or writable plugin-cache assumption. A source package with resource lookup preserves the repository's no-build workflow.

The bootstrap must be idempotent and safe under simultaneous MCP startups: lock environment creation, publish it only after installation succeeds, and recover from partial installations. `--probe` remains read-only. Diagnostics go to stderr during MCP startup; stdout contains only protocol messages. Daemon and child MCP processes use the same selected interpreter, never an ambient `python3` that lacks dependencies.

## Workstream 2 design: harness-agnostic startup and init

Distinguish the host running the conductor from the provider CLI executing a child step. A successful Claude CLI MCP probe does not establish that Codex, Cursor, or Grok can see Wise tools. Installing Claude or logging into Claude must not be a global prerequisite when the selected workflow uses another available provider. Preserve Gemini child execution support from workstream 1; this workstream explicitly targets Codex, Claude, Cursor, and Grok as conductor hosts.

Use one canonical Python launcher and one engine. Resolve package resources relative to the installed launcher/package, not the project cwd or a Claude-only environment variable. Host registration must locate the launcher before execution: fixing path resolution inside `engine.sh` cannot help when the host cannot find that script. Keep `engine/engine.sh` as a compatible entry point where useful; it is not a universal host registration mechanism.

In P6, inspect each host's actual registration contract and choose supported launch metadata or a stable managed launcher. Where configuration needs an absolute path, resolve it during installation/registration and refresh it automatically after upgrade or relocation. Do not ship a user-specific path, a versioned cache-path override requiring manual updates, or an assumption that JSON arguments expand environment variables. Keep arguments as argument arrays with correct space/Unicode handling; do not add `bash -c` interpolation as the generic fix.

Host-specific registration adapters may write the host's supported configuration, preserving unrelated servers and settings. Make registration idempotent, changes reviewable, and rollback possible. A registration probe must work even when Wise MCP is unavailable, so init can diagnose and repair the setup needed to expose its own tools. Avoid depending on the missing MCP server to bootstrap that server.

Init reports separate results for runtime dependencies, launcher resolution, server process startup, MCP handshake, tool discovery, daemon health, host-session availability, and selected-provider authentication. Include the failing stage and actionable error without exposing credentials. Recommend restart/reload only after configuration and handshake pass and that host actually requires reload. A literal unresolved root placeholder is a configuration failure, not `restart-needed`.

Cache results by host identity, resolved installation/build, and relevant configuration fingerprint. Shared dependency results may be reused; host connectivity cannot be inferred from another host or an older init registry. Replace the plugin-cache-local/Claude-only registry assumption with a documented host-neutral data location and migrate or invalidate old records. Keep user roots and run-history paths compatible with workstream 1.

Use MCP elicitation where supported, otherwise the host's native structured picker. If neither is available, provide an explicit interactive CLI route to the same Python engine. Preserve every required step-selection, provider-permission, harness, model, and effort choice; do not fill defaults, grant permissions, or execute unresolved choices automatically. A transport fallback must never reactivate the legacy conductor. Update the workflow skills so missing host MCP triggers accurate setup diagnostics or the supported interaction route, not an unconditional init/restart loop.

Do not assume all four hosts provide identical MCP or UI features. Record supported versions and verified capabilities during implementation. Any native capability gap must have a tested equivalent route to the same engine before claiming that host is supported.

## Contracts to preserve

Before porting, inventory exact contracts from source and tests, not just documentation:

1. CLI commands and flags: `preflight`, `compile-check`, `migrate`, `list-defs`, `run`, `wait`, `status`, `answer`, `cancel`, `resume`, `report`, `daemon`, `mcp`, `unit-mcp`, `auth`, `models`, `dispatch`, `version`, and `help`. Preserve JSON output, text mode, exit codes, and stderr separation. Runtime/build identity changes intentionally.
2. Parent MCP tools: `wise_preflight`, `wise_run`, `wise_wait`, `wise_answer`, `wise_status`, `wise_cancel`, `wise_nudge`, and `wise_resume`. Child tools: `wise_report`, `wise_ask`, `wise_context`, and `wise_checkpoint`. Preserve names, schemas, error envelopes, cancellation, progress notifications, and wait bounds.
3. JSON-RPC framing, request IDs, handshake, domain error codes, socket permissions, startup races, stale lock handling, host disconnect behavior, idle shutdown, build refresh, and log rotation.
4. Staged preflight: step selection, inputs, then applicable harness/model/effort questions. Installed binaries and authenticated sessions are different facts. Interactive execution must reject unresolved required choices with `MISSING_ANSWERS`; defaults are not user answers. Preserve scripted CLI behavior separately.
5. Definition precedence, folder/flat layouts, context rendering, condition evaluation, trigger rules, gates, fallback, concurrency ceilings, profiles, effort policy, permissions, and usage accounting. Preserve all currently supported v2 step types and unit phases.
6. Adapter behavior for Claude, Codex, Cursor, Gemini, and Grok: executable identity, arguments, environment isolation, subscription/API-key routing, parsing, session resume, schema handling, bounded stderr, timeout, and process-tree termination. Preserve the distinction between Cursor-hosted models and standalone Grok.
7. Persistence: current XDG and plugin-data precedence, cwd slug, ULIDs, `state.json`, `events.jsonl` sequence numbers, unit checkpoints, logs, context files, and worktrees. Python must read existing v2 run directories without rewriting history or repeating completed side effects.

Python translation traps require explicit fixtures: missing versus null, booleans versus integers, JavaScript truthiness, YAML timestamps and `on`/`off`, Unicode chunk boundaries, milliseconds versus seconds, regex behavior, path symlinks, JSON number handling, and object ordering where visible to users.

## Legacy removal and data policy

There will be one production executor after cutover. Temporary TypeScript/Python comparison during implementation uses isolated test roots and sockets. It is not a user-selectable runtime switch and must not survive release.

| Current surface | Final treatment |
|---|---|
| `engine/src/**/*.ts`, TypeScript tests and helpers | Replace with Python implementation/tests, retain language-neutral fixtures and prompts, then delete TypeScript files. |
| `engine/package.json`, `bun.lock`, `tsconfig.json`, JS lint/format setup | Delete after parity. Remove local `node_modules` from test/install assumptions; no global package uninstall. |
| `scripts/workflows.py` v1 execution/state commands | Delete after all supported callers move. No compatibility scheduler, dispatch flag, or hidden fallback. |
| `references/legacy-conductor/` | Delete all six files and every executable reference. |
| Workflow run/resume/status skills | Use only v2 engine interfaces. Return explicit unsupported-v1 guidance instead of invoking prose execution. |
| Workflow create/list/remove skills | Use canonical v2 discovery/validation and generate v2 definitions. Keep folder/flat discovery because storage layout is independent of schema version. |
| Profiles/session lookup/roster/path helpers | Move only still-used behavior to focused Python helpers or the canonical engine CLI. Update all consumers and tests before deleting their old owner. |
| Reports | Replace `state.yaml` assumptions and legacy run commands with v2 status/report data. Preserve standalone session reporting. |
| Standalone supervision | Preserve `/wise-supervise` and standalone implementation supervision. Relocate heartbeat/config helpers without a workflow scheduler, then update `supervise-loop.md`, executor instructions, and the bundled implementation prompt. |
| Insights and skill catalog | Preserve independent behavior. Move insights' optional `workflows` path import to a stdlib-only shared path helper; keep hook startup free of engine/MCP dependency imports. Keep catalog scripts. |
| Validator and pytest fixtures | Remove legacy engine imports and v1 scheduler checks; validate all bundled definitions through Python. Retain or move non-engine regressions, rather than deleting the entire old pytest directory. |

Existing v2 runs must remain readable and resumable. Preserve their storage schema unless P0 finds an unavoidable incompatibility; that would require an explicit versioned migration and a revised gate before proceeding.

Existing v1 definitions may be converted through the Python `migrate` command. Preserve dry-run default, `--out`, backup behavior for `--write`, warnings/manual notes, comment preservation, and validation exit status. This converter is an import utility, not a second execution solution.

Do not convert v1 `state.yaml` runs into resumable v2 runs automatically: steps, teams, and side effects do not map safely. Preserve their files unchanged. Report them as unsupported legacy runs with guidance to migrate the definition and start a new run after reviewing already-completed side effects. Read-only legacy detection does not authorize execution. Do not delete historical runs, user definitions, or worktrees as part of code removal.

## Implementation sequence

Each phase updates its checklist here with actual validation evidence. A failed gate blocks dependent work. Keep changes scoped; do not mix provider features or scheduler redesign into parity work.

### P0 - Freeze baseline and contracts

- [x] Record implementation-start commit and clean/dirty status; rerun source inventory because this planning snapshot may drift.
- [x] Run existing `just check`; distinguish pre-existing failures from migration regressions.
- [x] Map all 33 TypeScript test files and legacy pytest cases to port, retain, merge, or retire, with reasons. Record the mapping in this plan during implementation.
- [x] Capture normalized CLI, RPC, MCP, questionary, adapter, and ledger fixtures. Normalize only nondeterministic IDs, paths, PIDs, and times.
- [x] Inventory every `workflows.py` caller and shared export, including tests, skills, hooks, documentation examples, and validator imports.

Gate: baseline failures documented, contract fixtures captured, and every legacy caller has a named destination or retirement reason. Tests must not contact production trackers or push/merge repositories.

### P1 - Python skeleton and transport spike

Depends on P0.

- [ ] Add package, dependency declarations/locks, and development commands; keep production entry point on TypeScript for now.
- [ ] Implement isolated Python parent/child MCP prototypes with the existing schemas, including long polls, cancellation, host disconnect, progress, and structured errors.
- [ ] Prove subprocess streaming and process-group shutdown on macOS and Linux.
- [ ] Prove managed-environment bootstrap from a read-only plugin copy without JavaScript tools on PATH, including concurrent startup and failed-install recovery.
- [ ] Confirm Python minimum and exact dependency versions; record them here.

Gate: fixture MCP clients connect and exercise both servers; framing and tool schemas match. No provider login or model spending is required for this gate.

### P2 - Pure engine logic and persistence

Depends on P1.

- [ ] Port definitions, validation, rendering, scheduler, models/resolution, profiles, permissions, pricing, paths, preflight, and context resources.
- [ ] Port ledger, events, unit checkpoint storage, pruning safeguards, and worktree inclusion.
- [ ] Port v1 definition converter with round-trip fixtures and manual-warning semantics.
- [ ] Add non-executing CLI commands and necessary shared helper destinations.
- [ ] Compare outputs against P0 fixtures and the TypeScript implementation using isolated data roots.

Gate: every bundled workflow compiles; staged preflight and migration fixtures match; saved v2 runs load; event order and atomic-write recovery tests pass. No v1 executor is introduced.

### P3 - Adapters, daemon, and execution

Depends on P2.

- [ ] Port all five adapters and authentication probes, plus standalone dispatch/model commands.
- [ ] Port RPC client/server, daemon lifecycle, startup arbitration, build identity, host watching, and long polling.
- [ ] Port agent/bash/gate execution, fallback, retries where currently supported, cancellation, and concurrency controls.
- [ ] Port units and all phases: claim, worktree, plan/implementation/review/fix/watch, push, PR, review request, and cleanup as implemented by current sources.
- [ ] Connect both MCP servers and child channel to the real Python executor; preserve token scoping, checkpoint semantics, and permission filtering.
- [ ] Replace TypeScript fake child programs with Python equivalents; use local disposable Git repositories and mocked GitHub/tracker responses for side-effect tests.

Gate: behavioral parity across lifecycle, channels, permissions, adapter failures, and unit loops. Crash/restart tests prove completed phases are not replayed and timed-out child trees are reaped. Fixture success must not be reported as live provider verification.

### P4 - Route every caller and remove v1 execution

Depends on P3.

- [ ] Switch the public `engine/engine.sh` to Python; keep `.mcp.json` server identity and command contract stable.
- [ ] Update daemon self-launch and all child MCP injection to the managed interpreter/package path.
- [ ] Migrate workflow skills, profiles, reports, shared references, supervisor helpers, insights import, and validator as listed in the removal table.
- [ ] Remove legacy skill fallback branches, `references/legacy-conductor/`, and `scripts/workflows.py` after caller checks pass.
- [ ] Replace global Node/Bun requirements in bootstrap, init wizard, registry, and shared init references. Recognize older registry records without treating a stale success marker as proof that Python engine dependencies exist.
- [ ] Exercise every affected public skill's command path, not only workflow-run.

Gate: exactly one reachable workflow executor. `/wise`, profiles, insights, reports, supervision, authoring, discovery, run/resume/status all retain their supported behavior. v1 runs never silently execute.

### P5 - Delete TypeScript tooling and reconcile documentation

Depends on P4.

- [ ] Delete TypeScript implementation/tests after the parity map accounts for their behavior. Keep JSON/YAML/transcript fixtures needed by Python tests.
- [ ] Delete JS manifests/lockfile/configuration and remove Bun/npm commands from root and engine justfiles.
- [ ] Remove Node setup from CI; install pinned Python dependencies and run Python lint, type checks, tests, structural validation, JSON parsing, and shell syntax checks. Include `engine/engine.sh` in syntax coverage.
- [ ] Update `scripts/validate_repo.py` and test discovery so zero collected tests fails CI instead of being accepted.
- [ ] Reconcile root/plugin AGENTS guidance, CLAUDE guidance, CONTRIBUTING, READMEs, `docs/wise/workflows.md`, and affected skills/references. Update bundled workflow READMEs when their prompts/instructions change.
- [ ] Mark earlier engine plan/research as superseded historical records with a link here; remove stale operational instructions from active documentation.
- [ ] Bump the plugin version per repository policy. Explicitly document retirement of v1 execution and removed CLI contracts; do not treat it as a transparent patch release.

Gate: clean install and full `just check` pass in a Python-only environment with JavaScript executables absent. Repository searches find no engine-owned JS runtime requirement or executable legacy reference. Historical records and commands belonging to user projects are reviewed exceptions, not blanket failures.

### P6 - Harness-agnostic launch, init, and workflow control

Depends on P5. This is workstream 2, after the Python migration.

- [ ] Reproduce the reported Codex literal-placeholder startup failure and capture a regression fixture. Confirm that restart alone leaves it broken and that a resolved launcher reaches MCP initialization.
- [ ] Inspect and document registration, variable expansion, cwd, environment, reload, MCP elicitation, and native picker support for Codex, Claude, Cursor, and Grok. Record actual supported versions in this plan.
- [ ] Implement the shared Python launcher resolution and minimal host registration adapters. Cover fresh installs, read-only caches, spaces/Unicode paths, symlinks, relocation, and version-changing upgrades without manual path edits.
- [ ] Update init scripts/skill, registry ownership and invalidation, plugin MCP configuration, installation instructions, and workflow run/resume/status skills. Remove Claude-only assumptions from conductor setup and dependency requirements.
- [ ] Add staged diagnostics for unresolved launcher, startup exit, dependency failure, handshake failure, missing tools, unavailable session tools, daemon failure, and selected-provider login. Preserve previously chosen optional-dependency skips.
- [ ] Test each host configuration launching the real Python MCP server: initialize, list tools, and call `wise_status`. A subprocess started directly with an absolute path is a diagnostic control, not proof the host configuration works.
- [ ] Test preflight, run, wait, approval/question answers, cancel, and resume through each host's supported interaction route against disposable fixtures. Verify all eight parent tools and four child tools where those respective interfaces are used.
- [ ] Test launching from a non-Claude host with Claude absent and a different provider selected. Verify conductor host and child-provider selection remain independent.
- [ ] Test registration update/rollback without disturbing unrelated host configuration, active runs, or saved history. Verify stale registry records cannot hide launch failures.

Gate: Codex, Claude, Cursor, and Grok can each control a workflow through a documented, tested route to the same Python engine. The Codex regression passes through actual host registration. No manual versioned-path override, Claude login requirement for unrelated providers, or restart-only recovery loop remains. Record unavailable host access as an unmet gate, not a pass.

### P7 - Upgrade rehearsal and release readiness

Depends on P6.

- [ ] Rehearse upgrade using copies of existing v2 state, events, gated/paused runs, checkpoints, and configuration. Verify read/resume compatibility and context-path behavior.
- [ ] Rehearse v1 definition import and unsupported-v1-run handling; original files remain unchanged unless conversion was explicitly requested.
- [ ] Rehearse daemon replacement with active and idle old daemons. Never run both implementations against the same socket/ledger.
- [ ] Run end-to-end MCP/CLI workflows in disposable projects: success, approval, question, cancel, failed-step resume, fallback, child checkpoint, and units pipeline.
- [ ] Verify the four-host matrix from P6, and at least one live child-provider execution when credentials and execution authorization are available. Keep host transport proof separate from paid/live model execution; record unavailable access as an unmet gate, not inferred success.
- [ ] Confirm final test mapping, dependency inventory, changed public contracts, and rollback rehearsal in this plan.

Gate: all acceptance criteria below pass. Publishing, pushing, tagging, merging, or running real ticket automation remains a separate release action.

## Cutover and rollback

Before switching a deployed installation, stop accepting new work on its old daemon. Allow active children to finish, or explicitly cancel and confirm termination before resuming saved work through Python. Do not force takeover of a live PID/socket or delete a lock just because the implementation changed. Build identity must distinguish Python from TypeScript so persistent host MCP sessions reconnect correctly.

Take a snapshot of affected run/configuration data only after writers have stopped. Test rollback using copies: stop the Python daemon, restore the previous plugin revision and dependency environment, and verify compatible state before resuming. Prefer rollback without replacing run data when it remains compatible. Restoring an older snapshot after external Git/tracker actions requires reconciliation first, because restoring files does not undo external side effects.

Rollback uses a previous release, not a second engine shipped in the new release. Development comparison environments must never share production run roots. No automatic uninstall of Bun/Node or deletion of user data is included.

## Final acceptance checklist

- [ ] Python is the only shipped workflow executor; v1 conductor and TypeScript engine are gone.
- [ ] Engine startup, MCP servers, CLI, bootstrap, validation, and tests work without JavaScript tools.
- [ ] Codex, Claude, Cursor, and Grok each launch/control the same Python engine through tested host integration, including required interactive choices.
- [ ] Init distinguishes configuration errors from reload requirements; the literal `${CLAUDE_PLUGIN_ROOT}` regression is covered and no manual versioned-path override is required.
- [ ] Installation upgrades refresh host registration and invalidate host-specific probe caches without losing unrelated settings or run history.
- [ ] All five v2 workflows validate and their supported execution paths pass fixture integration tests.
- [ ] Parent/child MCP schemas, CLI outputs, error codes, preflight ordering, and permission behavior remain compatible except documented retirements.
- [ ] Existing v2 history/checkpoints remain usable; restart/resume never blindly repeats completed side effects.
- [ ] No runnable v1 fallback remains. Import assistance preserves definitions and legacy run history.
- [ ] Shared skills, insights hook, catalog, reports, profiles, and standalone supervision still work.
- [ ] Every previous test area is accounted for; full checks pass on macOS and Linux, including Python-only clean-install coverage.
- [ ] Active documentation describes one architecture, and the release notes state removed contracts and runtime requirements.

## Evidence limits for this planning stage

Repository source, manifests, tests, callers, and existing design documents were inspected. The official Python MCP SDK and YAML round-trip documentation were checked for replacement feasibility. No full test suite or live provider run was executed during planning; no performance or parity result is claimed. Dependency versions and live host behavior are intentionally implementation gates, not assumed facts.

The Codex launch failure and successful absolute-path handshake were supplied by the operator after the initial plan. They are recorded as reported evidence; this plan update does not claim an independent reproduction or verified host compatibility.

## Implementation evidence

### Baseline, 2026-09-11

- Branch: `feat/python-workflow-engine`, using the existing worktree at base `b1b5dee0ae8b2003aa5f7fda8f78c5a35d102676`. The only initial untracked file was this plan. The operator authorized native Codex agents, local commits on this branch, and no push.
- `just check`: structural validation passed; the pytest recipe stopped because the active interpreter did not have pytest. No source failure was observed.
- Equivalent Python test run in an isolated uv environment: `uv run --with pytest --with pyyaml --with python-ulid --with typing_extensions python -m pytest plugins/wise/tests -q`: 243 passed.
- `cd plugins/wise/engine && bun run check`: typecheck and formatting passed, lint emitted warnings without errors, 656 tests passed and 5 were skipped (661 total).
- Contract captures under `engine/test/fixtures/contracts/`: parent/child tool listings obtained through initialized MCP clients; protocol constants; CLI output/exit samples; all five bundled definitions with initial/partially answered questionaries; v2 state and event samples; JSON-RPC framing/error examples. Existing adapter transcripts and migration fixtures remain baseline inputs. These are samples alongside the source test inventory, not a claim of exhaustive parity.
- Optional `code-simplifier` agent is not exposed by this session's agent interface. The implementation uses the skill's unavailable-agent fallback and does not claim a simplifier pass.

### P0 test and caller disposition

All destinations are planned, not completed ports. Retain all current TS test cases and skips until equivalent Python assertions pass.

| TypeScript test | Python destination under `engine/tests/` |
|---|---|
| `adapters.claude.test.ts` | `test_adapters_claude.py` |
| `adapters.codex.test.ts` | `test_adapters_codex.py` |
| `adapters.cursor.test.ts` | `test_adapters_cursor.py` |
| `adapters.gemini.test.ts` | `test_adapters_gemini.py` |
| `adapters.grok.test.ts` | `test_adapters_grok.py` |
| `adapters.spawn.test.ts` | `test_adapters_spawn.py` |
| `channel.test.ts` | `test_channel.py` |
| `cli-client.test.ts` | `test_cli_client.py` |
| `cli.test.ts` | `test_cli.py` |
| `context-files.test.ts` | `test_context_files.py` |
| `daemon.test.ts` | `test_daemon.py` |
| `defs.test.ts` | `test_defs.py` |
| `dispatch.test.ts` | `test_dispatch.py` |
| `executor.test.ts` | `test_executor.py` |
| `host-watch.test.ts` | `test_host_watch.py` |
| `integration.test.ts` | `test_integration.py` |
| `ledger.test.ts` | `test_ledger.py` |
| `mcp.test.ts` | `test_mcp.py` |
| `migrate.test.ts` | `test_migrate.py` |
| `model-phases.test.ts` | `test_model_phases.py` |
| `permissions.test.ts` | `test_permissions.py` |
| `phases.test.ts` | `test_phases.py` |
| `preflight.test.ts` | `test_preflight.py` |
| `pricing.test.ts` | `test_pricing.py` |
| `profile.test.ts` | `test_profile.py` |
| `render.test.ts` | `test_render.py` |
| `resolve.test.ts` | `test_resolve.py` |
| `rpc.test.ts` | `test_rpc.py` |
| `scheduler.test.ts` | `test_scheduler.py` |
| `smoke.test.ts` | `test_smoke.py` |
| `steps.test.ts` | `test_steps.py` |
| `unit-mcp.test.ts` | `test_unit_mcp.py` |
| `units.test.ts` | `test_units.py` |

Legacy pytest mapping (13 test files plus conftest):

| File | Treatment |
|---|---|
| test_effort_ceiling.py | Merge into engine test_resolve.py; retain reason distinctions and overrides. |
| test_hook_contract.py | Retain independent plugin hook tests. |
| test_init_sh.py | Retain and extend host-specific init tests in P6. |
| test_low_profile_model.py | Merge into resolve/preflight; v1-only syntax becomes importer coverage. |
| test_neutralization.py | Merge into profile/ledger and shared stdlib path tests. |
| test_profile.py | Merge into canonical profile tests; preserve standalone skill integration. |
| test_prune_runs.py | Merge into v2 ledger tests; remove its direct importlib loader. |
| test_render.py | Merge into engine rendering tests. |
| test_robustness.py | Split among defs/ledger, retained insights/init-registry atomic writes, and standalone supervision heartbeat safety. |
| test_scheduler.py | Merge trigger truth tables into engine scheduler; v1 when-list conversion belongs to importer. |
| test_state_lifecycle.py | Merge into v2 ledger/resume tests. |
| test_tuning.py | Merge into defs/preflight/resolve; v1-only schema cases belong to importer. |
| test_worktree_include.py | Merge into ledger/worktree tests, preserving traversal and tracked-file protections. |

Remove conftest's old-engine import without making independent plugin tests import the MCP engine. Replace TS test helper programs with Python before deleting them. Retain language-neutral fixtures.

All 37 old CLI commands are accounted for by these ownership groups:

| Commands | Destination |
|---|---|
| new-ulid | Internal ledger identifier generation |
| locate-def, list-defs, probe-requires, list-inputs, validate-input | Canonical definitions/discovery/preflight |
| get-preflight, get-tuning, get-step-select, get-profiles | Canonical preflight; retire v1-shaped CLI output |
| init-state, start-run, update-step, update-run, record-output, reset-running, write-log | Internal v2 ledger/executor; retire v1 mutation CLI |
| next-wave | Canonical scheduler; no legacy executable |
| runs-root | Shared stdlib path owner |
| list-runs, dump-state, find-runs-by-session, list-resumable-runs, prune-runs | V2 ledger/status/report; v1 detection read-only |
| render | Canonical renderer |
| current-session-id, session-path, session-label, profile-set, profile-get | Focused profile/session helper using canonical profile module |
| worker-heartbeat, stale-workers, supervise-config | Standalone supervision helper |
| list-agents, resolve-model, resolve-team | Canonical roster/resolution; v1 team syntax only in importer |
| apply-worktree-include | Canonical ledger/worktree phase |

Caller closure: the removal table above covers direct callers. Also update `wise-pr-watch-auto` through profile-read, standalone executor heartbeat instructions, ticket-auto implementation instructions through supervise-loop, and workflow-remove's duplicated root/layout logic. Validator imports (`STEP_TYPES`, `TRIGGER_RULES`, `_parse_frontmatter`, and `cmd_get_*`) move to canonical definitions/roster validation. Insights' optional import moves to a stdlib-only path helper. Hooks have no direct workflows.py import. Root CLAUDE.md, docs/wise/insights.md, plugin .gitignore ownership comments, and active contributor examples join the documentation audit.

Parity obligations beyond captured samples: all RPC framing/cancellation failures; MCP runtime envelopes and progress; gated/failed/cancelled/resumed ledger transitions and torn event tails; checkpoint/unit/log/context persistence; complete preflight trajectories; all adapter argv/environment/results; YAML scalar/regex/JS-value differences; every CLI command and mode. Existing source tests remain the executable baseline until each obligation is ported. P0 freezes that baseline; it does not certify Python parity.

P0 gate passed on macOS with the isolated pytest invocation documented above. No live model run or external mutation was needed. Linux parity remains a later gate.
