# ensure-reviewers - read the current reviewers + ask about human reviewers

Before collecting user input, follow the [question lifecycle](../workflow-host-control.md#keep-asynchronous-questions-open).
A display acknowledgement is not an answer; keep asynchronous prompts open.
This does not add prompts to autonomous paths.

This fragment attaches nothing by default. It reads who is already
requested on the PR and asks the user whether to add human
reviewers; if so, the follow-up step (`propose-reviewers.md`)
handles the actual picker. Wise never requests a review bot (GitHub
Copilot code review, CodeRabbit): a bot configured on the repo
reviews on its own, and wise reviews locally with the 3-lens code
review team (`references/code-review-pass.md`).

Used by:
- `/wise-pr-add-reviewers` standalone skill (which also orchestrates
  `propose-reviewers.md` when the user picks `yes`).

## Context the caller supplies

- `pr_number` — PR number (must exist by the time this fragment runs).
- `pr_url` — PR url (for the final summary).
- `project.path` — absolute path to the repo working tree.

## Procedure

Run all `gh` commands with `cd <project.path>` first so they resolve
to the right repo.

### 1. Read the current reviewer list

```bash
gh pr view <pr_number> --json reviewRequests \
  --jq '[.reviewRequests[].login // .reviewRequests[].name] | join(",")'
```

Keep the result in `ALREADY_REQUESTED` — a comma-separated list of
user/team slugs already on the PR. Drop review bots from it
(`copilot-pull-request-reviewer`, `Copilot`, `coderabbitai`, any
login ending in `[bot]`): they are never wise's to manage. The goal
is idempotency: a slug already requested is never re-requested.

### 2. Ask whether to add human reviewers

This fragment does NOT enumerate org members or ask for typed
logins. Instead it asks a simple two-way choice and hands off to
`propose-reviewers.md` (the separate follow-up step) when the user
wants reviewers.

Use `AskUserQuestion`:

- question: `Add human reviewers to this PR?`
- header: `Reviewers`
- multiSelect: false
- options (2):
  - `No` - `Leave the reviewer list as it is. Continue to the next step.`
  - `Yes - Claude proposes candidates` - `Analyse the PR (changed files, CODEOWNERS, recent authors) and surface the most relevant org members as picks in the next step.`

Map the result to an `extras_choice` value:
- `No` → `no`
- `Yes - …` → `yes`

### 3. Emit the final line

`attached` is `ALREADY_REQUESTED` (the human reviewers already on
the PR when this step ran), or `NONE` when it is empty. Your
response's FINAL line - alone on its own line, no markdown, no
backticks - MUST match:

```
REVIEWERS: attached=<slug1,slug2,...-or-NONE> extras=<no|yes>
```

Examples:

```
REVIEWERS: attached=NONE extras=yes
REVIEWERS: attached=jlevdev extras=no
```

The caller captures both values - the `propose-reviewers` step
gates on `extras_choice == 'yes'`.

## Guardrails

- Never attach, request or re-request a review bot
  (`copilot-pull-request-reviewer`, `Copilot`, `coderabbitai`), by
  `gh pr edit --add-reviewer`, the GraphQL `requestReviews`
  mutation or a trigger comment.
- Never remove a reviewer.
- Never re-request a reviewer already on the PR.
- Do NOT ask the user to type comma-separated logins here. If they
  want reviewers, emit `extras=yes` and let `propose-reviewers.md`
  surface Claude-picked candidates in the next step.
