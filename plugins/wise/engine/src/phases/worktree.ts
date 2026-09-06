// `worktree`: bring `<runDir>/worktrees/<slug>` into being on the unit's branch, from a freshly
// fetched base (process-tickets.md §1 "Ensure the worktree"): reuse a registered worktree on our
// branch, re-attach a bare branch, prune a stale entry, else create branch and worktree together.
// `.worktreeinclude` files are copied once per worktree, gated by the ledger.

import { existsSync, mkdirSync, readdirSync, rmdirSync } from "node:fs";
import { dirname } from "node:path";
import { applyWorktreeInclude } from "../ledger.ts";
import { realpathLoose } from "../paths.ts";
import {
  errText,
  fail,
  git,
  localBranchExists,
  NETWORK_CMD_TIMEOUT_MS,
  ok,
  pass,
} from "./common.ts";
import type { PhaseCtx, PhaseResult, PhaseRunner } from "./common.ts";

/** Ledger marker: `cursors.worktree` once the include files were copied. */
export const INCLUDES_DONE = "includes-done";

type Registered = { path: string; branch?: string };

/** Parse `git worktree list --porcelain` into path + branch pairs. */
export function parseWorktrees(porcelain: string): Registered[] {
  const out: Registered[] = [];
  for (const block of porcelain.split(/\n\n+/)) {
    let path: string | undefined;
    let branch: string | undefined;
    for (const line of block.split("\n")) {
      if (line.startsWith("worktree ")) path = line.slice("worktree ".length);
      else if (line.startsWith("branch refs/heads/"))
        branch = line.slice("branch refs/heads/".length);
    }
    if (path !== undefined) out.push(branch !== undefined ? { path, branch } : { path });
  }
  return out;
}

async function registered(ctx: PhaseCtx, path: string): Promise<Registered | undefined> {
  const r = await git(ctx, ["worktree", "list", "--porcelain"]);
  if (!ok(r)) return undefined;
  const want = realpathLoose(path);
  return parseWorktrees(r.stdout).find((w) => realpathLoose(w.path) === want);
}

function dirIsEmpty(path: string): boolean {
  try {
    return readdirSync(path).length === 0;
  } catch {
    return false;
  }
}

export const worktreePhase: PhaseRunner = async (ctx): Promise<PhaseResult> => {
  const { unit } = ctx;
  const path = unit.worktree;
  const base = unit.base || "main";

  const fetched = await git(ctx, ["fetch", "origin", base], { timeoutMs: NETWORK_CMD_TIMEOUT_MS });
  if (!ok(fetched)) {
    const have = await git(ctx, ["rev-parse", "--verify", "--quiet", `origin/${base}`]);
    if (!ok(have)) return fail(`worktree: fetch origin/${base} failed and no local copy`);
    ctx.log(`worktree: fetch failed, using the local origin/${base}`);
  }

  let reg = await registered(ctx, path);
  if (reg === undefined && existsSync(path)) {
    // Directory without an admin entry: stale or corrupt. Prune, then re-attach an empty dir.
    await git(ctx, ["worktree", "prune"]);
    reg = await registered(ctx, path);
    if (reg === undefined) {
      if (!dirIsEmpty(path)) return fail(`worktree-corrupt: ${path} exists but is not a worktree`);
      rmdirSync(path);
    }
  }
  if (reg !== undefined) {
    if (reg.branch !== unit.branch) {
      return fail(`worktree-conflict: ${path} is on ${reg.branch ?? "detached HEAD"}`);
    }
    ctx.log(`worktree: reuse ${path}`);
  } else {
    mkdirSync(dirname(path), { recursive: true });
    const add = (await localBranchExists(ctx, unit.branch))
      ? await git(ctx, ["worktree", "add", path, unit.branch])
      : await git(ctx, [
          "worktree",
          "add",
          "--no-track",
          path,
          "-b",
          unit.branch,
          `origin/${base}`,
        ]);
    if (!ok(add)) return fail(`worktree: git worktree add failed: ${errText(add)}`);
    ctx.log(`worktree: created ${path} on ${unit.branch}`);
  }

  const cursors = { ...ctx.ledger.cursors };
  if (cursors.worktree !== INCLUDES_DONE) {
    const inc = applyWorktreeInclude(ctx.cwd, path);
    for (const n of inc.notices) ctx.log(n);
    if (inc.copied > 0) ctx.log(`worktree: copied ${inc.copied} include path(s)`);
    cursors.worktree = INCLUDES_DONE;
  }
  return pass({ unit: { ...unit, worktree: path, base }, cursors });
};
