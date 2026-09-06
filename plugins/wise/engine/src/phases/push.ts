// `push`: `git push -u origin <branch>` (process-tickets.md §5). Refuses protected branches;
// never forces. Runs from the base repo: worktrees share refs, so the branch is visible there.

import {
  errText,
  fail,
  git,
  isProtectedBranch,
  NETWORK_CMD_TIMEOUT_MS,
  ok,
  pass,
} from "./common.ts";
import type { PhaseResult, PhaseRunner } from "./common.ts";

export const pushPhase: PhaseRunner = async (ctx): Promise<PhaseResult> => {
  const { branch } = ctx.unit;
  if (isProtectedBranch(branch)) return fail(`push: refused, ${branch} is a protected branch`);
  const r = await git(ctx, ["push", "-u", "origin", branch], { timeoutMs: NETWORK_CMD_TIMEOUT_MS });
  if (!ok(r)) return fail(`push: ${errText(r)}`);
  ctx.log(`push: origin/${branch} updated`);
  return pass();
};
