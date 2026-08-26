# wise-workflow-run/references/step-types — team, supervised, interactive, approval, ask

Companion to the conductor SKILL.md §10d. The common step types
(`skill`, `prompt` single-agent, `bash`) stay in the SKILL body; this
file carries the dispatch recipes for the heavier types. Read it the
FIRST time a wave actually contains one of them — a run whose
workflow uses none of these never loads it. Schema/semantics live in
`docs/wise/workflows.md § Step types`.

- `type: prompt` — **team** (`mode: team` → ≥2 members, conductor-synthesized):
  one logical step worked by several roster roles. Keep it **atomic** — on
  resume mid-step the step re-runs whole (members are idempotent producers),
  so no extra run state is needed.
  1. **Round 1 — parallel drafts.** Dispatch every **non-lead** member as a
     `Task` **in one message** (they run concurrently). Each: `subagent_type:
     "wise:<member.role>"`, its `model` (omit if `inherit`), and `prompt` = the
     rendered `def.prompt` prefixed with one framing line — *"You are the
     `<role>` on a panel. Give your `<role>` perspective on the task below; do
     NOT try to produce the team's final answer — a synthesis step does that."*
     — plus the member's effort directive. Do **not** pass `until:` to members
     (it governs the synthesis only). Capture each member's returned text as its
     draft and log it under the step log.
  2. **Round 2 — lead integration** (only when `lead` is set). Dispatch the
     lead as one `Task` (`wise:<lead>`, its model/effort) with `prompt` = the
     rendered `def.prompt` + *"\n\nYour panel produced these inputs:\n`<role>`:
     `<draft>`\n…\n\nReconcile them into one integrated recommendation; call out
     where they disagree."* Capture the lead's proposal.
  3. **Synthesis — you, the conductor** (main thread, **no** subagent). Merge
     the drafts (and the lead proposal, if any) into the step's single result:
     dedup overlaps, combine complementary points, surface disagreements. This
     synthesis IS the step output — if `def.until` is set, end it with the
     matching final line; if `def.outputs` is set, extract from it. Append the
     synthesis to the step log beneath the member drafts. Remember every
     member's `agent`/`model`/`effort` + the `lead` — 10e reports them.

  **Important — `prompt` steps run in an isolated Task subagent.**
  The subagent has its own tool list (for a `general-purpose` dispatch,
  the full set; for a `wise:<role>` dispatch, the role's scoped `tools`
  frontmatter — the reason auto-selection above is tool-aware) but
  **cannot** call `AskUserQuestion` — that tool only works in the main
  conversation. If the step needs to walk the user through a per-item
  wizard, use `type: interactive` below instead. A `prompt` step that tries to AskUserQuestion silently
  degrades (the subagent typically falls back to "list the items
  and return a summary"), which is worse than the step failing
  loudly because it looks like the step worked.

- `type: supervised-prompt` — a `prompt` step run as a **supervised background
  worker** instead of a blocking `Task`, so a worker that hangs mid-turn or goes
  idle without finishing gets nudged back on task rather than silently stalling
  the wave (`Task` has no timeout/heartbeat — a hung subagent hangs the conductor
  indefinitely). Resolve `agent`/`model`/`effort` exactly as `prompt`
  (`resolve-team` → a single member; a team **list** is not supported here — one
  supervised step is one worker). Then follow
  `${CLAUDE_PLUGIN_ROOT}/references/supervise-loop.md`:
  1. `TeamCreate({ team_name: "wise-<run.id>-<step.id>" })`.
  2. `TaskCreate` the step's goal, then spawn ONE background worker:

     ```text
     Agent({
       team_name: "wise-<run.id>-<step.id>",
       name: "<step.id>-w1",
       run_in_background: true,
       subagent_type: <"wise:<role>" | "general-purpose">,
       model: <member.model — include ONLY if not "inherit">,
       prompt: "<rendered def.prompt>"
               + [if member.effort] "\n\nReason at <EFFORT> effort — <gloss>."
               + [if def.until]  "\n\nEnd your last line with a value that matches /<until>/."
               + "\n\nHeartbeat: as your FIRST action each turn and after each significant tool call, run:\n"
               + "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py\" worker-heartbeat \"<run.dir>\" \"<step.id>-w1\" \"<phase>\" \"<step.id>\""
     })
     ```
  3. Arm the supervisor Monitor (`supervise-loop §3`) over the single worker name
     and run the loop — idle handler (§4), Monitor stale handler (§5), escalation
     ladder (§7) — until the worker's task is `completed`/`failed`.
  4. **Teardown** (`§8`): `TaskStop` the Monitor, collect `TaskOutput` as the step
     result (capture `def.until` / `def.outputs` from it exactly as a `prompt`
     step), shut the worker down, `TeamDelete`. The step stays **atomic** — a
     mid-step resume re-runs it whole, after `TeamDelete`-ing the orphaned team
     (`§9`). All execution is in-conversation (subscription-covered) — the worker
     is a native background `Agent`, never a `claude -p` subprocess.

- `type: interactive`:

  Runs inline in the conductor's main conversation instead of
  being spawned as a Task subagent. The conductor — that's you —
  reads the step's `prompt` body and executes it directly, with
  full main-thread tool access (Read, Edit, Write, Bash,
  AskUserQuestion, TodoWrite, etc.).

  Use this for step bodies that drive per-item wizards, iterate
  over a variable-length list with user decisions per iteration,
  or otherwise need to prompt the user mid-step. Pre-0.49
  workflows that used `type: prompt` for this pattern quietly
  lost the user-facing prompts (see the warning above) — switch
  them to `interactive`.

  Execution model:
  1. Render `def.prompt` via the template engine (`{{…}}`
     variables) exactly as for a `prompt` step.
  2. Read the rendered prompt in chat and follow it, emitting
     the final line as the prompt's `until:` contract demands.
  3. Capture outputs from the final line's regex groups via
     `record-output` just like a `prompt` step.

  Trade-offs vs `prompt`:
  - Pro: full tool access, AskUserQuestion works, richer dispatch
    logic across turns (since the conductor stays in context).
  - Con: **sequential only**. An interactive step blocks the
    conductor's main conversation until it completes, so two
    interactive steps in the same wave cannot run in parallel
    (one must finish before the other starts). Non-interactive
    parallel steps in the same wave still run via Task as usual.
  - Con: context budget — the conductor accumulates tool output
    into the main conversation, unlike a Task subagent which
    releases its turn transcript back on return. Use for flows
    that genuinely need user interaction; stay on `prompt` for
    anything that can complete unattended.

  **In `wave-sync` or `auto-advance` mode** an interactive step may
  call `AskUserQuestion` freely — that's the point of `interactive`
  (e.g. `ticket-plan`'s `setup` questionnaire).

  **In synchronous mode** an interactive step still runs inline and
  captures its `until:` / `outputs:` exactly as above — `interactive`
  is chosen for main-thread tool access, not for prompting. It just
  must not call `AskUserQuestion` (sync mode is a blanket approval);
  treat any decision the body would prompt for as auto-approved and
  proceed, the same way `approval` steps do. `ticket-auto`'s
  `process-tickets` step depends on this combination — it is
  `interactive` for tool access while the run is `synchronous`.


- `type: approval`:

  **In wave-sync or auto-advance mode** — use `AskUserQuestion`:
  ```
  AskUserQuestion({
    question: "<def.message>",
    header: "Approval — <step.id>",
    options: [
      { label: "Approve", description: "Mark this step completed and continue." },
      { label: "Reject",  description: "Mark this step failed and stop the dependent branch." },
    ]
  })
  ```

  **In synchronous mode** — auto-approve. Picking synchronous at
  pre-flight is itself an implicit blanket approval to run the whole
  DAG through without stopping. The step transitions directly to
  `completed`, and a one-line note `[sync auto-approved]` is written
  to its log file so the decision is auditable after the fact. Do
  NOT emit `AskUserQuestion` in sync mode — it would stall the run
  and defeat the point of picking synchronous. If the workflow
  genuinely needs a human gate, the user should have picked
  wave-sync or auto-advance (or the gate should be upgraded to a
  `type: prompt` step that encodes the check as a programmable
  condition).

- `type: ask`:

  Interactive step that captures an answer from the user and
  records it as a named output so downstream step templates can
  reference it as `{{<output-name>}}`. Two rendering shapes —
  which one you get depends on whether `confirm_label` is
  declared:

  **Shape A — free-text capture** (no `confirm_label`). The
  natural fit for "give me a comment" / "type a value":

  ```yaml
  - id: user-comments
    type: ask
    question: "<question text>"       # required
    header: "<chip label>"            # optional — ≤12 chars, defaults to step id
    output: user_comments             # required — name under state.outputs
    skip_label: "Skip"                # optional — defaults to "Skip"
  ```

  `AskUserQuestion` options:
  - `<def.skip_label or 'Skip'>` — description: `Record an empty value and continue.`
  - `Provide input` — description: `Type your answer via the free-text Other affordance.`

  Map the result:
  - Picked the skip label → `answer = ""`.
  - Picked "Provide input" → the user's Other-text is the answer.
  - The user picked Other directly with text → that text is the
    answer.

  **Shape B — binary choice** (`confirm_label` is declared). The
  natural fit for "yes/no" / "opt-in to this extra stage":

  ```yaml
  - id: ask-watch
    type: ask
    question: "<question text>"       # required
    header: "<chip label>"            # optional
    output: watch_choice              # required
    skip_label: "No — I'll watch manually"       # optional — defaults to "Skip"
    confirm_label: "Yes — watch pipelines"       # required for Shape B
    confirm_value: "yes"                         # optional — defaults to confirm_label verbatim
  ```

  `AskUserQuestion` options:
  - `<def.skip_label>` — description: `Record an empty value and continue.`
  - `<def.confirm_label>` — description: `Record "<def.confirm_value or confirm_label>" and continue.`

  Map the result:
  - Picked the skip label → `answer = ""`.
  - Picked the confirm label → `answer = def.confirm_value` (or
    `def.confirm_label` if `confirm_value` isn't set).
  - The user picked Other directly with text → **ignore** and
    re-prompt. Shape B is deliberately binary; free-text doesn't
    apply.

  Downstream steps gate with `when: "<output> != ''"` or
  `when: "<output> == '<confirm_value>'"`.

  Pick Shape B whenever the question is yes/no — the "Provide
  input" free-text affordance in Shape A misleads users into
  thinking they need to type `yes`.

  Record the answer and mark the step completed (both shapes):

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" record-output \
    "$STATE" "<def.output>" "<answer>"
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/workflows.py" update-step \
    "$STATE" "<step.id>" status=completed completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  ```

  **In `wave-sync` or `auto-advance` mode** — render the prompt
  normally (both shapes), exactly as above.

  **In synchronous mode** — no prompt. The step records an empty
  string for `<def.output>` (both shapes) and transitions straight
  to `completed`, with a one-line `[sync skipped ask]` note in its
  log. If the workflow genuinely needs a user answer, the user
  should pick wave-sync or auto-advance at pre-flight.

