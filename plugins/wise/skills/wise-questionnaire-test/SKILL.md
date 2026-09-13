---
name: wise-questionnaire-test
description: >-
  Smoke-test Wise's real preflight questionnaire through native GUI or terminal
  TUI and report the answer contract without starting a workflow.
  Invoked as `/wise-questionnaire-test` (bare alias) or
  `/wise:wise-questionnaire-test` (canonical), or as
  `$wise:wise-questionnaire-test` in hosts that use skill mentions. Use when the user says
  "test the questionnaire", "test preflight UI", "check native pickers",
  or types `/wise-questionnaire-test`.
argument-hint: "[<workflow-name>]"
allowed-tools: Read, AskUserQuestion, Bash(git rev-parse:*), Bash(pwd:*), Bash(test:*), Bash(${CLAUDE_PLUGIN_ROOT}/engine/engine.sh:*), Bash(bash ${CLAUDE_PLUGIN_ROOT}/engine/engine.sh:*), Bash(${HOME}/.local/share/wise/bin/wise-engine:*)
---

# /wise-questionnaire-test

Before asking any user question, read and follow the
[question lifecycle](../../references/workflow-host-control.md#keep-asynchronous-questions-open).
Keep asynchronous prompts open until answered. A display acknowledgement is
not an answer.

## Why this skill exists

Exercise the installed engine and the current conversation's actual input UI
on Claude Code, Claude Desktop, Codex, Cursor, Grok, or another compatible host.
The main harness running this skill owns every prompt. Never delegate prompt
collection to a provider child or nested agent; such children may only request
that this conductor ask on their behalf.
Test only preflight: even choosing a separate worktree or an apply mode creates
no worktree and executes no workflow step. Report the route actually observed,
not a host's advertised capabilities. This command does not install or register
Wise in a host that lacks it.

## Arguments

Trim `$ARGUMENTS`. Accept zero or one whitespace-separated workflow name.
Default to `code-review`: its preflight offers `worktree`, multi-select
`step-select`, text inputs, the `input.mode` choice, and provider/model/effort
stages. Its execution can change files, but execution is never part of this test.
An optional workflow name permits reproducing a workflow-specific UI problem;
report question kinds or stages that definition does not expose as unexercised.

Reject extra tokens, paths, flags, placeholders (`TODO`, `FIXME`, `...`, `$VAR`,
`<name>`, `{name}`, `?`), or values outside `[a-z0-9]+(?:-[a-z0-9]+)*` with
`INVALID_ARGUMENT`. Never prompt to recover an invalid argument.

## Procedure

1. Parse and validate arguments before using tools. Read
   [host control](../../references/workflow-host-control.md) and resolve the
   loaded installation from this skill's location. Identify the actual
   conductor and client separately, for example `claude/desktop` or
   `codex/cli`; selecting a provider later does not change that identity.
   Use `unknown` if the client cannot be established from live session evidence.
   Resolve `cwd` to the current git toplevel, otherwise the absolute working
   directory. Keep all test answers only in the current conversation.

2. Follow host control's loaded-installation refresh for an existing owned
   registration and select the actual `--wise-host` on shell calls. If setup
   is missing or refuses refresh, report its error and the required setup
   action; do not run setup, installers, login, or configuration repair here.
   Use the active session's managed `wise_preflight` tool when available.
   Do not invoke another Wise action skill.

3. Start fresh with `answers: {}` and no context or input prefills. Call
   `wise_preflight {workflow, cwd, answers: {}, interactive: false}` to inspect
   the initial engine questionary internally. It must put `worktree` first.
   For the default definition, also check `step-select` is `multi` and that
   text and choice inputs are present. A mismatch is `QUESTION_CONTRACT`.
   Do not display the raw questionary in chat. If MCP is unavailable, use the
   host-selected launcher's non-interactive `preflight <workflow> --cwd <cwd>`
   for this inspection. Confirm the default resolves to the loaded bundled
   `code-review` definition using `list-defs` when needed; a user override is
   `WORKFLOW_SHADOWED`, not evidence about the bundled smoke test.

4. Collect real user answers through the first usable route:

   - **MCP form:** call `wise_preflight` with the same workflow/cwd, empty
     answers and `interactive: true`. Let its forms own staged collection.
   - **Native picker:** on `INTERACTIVE_UI_REQUIRED`, or if the MCP tool is
     absent but CLI inspection works, render the current engine questions with
     the host's supported structured input tool. Populate its actual options
     field. Process `worktree` first, then `step-select` and inputs, then the
     returned harness, permission, model and effort stages. Re-call
     non-interactive preflight with the cumulative answers after each answered
     question. Preserve exact engine values and keys. Use native multi-select
     or the shared clickable Include/Exclude sequence, never a typed list.
     Free text belongs in a native text field. Honor each input tool's rules
     for permission questions; choose another permitted structured route when
     necessary. Do not rename a permission question to evade a restriction.
   - **Terminal TUI:** when no permitted persistent native control covers the
     next question, use the host-selected stable launcher in a terminal the
     user can actually operate:

     ```bash
     "$HOME/.local/share/wise/bin/wise-engine" --wise-host "$WISE_HOST" \
       preflight "$WORKFLOW" --cwd "$TEST_CWD" --answers "$ANSWERS_JSON" --interactive
     ```

     Substitute resolved variables and safely quoted serialized cumulative
     answers (`{}` for a fresh test). Keep JSON output enabled, so do not add
     `--text`. A tool-owned PTY without user input access is not a usable TUI.
     If no user-operable terminal is available, report
     `INTERACTIVE_UI_REQUIRED`; do not launch a stranded prompt or simulate
     keystrokes. If the user runs the command externally, wait for its returned
     JSON before assessing it. Do not claim a TUI pass from a command suggestion.

   The terminal TUI, when used, must run in a terminal owned by this same main
   harness. Await actual selections and preserve prior answers across route changes.
   User acceptance of a highlighted default is valid; auto-submitting defaults
   is not. Keep the turn active while a prompt is open. Explicit cancellation,
   closed stdin, or an invalid UI response ends the test with the returned
   error code. Do not restart collection after cancellation. Record every route
   used and any fallback error separately from the final result.

5. Verify the returned contract before PASS. The final payload must identify
   the requested workflow, contain an object `answers`, `questions: []`, and
   an empty `requires_missing` list, with no `error`. For the native-picker
   route, retain your cumulative answer object beside the final raw response
   (raw preflight does not itself return an `answers` field).

   Replay the collected answers through **non-interactive preflight only**:
   start with an empty local prefix, inspect its current questions, validate
   the collected value for the next unlocked question, add it to the prefix,
   and repeat until no questions remain, with a limit of 256 questions
   (`PREFLIGHT_LIMIT` if exceeded). Do not open new prompts during this
   verification. Require strings for `text`, exactly one declared option value
   for `choice`, and an array of declared values for `multi`, respecting any
   returned constraints. Reject missing values, unknown keys, synthetic
   navigation labels, display acknowledgements, and changed earlier answers
   as `ANSWER_CONTRACT`. A nonempty final questionary is `QUESTIONS_REMAINING`.
   This replay checks even the answers hidden inside an MCP form loop against
   the actual engine stages. Never use `wise_run` to validate answers.

   Record question IDs in collection order, observed kinds, and tuning stages
   reached. Single-provider/model/effort catalogs and locked or disabled
   groups legitimately omit questions: mark those stages unexercised, with
   the engine evidence, rather than inventing answers or claiming coverage.
   A permission question is per provider; one provider's answer cannot fill
   another provider's key. Missing dependencies produce `REQUIRES_MISSING`
   even if an interactive wrapper returns `questions: []`.

6. Emit this compact copy-pasteable report using observed values:

   ```text
   QUESTIONNAIRE PASS|FAIL
   harness=<conductor> client=<client> workflow=<name>
   route=<MCP form|native picker|terminal TUI; list transitions if used>
   collected=<comma-separated question IDs, or none>
   coverage=<observed kinds/stages>; unexercised=<stages and short reasons, or none>
   contract=<valid|invalid|unverified> code=<failure code or none> fallback=<code or none>
   workflow_started=no
   ```

   PASS requires actual UI answers and successful contract replay, not merely
   tool discovery or a successful engine subprocess. Preserve engine error
   codes verbatim. Use `ENGINE_UNAVAILABLE` if no preflight route can be called,
   `CONTRACT_UNVERIFIED` if returned answers cannot be checked, and the local
   codes above for specific contract failures. Add at most one sentence naming
   the failed stage and practical next action. Do not print answer values,
   free-text contents, full payloads, or credentials in the report.

## Guardrails

- Never call `wise_run`, CLI `run`, dispatch, resume, workflow steps, or provider
  children. Never create a branch/worktree, stage, commit, push, or write a
  report artifact. Preflight may start the managed engine daemon on demand.
- Never render preflight as raw chat questions, answer for the user, infer a
  choice from elapsed time, or treat cancellation as an empty selection.
- Do not claim other harnesses passed from this invocation. Repeat this command
  inside each target host after loading the skill there; slash-command discovery
  depends on the host. In hosts without slash commands, invoke the loaded
  `wise-questionnaire-test` skill by name.
