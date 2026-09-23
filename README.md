# wise-claude

> A coding copilot for **Claude Code**: everyday `/wise-*` commands, a
> workflow engine that runs multi-step jobs across several AI coding CLIs, and
> ready-made pipelines that take a ticket all the way to a merged PR.

![version](https://img.shields.io/badge/version-5.20.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![Agent Skills](https://img.shields.io/badge/Agent%20Skills-standard-informational)

`wise-claude` is the plugin marketplace that hosts **`wise`**. It gives you
three things:

1. **Skills** - slash commands for the chores you repeat every day:
   commit messages, commits, PRs, CI babysitting, ticket research and
   planning, PRDs and TRDs.
2. **Workflows** - YAML files that describe a multi-step job. The wise engine
   runs them in the background on `claude`, `codex`, `cursor-agent`, `gemini`
   or `grok`, with the harness, model and effort you pick for each step.
   Several ship with the plugin, and you can write your own in minutes.
3. **An agent roster** - SDLC role subagents (architect, software engineer,
   QA, security, SRE, product manager and more) that skills and workflows
   can delegate to.

Use a single command for a quick task, or hand a whole
**ticket -> plan -> code -> review -> PR -> merge** pipeline to the engine and
let it run unattended.

## Contents

- [Quick start](#quick-start)
- [Skills](#skills)
- [Workflows](#workflows)
  - [How a run works](#how-a-run-works)
  - [Bundled workflows](#bundled-workflows)
  - [Create your own workflow](#create-your-own-workflow)
  - [Managing runs](#managing-runs)
- [Harnesses, models and effort](#harnesses-models-and-effort)
- [Agent roster](#agent-roster)
- [Requirements](#requirements)
- [Troubleshooting](#troubleshooting)
- [Feedback and feature requests](#feedback-and-feature-requests)
- [Repository layout](#repository-layout)
- [Contributing](#contributing)

## Quick start

### 1. Install

Claude Code:

```
/plugin marketplace add e1024kb/wise-claude
/plugin install wise@wise-claude
```

Codex, from a terminal rather than the Codex prompt:

```bash
codex plugin marketplace add e1024kb/wise-claude
codex plugin add wise@wise-claude
```

For Cursor, Grok or T3 Code, load the wise skills through that host's own
skill installation mechanism. The
[host setup guide](plugins/wise/references/workflow-host-control.md) covers
registration and workflow control for every host.

### 2. Initialise

Run `/wise-init` once per host. It prepares the Python runtime for the
workflow engine, registers the engine with your host, and reports which
provider CLIs are installed and logged in. Optional pieces (GitHub, SSH, MCP
connectors) are only checked when something you run needs them.

### 3. Try it

| Try this | What happens |
|---|---|
| `/wise` | Prints the command catalog. `/wise open a PR` maps free text to the right command. |
| `/wise-commit` | Stages your changes, drafts a Conventional-Commits subject and commits. |
| `/wise-pr-create` | Opens or refreshes a PR with a body drafted from your diff. |
| `/wise-workflow-run example-workflow` | Runs a harmless demo workflow that exercises every step type. A good first check that the engine works. |
| `/wise-workflow-create Review the current branch, fix findings, and run tests` | Turns a sentence into a saved workflow you can run again. |

## Skills

Every skill is a slash command. The full reference with every flag lives in
the [plugin README](plugins/wise/README.md#commands).

| Area | Commands | What they do |
|---|---|---|
| Commits | `/wise-commit-message`, `/wise-commit`, `/wise-commit-push` | Draft a Conventional-Commits subject (with ticket scope when one is found), then optionally commit and push. Never force-pushes or skips hooks. |
| Pull requests | `/wise-pr-create`, `/wise-pr-add-reviewers`, `/wise-pr-watch` | Open or refresh a PR from your project's template, attach reviewers, and drive CI failures and review comments to green. |
| Research and planning | `/wise-grill`, `/wise-revise`, `/wise-tickets`, `wise-estimation` | Deep-research a ticket, doc or question into a plan. Audit a folder or project into ranked improvement plans. Shape tickets and size them in story points. |
| Product and design docs | `wise-prd-architect`, `wise-trd-architect` | Author PRDs and TRDs when you ask for one. |
| Writing style | `/wise-human-writing`, `/wise-code-comments` | Keep PR bodies, tickets, chat messages and code comments readable and free of generated-text noise. |
| Autonomous building blocks | `/wise-pr-create-auto`, `/wise-pr-watch-auto`, `/wise-implement-plan-auto`, `/wise-simplify-auto`, `/wise-pr-request-review-auto` | Prompt-free variants of the commands above. They never stop to ask, so pipelines can chain them. |
| Other harnesses | `/wise-exec-on-harness` | Run one prompt on `codex`, `cursor`, `gemini` or `grok` from inside your current session. |
| Session tools | `/wise-report`, `/wise-fork`, `/wise-profile`, `/wise-supervise` | Verified status reports, clean restarts after a session fork, token-budget profiles, and a watchdog for background agent teams. |
| Self-improvement | `/wise-insights-mine`, `/wise-insights-refine`, `/wise-insights-reset` | Find tasks you repeat across sessions and turn them into personal skills, with your approval. Fully local. |
| Workflows | `/wise-workflow-run`, `-create`, `-list`, `-status`, `-resume`, `-remove` | Run, build and manage workflows. See [Workflows](#workflows). |
| Extending wise | `/wise-skills-create`, `/wise-skills-edit` | Scaffold or edit wise skills. |

## Workflows

A workflow is a reusable, multi-step job described in one YAML file. Each
step is one of:

- an **AI agent** running headless on the harness and model you choose,
- a **shell command**,
- a **question** or **approval** that pauses the run for you,
- a **units** pipeline that drives tickets or plans all the way to merged PRs.

Steps declare what they depend on. Steps whose dependencies are done run in
parallel. Results flow from one step to the next through named outputs.

The engine runs as a small background daemon. Your conversation stays light:
it asks the pre-flight questions, prints one line per event, and relays
approvals. It never loads step output into your context. Every run is
recorded on disk, so an interrupted run can resume where it stopped.

### How a run works

1. **Pick a workflow.** `/wise-workflow-run <name>`, or a skill that wraps
   one (for example `/wise-pr-watch-auto` runs `pr-watch`).
2. **Pre-flight.** You answer every question up front: current checkout or a
   new worktree, which optional steps to run, the workflow's inputs, then
   harness, permission level, model and effort for each group of steps.
   Questions show up as native pickers in your host.
3. **Run.** The engine schedules the steps, runs independent ones in
   parallel, and switches to a fallback harness if one hits a rate limit.
4. **Gates.** An `ask` or `approval` step pauses the run and the question
   appears in your conversation. Workflows marked synchronous never pause.
5. **Report.** You get a table of steps, verdicts, models and token usage,
   plus a cost estimate per harness.

### Bundled workflows

| Workflow | Start it with | Use it when |
|---|---|---|
| [`ticket-auto`](plugins/wise/workflows/ticket-auto/README.md) | `/wise-workflow-run ticket-auto` | You want tickets turned into merged PRs unattended. Per ticket it claims a branch and worktree, plans, implements, reviews and fixes, opens the PR, handles CI and review bots, and merges when green. One PR per ticket. |
| [`ticket-plan`](plugins/wise/workflows/ticket-plan/README.md) | `/wise-workflow-run ticket-plan` | You want a researched, story-point-estimated implementation plan to review before any code is written. Gaps come back as targeted questions. |
| [`impl-plan-auto`](plugins/wise/workflows/impl-plan-auto/README.md) | `/wise-workflow-run impl-plan-auto` | You already have `PLAN-*.md` files (for example from `/wise-revise`) and want them implemented and merged. |
| [`impl-plan`](plugins/wise/workflows/impl-plan/README.md) | `/wise-implement-plan-auto <plan>` | You want one plan implemented on the current branch as atomic commits, without pushing. |
| [`code-review`](plugins/wise/workflows/code-review/README.md) | `/wise-workflow-run code-review` | Before you push: three parallel reviewers (correctness, security, tests), a curator, optional verification, then a fixer that commits the kept findings. |
| [`pr-watch`](plugins/wise/workflows/pr-watch/README.md) | `/wise-pr-watch-auto` | A PR is open and you want it driven to merge: CI fixes, bot review threads, a substitute review if a bot is stuck. |
| [`example-workflow`](plugins/wise/workflows/example-workflow/README.md) | `/wise-workflow-run example-workflow` | You want to check the engine after an install, or see every step type in one small file. |

Each workflow's README documents its flow diagram, steps, inputs and outputs.

### Create your own workflow

The bundled workflows are examples of what the engine can do. The real power
is writing your own for the jobs your team repeats: a release checklist, a
migration across services, a nightly audit, a review pass with your house
rules.

#### Option 1: describe it in one sentence

```
/wise-workflow-create Review the current branch, fix findings, and run tests
```

wise infers the name, steps and dependencies, asks harness, model and effort
for each AI step through the usual pickers, then validates and saves the
definition with its own README. Run it later with
`/wise-workflow-run <name>`. Use `--name <name>` to choose the name yourself.

#### Option 2: write the YAML

A complete workflow that drafts release notes while the tests run, then asks
before saving:

```yaml
version: 2
name: release-notes
description: Draft release notes since the last tag, run the tests, and ask before saving.

tuning:
  groups:
    - id: writer
      label: "Release-notes writer"
      default: { harness: claude, model: sonnet, effort: medium }
      fallback: [codex]            # used if claude hits a rate limit

inputs:
  - name: audience
    prompt: "Who reads these notes?"
    default: "users"
    validate: "^(users|developers)$"

steps:
  - id: changes                    # shell step, stdout becomes an output
    type: bash
    run: git log "$(git describe --tags --abbrev=0)..HEAD" --oneline
    outputs: [changes]

  - id: tests                      # no depends_on, so it runs alongside `changes`
    type: bash
    run: just test
    timeout: 900

  - id: draft                      # AI step with a structured result
    type: agent
    group: writer
    depends_on: [changes]
    prompt: |
      Write release notes for {{project.name}} aimed at {{audience}}.
      Commits since the last tag:
      {{changes}}
      Return the field directly: notes.
    schema:
      type: object
      properties: { notes: { type: string } }
      required: [notes]
      additionalProperties: false
    outputs: [notes]

  - id: approve                    # pauses the run for you
    type: approval
    depends_on: [draft, tests]
    message: "Tests passed. Save these notes?\n\n{{notes}}"

  - id: save
    type: bash
    depends_on: [approve]
    run: printf '%s\n' "$WISE_NOTES" > RELEASE_NOTES.md
```

Save it, check it, preview its questions, and run it:

1. Save it as `workflows/definitions/release-notes/workflow.yaml` under the
   wise data directory (usually `~/.local/share/wise`).
   `wise-engine definition-roots` prints the exact folder.
2. Validate: `wise-engine compile-check release-notes`.
3. Preview the pre-flight questions: `wise-engine preflight release-notes`.
4. Run it: `/wise-workflow-run release-notes`.

`wise-engine` is the launcher at `~/.local/share/wise/bin/wise-engine`, created
by `/wise-init`.

#### The building blocks

| Concept | What it gives you |
|---|---|
| `agent` step | One headless AI child with a `prompt`. Add a JSON `schema` and `outputs` to get structured results that later steps can use. `allowed_tools` pre-grants tools, `max_turns` and `timeout` bound it. |
| `bash` step | A deterministic shell command. The first output name captures stdout. Inputs and outputs are exported as `WISE_<NAME>` environment variables. |
| `ask` step | Pauses the run with a question, with fixed options or free text. The answer becomes an output. |
| `approval` step | Pauses for approve or reject. Reject fails the step. |
| `units` step | The built-in ticket, plan and PR pipelines, to embed in your own workflow. |
| `depends_on` | Builds the step graph. Steps whose dependencies are done run in parallel. |
| `trigger-rule`, `when` | Run a step only when its dependencies succeeded, or all finished, or none failed. `when:` adds a condition such as `mode == 'strict' && tests != ''`. |
| `tuning` groups | Name a group of steps, give it a default harness, model and effort, and pre-flight lets the user change it. Add `fallback` harnesses for rate limits. |
| `inputs` | Values asked at pre-flight, with defaults, regex validation, branch pickers, and pre-fill from the conversation (a ticket, your guidance). |
| `step-select` | Mark steps `optional` and the user picks which ones run. |
| Templating | `{{name}}` inserts an input or output. `{{project.path}}`, `{{run.dir}}` and `{{workflow.dir}}` point at the project, the run's scratch folder and your workflow's folder (for prompt files and templates). |
| `preflight` | Defaults for worktree mode, and `control-mode: synchronous` for runs that must never pause. |
| Roster agents | A Claude agent step can delegate to the roster: "run three reviewers as `wise:code-reviewer`, `wise:security-engineer`, `wise:qa-engineer`". |

#### Tips

- **Pass data through outputs and files, not memory.** Each agent step
  starts fresh and sees only its prompt. Hand results forward with `schema`
  outputs or files under `{{run.dir}}`.
- **Ask everything up front.** Inputs asked at pre-flight let a long run
  finish without anyone watching.
- **Put prompts in files.** Folder-form workflows can ship `prompts/*.md` and
  reference them through `{{workflow.dir}}`. Add a `README.md` for your team.
- **Start from a bundled workflow.** A user workflow with the same name as a
  bundled one replaces it, so you can copy `code-review` and add your own
  rules.
- **Read values from environment variables in shell steps.** Use
  `"$WISE_NOTES"`, not `{{notes}}`, so a value with quotes cannot break the
  command.

The complete schema, including every field, the `when` grammar, model
resolution, gates and the run directory, is in the
[workflow reference](docs/wise/workflows.md).

### Managing runs

| Command | Purpose |
|---|---|
| `/wise-workflow-list` | List bundled and user workflows. |
| `/wise-workflow-status [<run-id>]` | List runs, or show one run and its open question. |
| `/wise-workflow-resume [<run-id>]` | Continue a paused or interrupted run, or answer its open question. |
| `/wise-workflow-remove <name>` | Delete a user workflow. Bundled ones cannot be removed. |

## Harnesses, models and effort

A workflow step can run on any of five provider CLIs: `claude`, `codex`,
`cursor-agent`, `gemini` and `grok`. Each runs under your own login, so
subscription plans work without API keys. The host you talk to (Claude Code,
Codex and so on) and the provider that runs a step are separate choices: you
can conduct from Claude Code and run the fixes on Codex.

At pre-flight the engine offers a model catalog per harness, plus any extra
models the installed CLI reports, and the effort levels each model supports.
Each run ends with token usage and a cost estimate per harness. The current
catalog is listed in the
[workflow reference](docs/wise/workflows.md#pre-flight-questionary).

## Agent roster

`wise` ships SDLC role subagents under
[`plugins/wise/agents/`](plugins/wise/agents/), catalogued in the
[agent index](plugins/wise/AGENTS.md). In Claude Code they appear in
`/agents` and are invocable as `subagent_type: wise:<name>`. A workflow's
agent step can delegate to them too.

- **`ceo`** - business calls: prioritises competing initiatives, weighs value,
  risk, cost and timing, and makes go / no-go decisions.
- **`cto`** - technical strategy: build vs buy, platform and stack direction,
  technical risk, and settling cross-team architecture disputes.
- **`product-manager`** - turns a problem into requirements: user stories,
  testable acceptance criteria, and MVP vs later scope.
- **`engineering-manager`** - turns an approved plan into a delivery plan:
  tasks with acceptance criteria, estimates, dependencies and parallel waves.
- **`architect`** - system and component design: patterns, boundaries, data
  flow and trade-offs, recorded as ADR-style decisions.
- **`software-engineer`** - hands-on implementation: features, bug fixes,
  refactors and tests, reusing what the codebase already has.
- **`code-reviewer`** - reviews a diff or branch for correctness, security and
  clear quality problems, one severity-tagged finding per line.
- **`qa-engineer`** - test strategy: edge cases, test plans, automated tests,
  and reproducible defect reports.
- **`security-engineer`** - application security review: threat modelling,
  vulnerability audits of auth, crypto, secrets and input handling.
- **`devops-engineer`** - CI/CD, infrastructure as code, containers and safe
  rollouts (blue-green, canary) with a rollback path.
- **`sre`** - reliability: SLIs, SLOs and error budgets, monitoring and
  alerting, capacity planning and incident runbooks.
- **`ux-designer`** - experience design: user flows, information
  architecture, usability and accessibility (WCAG) reviews.
- **`technical-writer`** - developer docs checked against the code: READMEs,
  API references, how-to guides and changelogs.

## Requirements

- One of these as the host you talk to:
  - [Claude Code](https://claude.com/product/claude-code)
  - [Claude Desktop](https://claude.ai/download)
  - [Codex](https://openai.com/codex/)
  - [Cursor](https://cursor.com)
  - [Grok](https://grok.com/build)
  - [T3 Code](https://github.com/pingdotgg/t3code)
- **`git`**, and an authenticated **`gh` CLI** for the PR skills and
  pipelines.
- **Python 3.11+** for the workflow engine. The launcher installs pinned
  dependencies in its own managed environment. No Node, npm or Bun needed.
- A logged-in provider CLI for every harness you pick (`claude`, `codex`,
  `cursor-agent`, `gemini` or `grok`).
- `/wise-init` run once per host.

## Troubleshooting

- **Commands don't show up.** Confirm `/plugin install wise@wise-claude` ran,
  then start a fresh session.
- **A workflow or PR step fails on auth.** Run `/wise-init`, check that
  `gh auth status` is green and that the repo has an `origin` remote.
- **A workflow won't start.** Run `wise-engine compile-check <name>` to see
  validation errors with hints. Old v1 definitions can be converted with
  `wise-engine migrate <path>`.
- **`/wise` can't classify a request.** Type `/wise-` to browse every command
  in the menu.

## Feedback and feature requests

Found a bug, missing a feature, or have an idea to make wise better? Tell us
from inside your session:

```
/wise-feedback The PR body should link the ticket when the branch name has one
```

`/wise-feedback` is not only for bugs. Use it for:

- **Feature requests** - a new command, workflow, harness or model you want.
- **Improvements** - a skill that asks too much, output that could be
  clearer, a workflow step that should be optional.
- **Bugs** - something that failed or behaved unexpectedly.

It drafts a GitHub issue from your words and the current session (problem,
summary, proposal), adds your OS, host version and project, and shows you
the full issue before anything is filed. You can also open an issue directly
on [GitHub](https://github.com/e1024kb/wise-claude/issues).

## Repository layout

```
wise-claude/
├── .claude-plugin/marketplace.json      # marketplace index, points to plugins/wise
├── plugins/wise/                        # the plugin: skills, agents, workflows, engine/
├── docs/wise/                           # workflow engine and authoring reference
├── scripts/validate_repo.py             # repository structure checks
├── justfile                             # task runner (validate, test, check)
└── CONTRIBUTING.md                      # contributor manual
```

## Contributing

Issues and PRs are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
conventions, local install steps and validation checks.

## License

[MIT](LICENSE) © e1024kb
