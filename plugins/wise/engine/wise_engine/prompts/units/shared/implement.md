# wise unit phase: implement

Implement the plan at {{plan_path}} in the worktree {{worktree}} on branch {{branch}} (base {{base}}, project kind {{project.kind}}). This run is autonomous: never ask a question. Operator guidance (binding): {{guidance}}

## Procedure

1. Read the plan: `## Tasks` (waves and tasks), `## Decisions Made`, `## Assumptions`, `## Validation`. Reuse the assets the plan names before writing new code.
2. Waves in plan order. Wave N+1 starts only when every task of wave N is committed or recorded failed. Tasks inside a wave are independent: where you have subagents, give each task its own fresh subagent (only its task plus the plan's decisions; subagents edit, they never run git); otherwise do the tasks one after another.
3. Per task, after its edits: tidy the touched files (clarity, dead code, consistency; behaviour unchanged), stage only that task's files, commit once with a Conventional Commits subject (scope from the branch key when one exists). One atomic commit per task. No AI attribution trailer.
4. After each commit run the plan's validation relevant so far (type-check, lint, the task's tests; infer the commands from the project when the plan names none). On failure make one fix attempt and amend (nothing is pushed yet). Still failing: keep the commit, record the task failed, continue.
5. A failed task never aborts the run. Finish the wave, carry on.

Never push. Never edit files outside the worktree. Never run git concurrently on this worktree.

Return the fields directly, no wrapping: waves, tasks, done, failed, commits (commits you made).
