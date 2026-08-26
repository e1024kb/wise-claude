# sonar-fetch — SonarCloud component-key discovery + issue fetch

Shared routine extracted from `handle-sonar-issues.md` §1–§2 so both
the interactive handler (`references/pr/handle-sonar-issues.md`) and
the autonomous one
(`workflows/ticket-auto/prompts/handle-sonar-issues-auto.md`) run the
identical queries without either loading the other's full procedure.

Caller supplies `pr_number` and `project.path` (run everything with
`cd <project.path>` first). The routine produces:

- `SONAR_KEY` (always set — step (d) guesses as a last resort) and
  `SONAR_KEY_GUESSED=true` when it came from the `<org>_<repo>`
  convention rather than a corroborated source.
- One fetch outcome bucket: `OK (N issues)` / `OK (0 issues)` /
  `AUTH-FAIL` / `FETCH-FAIL` — defined at the end of §2. The caller
  owns what each bucket means (gate, verdict, postponement).

### 1. Discover the SonarCloud component key (authoritative)

The cleanest source is SonarCloud's bot comment on the PR —
its link to the issues page contains the `id=<key>` query
param, which IS the project key. Try that first; fall back to
config files; fall back to the `<org>_<repo>` convention last.

```bash
PR=<pr_number>
OWNER_REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"
SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/wise-pr-XXXXXX")"

# a) SonarCloud bot comment — parse id=<key> from the issues URL.
#    EXACT logins only. This key selects WHICH project's verdict gates
#    the merge, so a substring match on "sonar" would let any commenter
#    whose login merely contains it (e.g. "sonarfan") point the gate at
#    a project they own - an empty one returns 0 issues and reads as
#    clean. Same exact-login standard the bot allowlists use.
SONAR_KEY="$(gh api "repos/$OWNER_REPO/issues/$PR/comments" \
  --jq '.[] | select(.user.login as $l | ["sonarqubecloud[bot]","sonarqubecloud","sonarcloud[bot]","sonarcloud","sonarqube[bot]","sonarqube"] | index($l)) | .body' \
  | grep -oE 'sonarcloud\.io/[^)"]*[?&]id=[^&)"[:space:]]+' \
  | head -1 | sed -E 's/.*[?&]id=([^&)"[:space:]]+).*/\1/')"

# b) sonar-project.properties.
if [ -z "$SONAR_KEY" ] && [ -f sonar-project.properties ]; then
  SONAR_KEY="$(grep -E '^sonar\.projectKey[[:space:]]*=' sonar-project.properties \
    | head -1 | awk -F= '{gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2}')"
fi

# c) pom.xml for Maven.
if [ -z "$SONAR_KEY" ] && [ -f pom.xml ]; then
  SONAR_KEY="$(grep -oE '<sonar\.projectKey>[^<]+</sonar\.projectKey>' pom.xml \
    | sed 's/<[^>]*>//g')"
fi

# d) Last resort — the common <org>_<repo> convention. Mark as a guess.
if [ -z "$SONAR_KEY" ]; then
  SONAR_KEY="$(echo "$OWNER_REPO" | tr '/' '_')"
  SONAR_KEY_GUESSED=true
fi
```

### 2. Fetch the issues — prefer the Sonar MCP

If Claude Code sees any tool matching `mcp__*sonar*__*`
(the MCP naming convention for Sonar servers — e.g.
`mcp__*__search_sonar_issues_in_projects`,
`mcp__*__issues_search`, or similar), **prefer it**. The MCP's
stored credentials handle auth transparently and sidestep the
401-on-private-project trap.

Fallback order if no MCP is visible:

1. **`$SONAR_TOKEN`-authenticated curl** (when the env var is set):
   ```bash
   curl -fsSL -u "$SONAR_TOKEN:" \
     "https://sonarcloud.io/api/issues/search?componentKeys=$SONAR_KEY&pullRequest=$PR&issueStatuses=OPEN,CONFIRMED&resolved=false&ps=500" \
     > "$SCRATCH/sonar-issues-$PR.json"
   ```
2. **Anonymous curl** (public projects only).

**The issues-search endpoint is authoritative.** Do **not** run
a separate "sanity-check" probe against the project key
(`/api/components/show`, `/api/projects/search`, etc.) and then
escalate the queue to FETCH-FAIL on its 404 while the
issues-search call itself returned 200. SonarCloud's permission
model lets a token read issues without granting read access to
the component metadata, so a key that 404s on `components/show`
can still be the correct key for this PR. The pre-2.6.3 LLM-
improvised sanity-check produced a "200/0 issues but key 404'd"
deadlock that asked the user 4 questions when the right answer
was always "trust the 200/0 and emit all-clear".

Outcome buckets (decided by the issues-search call alone, no
separate probes):
- MCP call succeeded / curl exit 0 (any count, including 0) →
  `OK`; parse results.
- Issues-search HTTP 401 / 403 OR `$SONAR_TOKEN` unset on a
  private project → `AUTH-FAIL`.
- Issues-search HTTP 404 (truly bad `SONAR_KEY`) →
  `FETCH-FAIL`.
- Network error / MCP error on the issues-search call /
  anything else → `FETCH-FAIL`.
