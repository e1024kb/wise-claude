# grill/gap-analysis — score the dossier, decide READY vs GAPS, craft the questions

Single source of truth for **how** wise judges whether a researched
subject is actionable — plannable for work, answerable for a
question — and, when it is not, how it turns each gap into a question
the right person can answer in one message. Read by:

- `skills/wise-grill/SKILL.md` §5 — the standalone `/wise-grill` gate
  (any subject type: ticket, doc, prompt, question).
- `workflows/ticket-plan/workflow.yaml` `gap-analysis` step — the
  interactive workflow's pre-plan gate (ticket subjects).
- `workflows/ticket-auto/prompts/plan-ticket.md` §6 — the autonomous
  variant (ticket subjects; see "Autonomous mode" below).

Input: the **Context Dossier** produced by
[`research-sources.md`](./research-sources.md), plus the caller's
subject type. Everything below applies to every type; the
question-type subset is called out where it differs.

## 1. Score the ten dimensions

For each dimension, give a verdict with one line of evidence:

| # | Dimension | Critical? |
|---|---|---|
| 1 | **Goal** — the user / business problem this solves (the WHY) | always |
| 2 | **Scope** — what is in, and explicitly what is out (the WHAT) | always |
| 3 | **Acceptance** — how anyone will know it is done | always |
| 4 | **Lexicon** — every load-bearing term resolved | always |
| 5 | **Design / UX** — expected screens, states, copy | user-facing subjects |
| 6 | **Technical constraints** — integration points, contracts, perf / compat requirements | when they steer the approach |
| 7 | **Dependencies** — other teams, tickets, migrations, feature flags | when evidence hints at one |
| 8 | **Data & edge cases** — inputs, volumes, error paths | logic-heavy subjects |
| 9 | **Verification** — how the change will be tested / demoed | always |
| 10 | **Risk / rollout** — migration, backwards compat, staged release | production-impacting subjects |

**Question-type subjects** (the caller wants an answer, not work) use
the subset that still applies: **1 Goal** = what is *really* being
asked and why; **2 Scope** = the boundaries of a satisfying answer
(which system, which timeframe, how deep); **4 Lexicon** as always;
**3 Acceptance / 9 Verification** = whether the evidence found is
strong enough to state the answer with confidence. Dimensions 5–8 and
10 are `n/a` unless the question is about them. READY here means
*answerable from the evidence*; the deliverable is the caller's ANSWER
artifact, not a plan.

Verdicts:

- **CLEAR** — evidence-backed in the dossier; cite the source.
- **ASSUMED** — not stated anywhere, but responsibly inferable; state
  the assumption and a confidence (`high` / `medium` / `low`) grounded
  in how strong the supporting evidence is.
- **UNKNOWN** — cannot be inferred without guessing; guessing here
  would risk building the wrong thing.

A dimension marked non-critical for this subject kind is scored `n/a`.

## 2. The readiness rule

- **READY** — no critical dimension is UNKNOWN, and no critical
  dimension rests on a `low`-confidence ASSUMED. Every ASSUMED carries
  into the plan's `## Assumptions` verbatim.
- **GAPS** — anything else. Each failing dimension becomes one or more
  questions (§3); the caller writes the blueprint
  ([`blueprint-format.md`](./blueprint-format.md)) instead of a plan.
  When the questions target the user themselves (prompt / question
  subjects, or fallback-routed questions), a caller with
  `AskUserQuestion` access (the standalone skill — not a workflow
  prompt-step subagent, which always writes the blueprint) walks them
  inline first and writes a blueprint only for what stays open — the
  caller's fork defines the exact flow.

The bar is *"would a senior engineer start building on this, or walk
over to someone's desk first?"* (for a question: *"would they state
this answer to their team as fact?"*) — not perfection. Nice-to-know
curiosity never blocks READY.

### Autonomous mode (`ticket-auto`)

An unattended run has nobody to ask, so the rule tightens at the top
and loosens below:

- **Goal or Scope UNKNOWN** → the ticket is unplannable; do NOT plan
  from guesses. Write the blueprint and emit the caller's blocked
  verdict (`reason=insufficient-context`). This is the same fail-closed
  posture as an unreachable tracker.
- **Every other gap** → the Lead Architect converts it to a decision:
  predict the most probable answer, record it as an ASSUMED entry in
  the plan's `## Assumptions` with its confidence, and proceed. The
  operator's `config_prompt` guidance outranks prediction wherever it
  implies an answer.

## 3. Craft the questions (the "grill")

First, two filters that kill most candidate questions:

- **Facts vs. decisions.** A *fact* that any reachable source could
  still answer (a file to read, a doc to search) is research debt, not
  a question — go look it up. Only *decisions* — preference, intent,
  priority, judgement — get escalated to a human.
- **Impact × uncertainty.** Rank the survivors by how much the answer
  changes what gets built × how unsure the evidence leaves you. A
  question that would not alter the plan is dropped, however curious.

**Budget: 5 questions, hard cap 7.** Question fatigue is the
documented failure mode of every "grill" tool — a ticket that
genuinely needs more than 7 needs a meeting, and the blueprint should
say exactly that instead of listing 30 questions. When several gaps
share one root decision, **propose 2–3 approaches with trade-offs and
a recommendation** as a single question — an approach choice absorbs
many small decisions at once.

For each surviving question:

1. **Lead with the recommended answer.** Closed beats open: *"We plan
   to X (recommended, because Z) — correct, or should it be Y?"* is
   answerable in one word; *"What should happen?"* pushes the work
   back. Offer 2–5 mutually exclusive options where they exist; a
   free-text question should be answerable in ≤ 5 words.
2. **Show your homework.** One `Context:` line stating what the
   research already found (with source), so the answerer never
   re-explains the known. Reference the real artifacts (file paths,
   doc titles) but phrase the question itself for the addressee's
   world — a PM gets product language even when the context line
   cites `services/UserService.ts`.
3. **State the default.** One `Default if unanswered:` line — the
   assumption the plan will proceed on if no answer arrives. This
   turns silence into a decision instead of a stall, and "accept the
   default" into the cheapest possible reply.
4. **Target a person, not the void.** Pick the addressee from the
   dossier's People map, by gap type:
   - Goal / intent → the **reporter** (they wanted it). For a
     prompt / question subject the reporter IS the user — their
     questions are asked inline, not parked in a blueprint.
   - Scope / acceptance / priority → the **PM / product owner** on the
     epic or linked PRD. For a doc subject, the **doc author** is the
     first candidate for anything the page itself left open.
   - Design / UX → the **designer** on the linked design file.
   - Technical constraints → the **code owner** — the dominant recent
     author of the affected area (`git log` from the dossier).
   - An attributed claim (Slack / comment / doc revision) → the
     **person who made it**, to confirm it still holds.
   No name in evidence → target the **role** and say how to find them
   ("whoever owns the payments service"); no role identifiable
   either → the question falls back to the **user** — in interactive
   callers asked inline, in a blueprint under a `### → You (requester)`
   block.
5. **Batch per person, order by blocking impact** — the question that
   unblocks the most plan sits first, and upstream decisions come
   before the questions they could invalidate.

## Output

Emit the scorecard, then the verdict — both back to the caller:

```markdown
## Gap analysis — <subject ref>
| dimension | verdict | evidence / assumption |
|---|---|---|
…
**Verdict: READY** (n assumptions carried) — or —
**Verdict: GAPS** (n questions for m people)
```

On GAPS, hand the question set (grouped by addressee, in §3 form) to
the caller for the blueprint.
