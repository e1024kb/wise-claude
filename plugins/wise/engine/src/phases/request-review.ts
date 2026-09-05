// `request-review`: attach the configured reviewers with `gh pr edit --add-reviewer`
// (request-review-auto.md §1-2). Best effort throughout: a missing PR, an unknown login or
// a `gh` failure is logged and never fails the unit. The watch phase confirms the bots.

import { errText, gh, jsonOf, ok, pass } from "./common.ts";
import type { PhaseCtx, PhaseResult, PhaseRunner } from "./common.ts";

type ReviewRequests = { reviewRequests?: unknown };

async function currentReviewers(ctx: PhaseCtx, number: number): Promise<Set<string>> {
  const r = await gh(ctx, ["pr", "view", String(number), "--json", "reviewRequests"]);
  const v = jsonOf(r) as ReviewRequests | undefined;
  const out = new Set<string>();
  if (!v || !Array.isArray(v.reviewRequests)) return out;
  for (const row of v.reviewRequests as { login?: unknown; name?: unknown }[]) {
    if (typeof row.login === "string") out.add(row.login.toLowerCase());
    if (typeof row.name === "string") out.add(row.name.toLowerCase());
  }
  return out;
}

export const requestReviewPhase: PhaseRunner = async (ctx): Promise<PhaseResult> => {
  const pr = ctx.unit.pr;
  if (!pr) {
    ctx.log("request-review: no PR recorded, skipped");
    return pass();
  }
  if (ctx.config.reviewers.length === 0) {
    ctx.log("request-review: no reviewers configured, skipped");
    return pass();
  }
  const have = await currentReviewers(ctx, pr.number);
  for (const login of ctx.config.reviewers) {
    if (have.has(login.toLowerCase())) {
      ctx.log(`request-review: ${login} already requested`);
      continue;
    }
    const r = await gh(ctx, ["pr", "edit", String(pr.number), "--add-reviewer", login]);
    ctx.log(
      ok(r)
        ? `request-review: ${login} attached`
        : `request-review: ${login} unavailable (${errText(r, 120)})`,
    );
  }
  return pass();
};
