---
name: wise-profile
description: >-
  Set the session's token-budget profile — low, medium, or max — which
  every profile-sensitive wise skill and workflow reads at start to
  scale model tiers, optional research scope, reviewer-panel size, and
  retry caps. Budget only: it NEVER changes correctness rules (commit
  conventions, dirty-tree refusals, review gates, push refusals).
  Bare invocation shows a three-option picker; `/wise-profile low`
  sets directly. Persists per session (default: medium = the plugin's
  standard behavior). Invoked as `/wise-profile` (bare alias) or
  `/wise:wise-profile` (canonical). Use when the user says "set
  profile", "low budget mode", "token budget", "cheaper runs", "max
  quality mode", or types `/wise-profile`.
argument-hint: "[low|medium|max]"
model: opus
effort: low
allowed-tools: Bash(python3:*), Bash(cat:*), Bash(mkdir:*), Bash(printf:*), Bash(mv:*), ToolSearch, AskUserQuestion
---

# /wise-profile — session token-budget profile

## Why this skill exists

wise's autonomous skills and workflows default to their standard
depth (opus tiers, full research, full reviewer panels). On API
billing that depth costs real money; on a subscription it is usually
what you want. This skill is the one knob: set the budget once per
session, and every profile-sensitive skill reads it silently.

What each level means (each consumer carries its exact mapping
table; `medium` = the plugin's standard behavior):

| level | meaning |
|---|---|
| `low` | cheapest run that keeps every gate: sonnet tiers for research / implementation (planning/authoring stay on Opus — **Opus 4.8, never Opus 5**: a MUST rule, see below), minimal optional research, solo leads instead of panels, low retry caps |
| `medium` | the standard defaults (set this to undo a `low`/`max`) |
| `max` | everything on: opus tiers across phases, full research, review findings adversarially verified before apply |

The hard invariant, at every level: profiles change model tier /
optional-step scope / team size / caps ONLY — never correctness
rules. Commit conventions, dirty-tree refusals, review gates, and
push refusals are identical across levels.

**The low-profile Opus rule (MUST).** Under `low`, wise NEVER
dispatches Opus 5. Every Opus-tier dispatch — planning / authoring
steps, the review-gate panel, the fixer, the PR watcher's fallback
reviewer — runs on `claude-opus-4-8` instead. Sonnet-tier dispatches
are unaffected. The engine enforces it on every model resolved under
`low` (`get-profiles` for the `low` level, `resolve-model` /
`resolve-team --profile low`), and every profile-sensitive skill reads
it as `PROFILE_OPUS_MODEL` from `references/profile-read.md`.

## Invocation

```
/wise-profile            # picker
/wise-profile low        # direct set
/wise:wise-profile max   # canonical form
```

## Procedure

### 1. Parse the argument

Read `$ARGUMENTS`. Take the first whitespace-separated token:

- `low`, `medium`, or `max` → go to §3 with that level.
- Empty / whitespace-only → go to §2 (picker).
- Anything else (including extra tokens after a valid level) → print
  `Usage: /wise-profile [low|medium|max]` and stop.

### 2. Picker (bare invocation)

Load `AskUserQuestion` via `ToolSearch` if it is not already
available, then ask ONE question:

- question: `Token-budget profile for this session?`
- header: `Profile`
- multiSelect: false
- options:
  1. `medium (Recommended)` — "The standard defaults — full quality,
     standard cost. Pick this to reset a low/max session."
  2. `low` — "Cheapest run that keeps every gate: sonnet tiers
     (planning/authoring stay on Opus 4.8 — never Opus 5), minimal
     optional research, solo leads, medium-effort reviews, low retry
     caps. Correctness rules unchanged."
  3. `max` — "Everything on: opus across phases, full research,
     review findings adversarially verified before apply. Highest
     cost."

### 3. Persist

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" profile-set <level>
```

It prints `PROFILE: level=<level> scope=session session=<sid>` on
success. On `INVALID:profile-level:*` (should be unreachable after
§1) print the usage line and stop.

If the command fails because python3 is missing, fall back to pure
shell — but ONLY when a session id is resolvable from the
environment; never guess one:

```bash
sid="${CLAUDE_CODE_SESSION_ID:-${WISE_SESSION_ID:-}}"
# Same token rule as workflows.py's _profile_safe_sid():
case "$sid" in ""|.|..|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*) sid= ;; esac
[ -n "$sid" ] || { echo "Cannot resolve a session id — run /wise-init to install python3, then retry."; exit 1; }
d="${XDG_DATA_HOME:-$HOME/.local/share}/wise/profile"
mkdir -p "$d"
printf '%s\n' "<level>" > "$d/$sid.tmp.$$" && mv "$d/$sid.tmp.$$" "$d/$sid"
```

### 4. Confirm

Tell the user in one line what is now active and what it affects,
e.g. `Profile low set for this session — sonnet tiers for research / implementation (planning/authoring on Opus 4.8, never Opus 5), minimal research, solo leads, medium-effort reviews. Reset with /wise-profile medium.`

Your response's FINAL line MUST be exactly, on its own line:

```
PROFILE: level=<low|medium|max> scope=session
```

## Examples

- `/wise-profile low` → sets low, prints the confirmation + final line.
- `/wise-profile` → picker → user picks max → sets max.
- `/wise-profile turbo` → `Usage: /wise-profile [low|medium|max]`.

## Guardrails

- Session-scoped only — never write a global or per-repo default.
- Budget only: never present this as changing review-gate existence,
  commit rules, or push behavior — it cannot.
- Consumers read the store through
  `${CLAUDE_PLUGIN_ROOT}/references/profile-read.md` and degrade to
  `medium` silently; a missing store is never an error.
- Never invoke another wise action skill from here.
