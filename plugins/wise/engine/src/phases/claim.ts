// `claim`: idempotent ownership of one unit (process-tickets.md §1 "Ownership & dedup gate").
// A ledger this run wrote earlier means the unit is ours (resume). Otherwise the live state
// decides: a merged PR on the branch = already shipped; a remote branch, local branch or
// registered worktree we did not create = another run owns it, skip and never stomp.

import { existsSync } from "node:fs";
import {
  fail,
  gh,
  git,
  jsonOf,
  localBranchExists,
  ok,
  pass,
  remoteBranchExists,
  resolveBase,
} from "./common.ts";
import type { PhaseCtx, PhaseResult, PhaseRunner } from "./common.ts";

/** Ledger marker: `cursors.claim` once this run owns the unit. */
export const OWNED = "owned";

export function isOwned(ctx: PhaseCtx): boolean {
  return ctx.ledger.cursors.claim === OWNED;
}

type PrRow = { number?: unknown; url?: unknown };

/** A merged PR for the branch, when `gh` can answer; `undefined` otherwise. */
async function mergedPr(
  ctx: PhaseCtx,
  branch: string,
): Promise<{ number: number; url: string } | undefined> {
  const r = await gh(ctx, [
    "pr",
    "list",
    "--head",
    branch,
    "--state",
    "merged",
    "--json",
    "number,url",
    "--limit",
    "1",
  ]);
  const rows = jsonOf(r);
  if (!Array.isArray(rows) || rows.length === 0) return undefined;
  const row = rows[0] as PrRow;
  if (typeof row.number !== "number" || typeof row.url !== "string") return undefined;
  return { number: row.number, url: row.url };
}

async function worktreeRegistered(ctx: PhaseCtx, branch: string): Promise<boolean> {
  const r = await git(ctx, ["worktree", "list", "--porcelain"]);
  return ok(r) && r.stdout.includes(`\nbranch refs/heads/${branch}\n`);
}

export const claimPhase: PhaseRunner = async (ctx): Promise<PhaseResult> => {
  const { unit } = ctx;
  if (ctx.config.pipeline === "plan") {
    if (unit.plan_path === undefined || !existsSync(unit.plan_path)) {
      return fail(`missing: plan file ${unit.plan_path ?? "?"} not found`);
    }
  }
  const base = unit.base || (await resolveBase(ctx));
  const withBase = { ...unit, base };
  if (isOwned(ctx)) {
    ctx.log(`claim: ${unit.ref} owned by this run (resume)`);
    return pass({ unit: withBase });
  }

  const merged = await mergedPr(ctx, unit.branch);
  if (merged) {
    return fail(`pr-merged: #${merged.number}`, "merged", { unit: { ...withBase, pr: merged } });
  }
  const remote = await remoteBranchExists(ctx, unit.branch);
  if (remote === undefined) return fail("claim: origin unreachable (git ls-remote failed)");
  if (remote) return fail(`already-claimed: origin/${unit.branch} exists`, "skipped");
  if (await localBranchExists(ctx, unit.branch)) {
    return fail(`already-claimed: local branch ${unit.branch} exists`, "skipped");
  }
  if (await worktreeRegistered(ctx, unit.branch)) {
    return fail(`already-claimed: a worktree is on ${unit.branch}`, "skipped");
  }
  ctx.log(`claim: ${unit.ref} -> ${unit.branch} (base ${base})`);
  return pass({ unit: withBase, cursors: { ...ctx.ledger.cursors, claim: OWNED } });
};
