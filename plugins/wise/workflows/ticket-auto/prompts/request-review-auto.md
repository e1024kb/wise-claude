# request-review-auto — attach Copilot review, autonomously

Autonomous analogue of `pr-interactive/prompts/ensure-reviewers.md`
(its defaults half only). Attaches Copilot code review to a PR with
**no prompts** — it NEVER calls `AskUserQuestion` and never enumerates
human reviewers. CodeRabbit needs no attach: it is a GitHub App that
auto-reviews every push, so for CodeRabbit there is nothing to do.

Source of truth for the `/wise-pr-request-review-auto` skill and the
`ticket-auto` workflow's request-review step.

## Context the caller supplies

- `pr_number` — the PR number.
- `project.path` — absolute path to the repo working tree.

## Procedure

Run all `gh` commands with `cd <project.path>` first.

### 1. Read the current reviewer list

```bash
gh pr view <pr_number> --json reviewRequests \
  --jq '[.reviewRequests[].login // .reviewRequests[].name] | join(",")'
```

If `copilot-pull-request-reviewer` is already in that list, skip §2
and report `copilot=already`.

### 2. Attach Copilot code review

Try the CLI shorthand first; fall back to the GraphQL mutation:

```bash
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
org, or auth lacks the scope), record `copilot=unavailable` with a
one-line reason and continue — **never fail the step over it**.

### 3. Emit the final line

FINAL line — alone, no markdown, no backticks — MUST match:

```
REVIEW-REQUEST: copilot=<attached|already|unavailable>
```

## Guardrails

- Never block on a Copilot-attach failure — it is best-effort.
- Never enumerate or attach human reviewers — an autonomous run does
  not pick people.
- Nothing to do for CodeRabbit — it auto-runs on PR push.
