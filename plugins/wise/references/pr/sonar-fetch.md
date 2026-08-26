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
  `AUTH-FAIL` / `NOT_FOUND` / `FETCH-FAIL` — defined at the end of §2.
  The caller owns what each bucket means (gate, verdict, postponement);
  callers that don't need the 404/other-failure distinction (the
  interactive handler) may treat `NOT_FOUND` the same as `FETCH-FAIL`.

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

# b) sonar-project.properties / .sonarcloud.properties (SonarCloud's
#    automatic-analysis config carries the same sonar.projectKey line).
for f in sonar-project.properties .sonarcloud.properties; do
  if [ -z "$SONAR_KEY" ] && [ -f "$f" ]; then
    SONAR_KEY="$(grep -E '^sonar\.projectKey[[:space:]]*=' "$f" \
      | head -1 | awk -F= '{gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2}')"
  fi
done

# c) pom.xml for Maven. A multi-module pom.xml can declare more than
#    one <sonar.projectKey> element; take the first match only so
#    SONAR_KEY never ends up holding several newline-joined keys.
if [ -z "$SONAR_KEY" ] && [ -f pom.xml ]; then
  SONAR_KEY="$(grep -oE '<sonar\.projectKey>[^<]+</sonar\.projectKey>' pom.xml \
    | head -1 | sed 's/<[^>]*>//g')"
fi

# c2) build.gradle / build.gradle.kts for Gradle — the sonarqube
#     plugin declares the key as property("sonar.projectKey"), "value"
#     (Kotlin) or property "sonar.projectKey", "value" (Groovy).
if [ -z "$SONAR_KEY" ]; then
  for f in build.gradle build.gradle.kts; do
    if [ -z "$SONAR_KEY" ] && [ -f "$f" ]; then
      SONAR_KEY="$(grep -oE 'property[( ]"sonar\.projectKey",[[:space:]]*"[^"]+"' "$f" \
        | head -1 | sed -E 's/.*,[[:space:]]*"([^"]+)".*/\1/')"
    fi
  done
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

1. **`$SONAR_TOKEN`-authenticated curl** (when the env var is set).
   Pass the token through a temporary `--netrc-file`, never through
   `-u`/argv — an argument-list credential is visible to any other
   process on the host that can read `/proc/<pid>/cmdline` or run
   `ps`:
   ```bash
   NETRC_FILE="$(mktemp "${TMPDIR:-/tmp}/wise-sonar-netrc-XXXXXX")"
   chmod 600 "$NETRC_FILE"
   printf 'machine sonarcloud.io login %s password\n' "$SONAR_TOKEN" > "$NETRC_FILE"
   HTTP_CODE="$(curl -sSL --netrc-file "$NETRC_FILE" \
     -o "$SCRATCH/sonar-issues-$PR.json" -w '%{http_code}' \
     "https://sonarcloud.io/api/issues/search?componentKeys=$SONAR_KEY&pullRequest=$PR&issueStatuses=OPEN,CONFIRMED&resolved=false&ps=500")"
   rm -f "$NETRC_FILE"
   ```
2. **Anonymous curl** (public projects only) — same `-w '%{http_code}'`
   capture, no `--netrc-file`.

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

Outcome buckets (decided by the issues-search call's HTTP status
alone, no separate probes):
- MCP call succeeded / `HTTP_CODE` is `200` (any count, including 0) →
  `OK`; parse results.
- `HTTP_CODE` is `401` or `403`, OR `$SONAR_TOKEN` unset on a
  private project → `AUTH-FAIL`.
- `HTTP_CODE` is `404` (the component key doesn't exist) →
  `NOT_FOUND` — kept distinct from the generic fetch failure below so
  a caller can tell "this key is wrong" from "the call itself broke"
  (the autonomous handler's §1b needs exactly that distinction to
  choose between `not-configured` and `blocked-fetch reason=key-unresolved`).
  CAVEAT (verified empirically): an **anonymous** issues-search does
  NOT 404 on a missing component — SonarCloud returns `200` with
  `total: 0` for any unknown key. `NOT_FOUND` is therefore realistic
  only on authenticated calls; an anonymous caller that needs to
  distinguish "missing project" from "empty project" must use the
  existence probe the autonomous handler's §1b defines
  (`api/measures/component`, which 404s reliably without auth).
- Network error / MCP error on the issues-search call, or any other
  non-200/401/403/404 status → `FETCH-FAIL`.
