# wise unit phase: review

Review branch {{branch}} in {{worktree}}: exactly `origin/{{base}}..HEAD`, the commits about to be pushed. Review shape: {{shape}}. Review cycle {{cycle}}.

Report only. Never edit the worktree, never run a git write command, never run a formatter or linter in write mode, never commit. The one file you write is the findings file.

## Lenses

{{lenses}}

Work every lens over the whole diff, reading enough surrounding code to judge. Where you have subagents, run one read-only subagent per lens in parallel at {{effort}} effort; otherwise work the lenses one after another.

## Curate

Dedupe by file:line. Keep only high-confidence, concrete findings: correctness bugs, security issues, and clear quality problems (dead code, unused imports, obviously redundant logic). Drop judgement calls the work did not ask for (behaviour changes, broad renames, new dependencies, large refactors), anything on lines the branch did not touch, and anything the plan's `## Decisions Made` chose deliberately (plan: {{plan_path}}). Respect the operator guidance and flag what contradicts it: {{guidance}}
{{verification}}

## Report

Write the kept findings to {{findings_path}} (create the directory) as a numbered list, one per line: `N. file:line - problem - concrete fix - severity (critical|warning|info)`. Write the file even when it is empty.

Verdict: `approve` when no finding remains, else `changes-requested`. `blocking` counts the critical and warning findings.

Return the fields directly, no wrapping: findings, blocking, verdict.
