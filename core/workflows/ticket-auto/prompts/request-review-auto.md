# request-review-auto — request Copilot + CodeRabbit review, autonomously

Autonomous analogue of `references/pr/ensure-reviewers.md`
(its defaults half only). Kicks off the **bot** reviews on a PR with
**no prompts** — it NEVER calls `AskUserQuestion` and never enumerates
human reviewers. It does two things:

- **Copilot** — attaches `copilot-pull-request-reviewer` as a code
  reviewer (Copilot only reviews when requested).
- **CodeRabbit** — posts an explicit `@coderabbitai review` trigger
  (CodeRabbit auto-reviews on push when enabled, but an explicit trigger
  also covers repos with auto-review off and re-points it at the current
  head). Harmless no-op on a repo where CodeRabbit is not installed.

Neither is confirmed here — confirming that each bot actually reviewed
the head (and handling CodeRabbit out-of-credits / rate-limit states) is
the **watch** step's job (`watch-pipelines-auto.md` §4). This step only
ensures both reviews are requested.

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
and record `copilot=already`.

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

### 3. Trigger CodeRabbit review

CodeRabbit only acts on a PR where its GitHub App is installed. Don't
try to prove installation here (it answers asynchronously — that's the
watch step's job). Probe the cheap footprint, then trigger:

```bash
OWNER_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
CR_FOOTPRINT="$(gh pr view <pr_number> --json comments \
  --jq 'any(.comments[]; .author.login | test("coderabbit";"i"))')"
```

- If `CR_FOOTPRINT` is `true`, CodeRabbit already started (auto-review
  on push) — record `coderabbit=present` and do **not** post a
  redundant trigger.
- Otherwise post one explicit trigger and record `coderabbit=triggered`:

  ```bash
  gh pr comment <pr_number> --body "@coderabbitai review"
  ```

A trigger on a repo without CodeRabbit is a harmless stray comment; the
watch step detects the absence and skips it. Never fail the step over a
CodeRabbit trigger error — best-effort, like Copilot.

### 4. Emit the final line

FINAL line — alone, no markdown, no backticks — MUST match:

```
REVIEW-REQUEST: copilot=<attached|already|unavailable> coderabbit=<present|triggered|error>
```

## Guardrails

- Never block on a Copilot-attach or CodeRabbit-trigger failure — both
  are best-effort.
- Never enumerate or attach human reviewers — an autonomous run does
  not pick people.
- This step only *requests* the reviews; it never waits, never reads a
  bot's findings, and never merges — `watch-pipelines-auto.md` owns the
  wait, the credit/rate-limit handling, the comment handling, and the
  merge.
