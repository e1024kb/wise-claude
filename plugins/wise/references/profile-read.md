# profile-read — resolve the session token-budget profile

Shared preamble for every profile-sensitive wise skill. Resolves the
level `/wise-profile` stored for THIS session — `low`, `medium`, or
`max` — with silent degradation: any failure at any stage (no store,
no session id, no python, garbage content) yields `medium`, which by
contract equals the consumer's pre-profile behavior. Never print a
warning for a missing profile — most users never set one.

## The read

Fire this in the same assistant message as the caller's own first
data calls (one message, no text between tool uses — the same pattern
as `init-check.md`):

```bash
sid="${CLAUDE_CODE_SESSION_ID:-${WISE_SESSION_ID:-}}"
[ -z "$sid" ] && sid="$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" current-session-id 2>/dev/null)"
# Same token rule as workflows.py's _profile_safe_sid(): plain token,
# alnum first char, [A-Za-z0-9._-] only, never a path or dot-name.
case "$sid" in ""|.|..|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*) sid= ;; esac
level="$(cat "${XDG_DATA_HOME:-$HOME/.local/share}/wise/profile/${sid:-none}" 2>/dev/null)"
# no external trim tool (consumers only grant cat/python3): command
# substitution strips trailing newlines, and the case guard rejects
# anything else anyway
case "$level" in low|medium|max) ;; *) level=medium ;; esac
# Low-profile Opus rule (MUST): `low` never dispatches Opus 5.
case "$level" in low) opus_model=claude-opus-4-8 ;; *) opus_model=opus ;; esac
echo "PROFILE_LEVEL=$level"
echo "PROFILE_OPUS_MODEL=$opus_model"
```

Pure shell on the happy path (env var + `cat`) so it works before
Python is installed; the `workflows.py current-session-id` fallback
covers older Claude Code builds without the env var, and its failure
falls through to `medium` like everything else.

## Interpretation rules

- `PROFILE_LEVEL` scales **token budget only**: model tiers, optional
  research scope, reviewer effort, team size, and retry caps.
  Each consuming skill owns its concrete mapping table — this fragment
  never prescribes one, with ONE exception, the low-profile Opus rule:
- **MUST — `PROFILE_OPUS_MODEL` is the only Opus model id a consumer
  may dispatch.** It is `opus` (the alias — the latest Opus, Opus 5) at
  `medium` / `max` and `claude-opus-4-8` at `low`: under the `low`
  profile wise NEVER dispatches Opus 5. Wherever a consumer's mapping
  table says "opus" for a `Task` `model:` (a reviewer, a planner, a
  fixer, a verification pass), pass `$PROFILE_OPUS_MODEL`, never the
  literal alias. Sonnet / haiku tiers are unaffected. Workflow runs
  carry the same value as the `opus_model` run output, and the engine
  applies the same rule to every model resolved with `--profile low`.
- The invariant every mapping honors: **profiles never change
  correctness rules** — commit conventions, dirty-tree refusals, the
  existence of review gates, push refusals are identical at every
  level.
- A consumer that also accepts a per-run `--profile <level>` argument
  gives that argument precedence over the session store (the store
  stays untouched — the override is for that one invocation).
- Session-id drift across harnesses (a store written under an env-var
  id, read later via a different resolution path) simply misses the
  file and degrades to `medium` — safe by design.
