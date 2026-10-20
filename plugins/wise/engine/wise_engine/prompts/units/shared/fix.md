# wise unit phase: fix

Apply the findings in {{findings_path}} to branch {{branch}} in {{worktree}} (base {{base}}), nothing more. Source of the findings: {{source}}.

For each finding make the concrete fix it asks for. Respect the plan's deliberate decisions ({{plan_path}}) and the operator guidance ({{guidance}}): do not redesign, widen scope, or touch unrelated files. A finding you judge wrong or out of scope: skip it and give the one-line reason in your final summary.

{{instructions}}

Run the project's quick validation (type-check, lint, affected tests) before committing. Commit the fixes on this branch with a Conventional Commits subject, one commit per concern or one for all. Never push. No AI attribution trailer.

Once {{branch}} is on origin (a PR exists), never rewrite a commit origin already has: bring in {{base}} (a conflict with it, a fix that needs it) with `git fetch origin {{base}}` then `git merge {{base_ref}}`, resolve any conflict in the merge commit, and never `git rebase`, amend, squash or reorder a pushed commit. The engine pushes without force, so rewritten history cannot land. Never `--force`, never `--force-with-lease`, never `--no-verify`.

Return the fields directly, no wrapping: fixed, skipped, commits (commits you made).
