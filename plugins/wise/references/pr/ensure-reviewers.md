# ensure-reviewers — attach the default reviewer + ask about extras

This fragment owns the **defaults** side of reviewer attachment —
Copilot code review. It also asks the user whether they want to add
extras beyond the default; if so, the follow-up step
(`propose-reviewers.md`) handles the actual picker.

Used by:
- `/wise-pr-add-reviewers` standalone skill (which also orchestrates
  `propose-reviewers.md` when the user picks `yes`).
- the `ticket-auto` workflow's autonomous analogue,
  `request-review-auto.md`.

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
user/team/bot slugs already on the PR. The goal is idempotency: if
a slug is already requested, don't re-request it.

### 2. Attach Copilot code review

Copilot's requested-reviewer slug varies by org setup. Try the CLI
shorthand first; fall back to the GraphQL `requestReviews` mutation
with Copilot's bot node id if the shorthand isn't accepted.

```bash
# Preferred: the simple slug. Works on orgs where Copilot code
# review is enabled at the repo or enterprise level.
gh pr edit <pr_number> --add-reviewer copilot-pull-request-reviewer
```

On "not a valid user" / "reviewer not found":

```bash
COPILOT_NODE="$(gh api graphql -f query='
  query { user(login: "copilot-pull-request-reviewer") { id } }
' --jq '.data.user.id' 2>/dev/null)"

if [ -n "$COPILOT_NODE" ]; then
  PR_NODE="$(gh pr view <pr_number> --json id --jq .id)"
  gh api graphql -f query='
    mutation($pr: ID!, $reviewers: [ID!]!) {
      requestReviews(input: { pullRequestId: $pr, userIds: $reviewers }) {
        pullRequest { number }
      }
    }
  ' -F pr="$PR_NODE" -F reviewers="$COPILOT_NODE"
fi
```

If neither approach works (Copilot code review not enabled for the
org, or auth doesn't grant the scope), log `Copilot reviewer NOT
attached — <one-line reason>` and continue. Don't fail the step
over it.

If Copilot was already in `ALREADY_REQUESTED`, skip the call and
note "Copilot already requested".

### 3. Ask whether to add extras

This fragment does NOT enumerate org members or ask for typed
logins. Instead it asks a simple three-way choice and hands off
to `propose-reviewers.md` (the separate follow-up step) when the
user wants extras.

Use `AskUserQuestion`:

- question: `Add individual reviewers beyond Copilot?`
- header: `Extras`
- multiSelect: false
- options (3):
  - `No — just Copilot` — `Keep Copilot only. Continue to the next step.`
  - `Yes — Claude proposes candidates` — `Analyse the PR (changed files, CODEOWNERS, recent authors) and surface the most relevant org members as picks in the next step.`
  - `Skip — don't touch reviewers` — `Revert the default this step just added (newly-added Copilot gets removed). Use when you realise mid-flow that review isn't wanted yet.`

Map the result to an `extras_choice` value:
- `No — …` → `no`
- `Yes — …` → `yes`
- `Skip — …` → `skip`

On `skip`: undo the step's own additions — any reviewer this step
NEWLY added in §2 gets `gh pr edit --remove-reviewer`'d. Do NOT
remove reviewers that were already requested before §2 ran (the
ones in `ALREADY_REQUESTED`). Then emit the final line below with
`attached=NONE-skipped`.

### 4. Emit the final line

Build a comma-separated list of slugs that ended up on the PR as
a result of this step (the default that actually stuck — whether
newly-added or already-present — or `NONE` if the user chose
skip). Your response's FINAL line — alone on its own line, no
markdown, no backticks — MUST match:

```
REVIEWERS: attached=<slug1,slug2,...-or-NONE> extras=<no|yes|skip>
```

Examples:

```
REVIEWERS: attached=copilot-pull-request-reviewer extras=yes
REVIEWERS: attached=copilot-pull-request-reviewer extras=no
REVIEWERS: attached=NONE-skipped extras=skip
```

The engine captures both values — the `propose-reviewers` step
gates on `extras_choice == 'yes'`.

## Guardrails

- Never block on a Copilot-attach failure.
- Never remove a reviewer the user didn't ask to remove. The
  `skip` path reverts only what *this step* added.
- Never re-request a reviewer already on the PR.
- Do NOT ask the user to type comma-separated logins here. If they
  want extras, emit `extras=yes` and let `propose-reviewers.md`
  surface Claude-picked candidates in the next step.
