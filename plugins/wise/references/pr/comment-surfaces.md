# comment-surfaces — PR comment fetch, bot allowlist, thread resolve

Shared routine for every comment-queue handler
(`handle-bot-reviews.md`, `handle-human-comments.md`,
`workflows/ticket-auto/prompts/handle-bot-reviews-auto.md`). GitHub
splits PR comments across three REST surfaces plus a GraphQL thread
view; the fetch queries, the bot-author allowlist, and the
`resolveReviewThread` mutation used to live copy-adjacent in each
handler — this file is now their single home, so the classifiers can
never drift apart (a drifted list means a Copilot comment leaks into
the human queue AND the bot queue and gets walked twice).

Caller supplies `pr_number` and runs everything with
`cd <project.path>` first. `<prefix>` below is a caller-chosen scratch
filename prefix (e.g. `pr-$PR`, `wise-hum-$PR`) so two queues in one
loop never collide.

## 1. Fetch the three comment surfaces

```bash
PR=<pr_number>
OWNER_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/wise-pr-XXXXXX")"

# Issue comments (top-level conversation; bot summaries).
gh pr view "$PR" --json comments \
  > "$SCRATCH/<prefix>-issue-comments.json"

# Line-level review comments (path + line + suggestion bodies).
gh api "repos/$OWNER_REPO/pulls/$PR/comments?per_page=100" --paginate \
  > "$SCRATCH/<prefix>-review-comments.json"

# Review summaries (state: CHANGES_REQUESTED / APPROVED / COMMENTED).
gh api "repos/$OWNER_REPO/pulls/$PR/reviews?per_page=100" --paginate \
  > "$SCRATCH/<prefix>-reviews.json"
```

## 2. Fetch the review threads (GraphQL)

Thread node IDs are what the reply / resolve steps need. Paginate —
a PR can have more than 100 review threads — and fetch every
comment in each thread (not just the one that opened it), since a
reply carries its own `databaseId` distinct from the opener's:

```bash
gh api graphql --paginate -f query='
  query($owner: String!, $repo: String!, $number: Int!, $endCursor: String) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $number) {
        reviewThreads(first: 100, after: $endCursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            isResolved
            isOutdated
            comments(first: 100) { nodes { databaseId } }
          }
        }
      }
    }
  }
' -f owner="${OWNER_REPO%/*}" -f repo="${OWNER_REPO#*/}" -F number=$PR \
  | jq -s '{data:{repository:{pullRequest:{reviewThreads:{
      nodes: (map(.data.repository.pullRequest.reviewThreads.nodes) | add)
    }}}}}' \
  > "$SCRATCH/<prefix>-threads.json"
```

`gh api graphql --paginate` recognises the `$endCursor` variable +
`pageInfo { hasNextPage endCursor }` shape and re-issues the query
per page automatically, emitting one JSON document per page; the
trailing `jq -s` merges every page's `nodes` back into the same
single-document shape every consumer already parses
(`.data.repository.pullRequest.reviewThreads.nodes[]`) — no caller
needs to change how it reads this file.

The mapping: each review-comment `databaseId` (surface #2 above) maps
to a thread `id` (GraphQL node ID) by matching against ANY of that
thread's `comments.nodes[].databaseId` — not just
`comments.nodes[0]` (the thread-opening comment). A reply posted
later in the same thread has its own `databaseId` and must resolve
to the same thread id as the opener's. Every queue skips threads that
are:

- `isResolved: true` — a person already marked the thread resolved.
- `isOutdated: true` — GitHub flagged the anchor stale (the referenced
  lines moved or were deleted since the comment was posted; GitHub
  renders these with an "Outdated" badge). The advice may no longer
  apply to the current diff; the bot re-posts if the concern survives
  the rebase.

## 3. The bot-author allowlist

An author is a BOT if **any** of the following matches. This is the
single alignment point between the human queue (which EXCLUDES these)
and the bot queues (whose `bot_filter` selects a subset of them):

- `.user.type == "Bot"` — GitHub's own flag. Necessary but not
  sufficient: Copilot's built-in reviewer has been observed to surface
  with `type: "User"` on the REST payload, so also apply the login
  checks below.
- login is the exact string `Copilot` — GitHub's built-in reviewer
  posts under this literal login. The bare `copilot-*` glob does NOT
  catch it (the glob needs a trailing hyphen the `Copilot` login
  doesn't have); this line is the one that matters.
- login is `copilot-pull-request-reviewer`, OR starts with `copilot-`
  (covers org variants like `copilot-pull-request-reviewer[bot]`).
- login is `coderabbitai` or `coderabbitai[bot]`.
- login is `github-actions` or ends with `[bot]`.
- login is `sonarcloud` or `sonarqubecloud`.

`bot_filter` values map onto this list exactly:

- `copilot` → `Copilot` OR `copilot-pull-request-reviewer` OR login
  starting with `copilot-`
- `coderabbit` → `coderabbitai` OR `coderabbitai[bot]`

Exact-login matching only — never substring-match on "bot" or a
vendor name (a human login merely containing it would be
misclassified; the same standard `sonar-fetch.md` applies to the
Sonar comment).

## 4. Resolve threads (`resolveReviewThread` mutation)

Loop over the thread IDs the caller decided are addressed:

```bash
RESOLVED=0
# Per-invocation error log: §4 may run in a different Bash invocation
# than the caller's mktemp, so never assume $SCRATCH is set — and a
# fixed filename under shared /tmp would let concurrent handler runs
# truncate each other's stderr before it is read.
ERRLOG="$(mktemp "${SCRATCH:-${TMPDIR:-/tmp}}/resolve-err-XXXXXX")"
for THREAD_ID in "${ADDRESSED_THREAD_IDS[@]}"; do
  if gh api graphql -f query='
    mutation($threadId: ID!) {
      resolveReviewThread(input: { threadId: $threadId }) {
        thread { isResolved }
      }
    }
  ' -F threadId="$THREAD_ID" 2>"$ERRLOG"; then
    RESOLVED=$((RESOLVED + 1))
  else
    echo "resolveReviewThread failed for THREAD_ID=$THREAD_ID: $(cat "$ERRLOG")" >&2
  fi
done
rm -f "$ERRLOG"
```

Failures (403, thread already resolved by someone else, no write
access) log + continue — the fix itself already landed locally; the
bot re-flags on its next pass if the concern still applies. Preserve
the error text (as above) rather than redirecting it to `/dev/null` —
a 403 / invalid-thread / permission failure should be visible as
something more than a lower resolve count. Report the successful
resolve count to the caller's verdict line.
