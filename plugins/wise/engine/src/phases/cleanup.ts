// `cleanup`: only a merged unit with a PR loses its worktree and local branch (process-tickets.md
// §9); the work is safe on the remote. Every other outcome keeps the worktree for a human, and
// the ledger's `unit.worktree` records where it is. No-op safe when nothing remains.

import { existsSync } from "node:fs";
import { errText, git, ok, pass } from "./common.ts";
import type { PhaseResult, PhaseRunner } from "./common.ts";

export const cleanupPhase: PhaseRunner = async (ctx): Promise<PhaseResult> => {
  const { unit, ledger } = ctx;
  const cleanable = ledger.verdict === "merged" && unit.pr !== undefined;
  if (!cleanable) {
    if (existsSync(unit.worktree)) ctx.log(`cleanup: kept worktree ${unit.worktree}`);
    return pass({ cleaned: false });
  }
  if (existsSync(unit.worktree)) {
    const removed = await git(ctx, ["worktree", "remove", unit.worktree]);
    if (!ok(removed)) {
      const forced = await git(ctx, ["worktree", "remove", "--force", unit.worktree]);
      if (!ok(forced)) {
        ctx.log(`cleanup: could not remove ${unit.worktree}: ${errText(forced)}`);
        return pass({ cleaned: false });
      }
    }
    ctx.log(`cleanup: removed worktree ${unit.worktree}`);
  }
  // -D: the remote squash / merge commit is not in local history, so -d would refuse.
  await git(ctx, ["branch", "-D", unit.branch]);
  await git(ctx, ["worktree", "prune"]);
  return pass({ cleaned: true });
};
