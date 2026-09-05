import { after, describe, test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { cleanEnv } from "../src/adapters/spawn.ts";
import { EMPTY_USAGE } from "../src/types.ts";
import type { Unit, UnitLedger } from "../src/types.ts";
import { claimPhase, OWNED } from "../src/phases/claim.ts";
import { cleanupPhase } from "../src/phases/cleanup.ts";
import {
  isProtectedBranch,
  makeUnit,
  parseItems,
  planBranch,
  ticketBranch,
  ticketRef,
} from "../src/phases/common.ts";
import type { PhaseCtx, UnitsConfig } from "../src/phases/common.ts";
import { defaultPrBody, fillPrTemplate, findPrTemplate, prPhase } from "../src/phases/pr.ts";
import { pushPhase } from "../src/phases/push.ts";
import { requestReviewPhase } from "../src/phases/request-review.ts";
import { INCLUDES_DONE, parseWorktrees, worktreePhase } from "../src/phases/worktree.ts";
import { commitFile, fakeExec, git, makeRepoPair, result, startsWith } from "./fixtures/git.ts";
import type { FakeExec, RepoPair } from "./fixtures/git.ts";

const roots: string[] = [];
function tmp(): string {
  const dir = mkdtempSync(join(tmpdir(), "phases-"));
  roots.push(dir);
  return dir;
}

type Fixture = { pair: RepoPair; runDir: string; exec: FakeExec; logs: string[] };

function fixture(exec: FakeExec = fakeExec()): Fixture {
  const root = tmp();
  const pair = makeRepoPair(root);
  const runDir = join(root, "run");
  mkdirSync(runDir);
  return { pair, runDir, exec, logs: [] };
}

function ledgerFor(unit: Unit, extra: Partial<UnitLedger> = {}): UnitLedger {
  return { unit, last_phase: "claim", cleaned: false, cursors: {}, usage: EMPTY_USAGE(), ...extra };
}

function ctxFor(
  f: Fixture,
  unit: Unit,
  opts: { ledger?: UnitLedger; config?: Partial<UnitsConfig> } = {},
): PhaseCtx {
  const ledger = opts.ledger ?? ledgerFor(unit);
  const config: UnitsConfig = {
    pipeline: "ticket",
    reviewers: ["copilot-pull-request-reviewer"],
    tickets: [],
    caps: {},
    groups: {},
    ...opts.config,
  };
  return {
    unit: ledger.unit,
    ledger,
    cwd: f.pair.clone,
    runDir: f.runDir,
    env: cleanEnv({
      parent: { PATH: process.env.PATH, HOME: f.pair.root },
      extra: { GIT_TERMINAL_PROMPT: "0" },
    }),
    exec: f.exec,
    config,
    log: (line) => {
      f.logs.push(line);
    },
  };
}

/** Claim, then create the worktree; returns the ledger the two phases produced. */
async function claimed(f: Fixture, ref: string): Promise<{ ctx: PhaseCtx; ledger: UnitLedger }> {
  const unit = makeUnit("ticket", ref, f.pair.clone, f.runDir);
  const ledger = ledgerFor(unit);
  let ctx = ctxFor(f, unit, { ledger });
  const c = await claimPhase(ctx);
  assert.ok(c.ok, `claim: ${JSON.stringify(c)}`);
  Object.assign(ledger, { ...c.patch, cursors: { ...ledger.cursors, ...c.patch?.cursors } });
  ctx = { ...ctx, unit: ledger.unit, ledger };
  const w = await worktreePhase(ctx);
  assert.ok(w.ok, `worktree: ${JSON.stringify(w)}`);
  Object.assign(ledger, { ...w.patch, cursors: { ...ledger.cursors, ...w.patch?.cursors } });
  return { ctx: { ...ctx, unit: ledger.unit, ledger }, ledger };
}

describe("phases", () => {
  after(() => {
    for (const r of roots) rmSync(r, { recursive: true, force: true });
  });

  // ---- naming ---------------------------------------------------------------------------------------

  test("naming: ticket refs, branches, plan slugs, protected branches", () => {
    assert.equal(ticketRef(" #678 "), "678");
    assert.equal(ticketRef("https://linear.app/acme/issue/ENG-9/some-title"), "ENG-9");
    assert.equal(ticketRef("https://acme.atlassian.net/browse/PROJ-777"), "PROJ-777");
    assert.equal(ticketBranch("PROJ-777"), "PROJ-777");
    assert.equal(ticketBranch("678"), "abstract-task-678");
    assert.equal(ticketBranch("weird ref/x"), "weird-ref-x");
    assert.equal(planBranch("docs/plans/001-api-caching.md"), "001-api-caching");
    assert.equal(planBranch("/abs/PLAN-PROJ-1.md"), "PROJ-1");
    assert.equal(planBranch("42.md"), "plan-42");
    assert.ok(isProtectedBranch("main") && isProtectedBranch("master"));
    assert.ok(isProtectedBranch("release/1.2") && !isProtectedBranch("PROJ-1"));
    const u = makeUnit("ticket", "#12", "/repo", "/run");
    assert.deepEqual(u, {
      ref: "12",
      branch: "abstract-task-12",
      worktree: "/run/worktrees/abstract-task-12",
      base: "",
    });
    const p = makeUnit("plan", "docs/plans/PLAN-X-1.md", "/repo", "/run");
    assert.equal(p.plan_path, "/repo/docs/plans/PLAN-X-1.md");
    assert.equal(p.branch, "X-1");
  });

  test("parseItems: comma, semicolon, newline, JSON array, dedupe", () => {
    assert.deepEqual(parseItems("PROJ-1, PROJ-2;PROJ-1\nPROJ-3\n"), ["PROJ-1", "PROJ-2", "PROJ-3"]);
    assert.deepEqual(parseItems('["A-1", {"ref": "B-2"}, 3]'), ["A-1", "B-2"]);
    assert.deepEqual(parseItems("  "), []);
  });

  test("parseWorktrees: porcelain blocks to path + branch", () => {
    const text =
      "worktree /repo\nHEAD abc\nbranch refs/heads/main\n\nworktree /wt/x\nHEAD def\ndetached\n\n";
    assert.deepEqual(parseWorktrees(text), [{ path: "/repo", branch: "main" }, { path: "/wt/x" }]);
  });

  // ---- PR body -----------------------------------------------------------------------------------------

  test("pr body: compact default and template fill keep unknown sections", () => {
    const facts = {
      ref: "PROJ-1",
      title: "PROJ-1: add panel",
      ticketLink: "[PROJ-1](https://x/PROJ-1)",
      commits: ["feat: add panel", "test: cover panel"],
    };
    const dflt = defaultPrBody(facts);
    assert.match(dflt, /## Summary\n- PROJ-1: add panel/);
    assert.match(dflt, /## Changes\n- feat: add panel\n- test: cover panel/);
    assert.match(dflt, /## Context\n- ticket: \[PROJ-1\]\(https:\/\/x\/PROJ-1\)/);
    assert.match(dflt, /## Testing\n- \[ \] Unit tests pass/);
    const template =
      "## Summary\n<1-3 bullets>\n\n## Changes\n<list>\n\n## Testing\n- [ ] Unit tests pass\n\n## Risk & rollout\n<flags>\n";
    const filled = fillPrTemplate(template, facts);
    assert.match(filled, /## Summary\n- PROJ-1: add panel\n/);
    assert.doesNotMatch(filled, /<1-3 bullets>/);
    assert.match(filled, /## Testing\n- \[ \] Unit tests pass/);
    assert.match(filled, /## Risk & rollout\n<flags>/);
    // No known headings: the default body leads, the template follows verbatim.
    assert.match(
      fillPrTemplate("## Notes\nfree text\n", facts),
      /## Summary[\s\S]*## Notes\nfree text/,
    );
  });

  // ---- claim -----------------------------------------------------------------------------------------

  test("claim: fresh unit is owned, base resolved from origin/HEAD when gh is unavailable", async () => {
    const f = fixture();
    const unit = makeUnit("ticket", "PROJ-1", f.pair.clone, f.runDir);
    const res = await claimPhase(ctxFor(f, unit));
    assert.ok(res.ok);
    assert.equal(res.patch?.cursors?.claim, OWNED);
    assert.equal(res.patch?.unit?.base, "main");
    assert.ok(
      f.exec.gh.some((a) => startsWith(a, "repo", "view")),
      "gh repo view was tried first",
    );
    assert.ok(f.exec.gh.some((a) => startsWith(a, "pr", "list", "--head", "PROJ-1")));
  });

  test("claim: remote branch, local branch, or a worktree on the branch means already-claimed", async () => {
    const f = fixture();
    // Remote: another clone pushed PROJ-2.
    const other = join(f.pair.root, "other");
    git(f.pair.root, ["clone", "-q", f.pair.origin, other]);
    git(other, ["checkout", "-q", "-b", "PROJ-2"]);
    git(other, ["push", "-q", "-u", "origin", "PROJ-2"]);
    const remote = await claimPhase(
      ctxFor(f, makeUnit("ticket", "PROJ-2", f.pair.clone, f.runDir)),
    );
    assert.equal(remote.ok, false);
    assert.equal(!remote.ok && remote.verdict, "skipped");
    assert.match((!remote.ok && remote.reason) || "", /^already-claimed: origin\/PROJ-2/);
    // Local branch only.
    git(f.pair.clone, ["branch", "PROJ-3"]);
    const local = await claimPhase(ctxFor(f, makeUnit("ticket", "PROJ-3", f.pair.clone, f.runDir)));
    assert.match((!local.ok && local.reason) || "", /^already-claimed: local branch PROJ-3/);
    // Fresh one still claims.
    const fresh = await claimPhase(ctxFor(f, makeUnit("ticket", "PROJ-4", f.pair.clone, f.runDir)));
    assert.ok(fresh.ok);
  });

  test("claim: a merged PR on the branch ends the unit as merged with the PR recorded", async () => {
    const f = fixture(
      fakeExec((a) =>
        startsWith(a, "pr", "list", "--head", "PROJ-9", "--state", "merged")
          ? result('[{"number":42,"url":"https://github.com/acme/r/pull/42"}]')
          : undefined,
      ),
    );
    const res = await claimPhase(ctxFor(f, makeUnit("ticket", "PROJ-9", f.pair.clone, f.runDir)));
    assert.equal(res.ok, false);
    assert.equal(!res.ok && res.verdict, "merged");
    assert.deepEqual(res.patch?.unit?.pr, { number: 42, url: "https://github.com/acme/r/pull/42" });
  });

  test("claim: owned ledger resumes without probing; plan pipeline needs the plan file", async () => {
    const f = fixture();
    const unit = { ...makeUnit("ticket", "PROJ-1", f.pair.clone, f.runDir), base: "main" };
    const res = await claimPhase(
      ctxFor(f, unit, { ledger: ledgerFor(unit, { cursors: { claim: OWNED } }) }),
    );
    assert.ok(res.ok);
    assert.equal(f.exec.gh.length, 0, "no gh probe on resume");
    const plan = makeUnit("plan", "docs/plans/PLAN-Z-1.md", f.pair.clone, f.runDir);
    const missing = await claimPhase(ctxFor(f, plan, { config: { pipeline: "plan" } }));
    assert.match((!missing.ok && missing.reason) || "", /^missing: plan file/);
    mkdirSync(join(f.pair.clone, "docs", "plans"), { recursive: true });
    writeFileSync(plan.plan_path ?? "", "# Z-1 plan\n");
    const present = await claimPhase(ctxFor(f, plan, { config: { pipeline: "plan" } }));
    assert.ok(present.ok);
  });

  // ---- worktree ------------------------------------------------------------------------------------------

  test("worktree: creates under <runDir>/worktrees from a fresh base, reuses, copies includes once", async () => {
    const f = fixture();
    // Ignored local file the worktree needs, listed in .worktreeinclude.
    commitFile(f.pair.clone, ".gitignore", ".env\n", "ignore env");
    writeFileSync(join(f.pair.clone, ".env"), "SECRET=1\n");
    writeFileSync(join(f.pair.clone, ".worktreeinclude"), ".env\n");
    const { ctx, ledger } = await claimed(f, "PROJ-1");
    const wt = join(f.runDir, "worktrees", "PROJ-1");
    assert.equal(ledger.unit.worktree, wt);
    assert.ok(existsSync(join(wt, "README.md")));
    assert.equal(git(wt, ["rev-parse", "--abbrev-ref", "HEAD"]), "PROJ-1");
    assert.equal(readFileSync(join(wt, ".env"), "utf8"), "SECRET=1\n");
    assert.equal(ledger.cursors.worktree, INCLUDES_DONE);
    // Reuse: same path, no re-copy of includes (the cursor gates it).
    rmSync(join(wt, ".env"));
    const again = await worktreePhase(ctx);
    assert.ok(again.ok);
    assert.equal(again.patch?.unit?.worktree, wt);
    assert.equal(existsSync(join(wt, ".env")), false);
    assert.ok(f.logs.some((l) => l.includes("worktree: reuse")));
  });

  test("worktree: re-attaches an existing branch and refuses a path on another branch", async () => {
    const f = fixture();
    const { ctx, ledger } = await claimed(f, "PROJ-1");
    // Drop the worktree but keep the branch: the next run re-attaches without -b.
    git(f.pair.clone, ["worktree", "remove", "--force", ledger.unit.worktree]);
    const back = await worktreePhase(ctx);
    assert.ok(back.ok);
    assert.equal(git(ledger.unit.worktree, ["rev-parse", "--abbrev-ref", "HEAD"]), "PROJ-1");
    // A worktree at our path but on another branch is a conflict, never adopted.
    const other = makeUnit("ticket", "PROJ-2", f.pair.clone, f.runDir);
    git(f.pair.clone, ["worktree", "add", "-q", other.worktree, "-b", "elsewhere", "origin/main"]);
    const res = await worktreePhase(ctxFor(f, { ...other, base: "main" }));
    assert.match((!res.ok && res.reason) || "", /^worktree-conflict: .* is on elsewhere/);
  });

  // ---- push ------------------------------------------------------------------------------------------

  test("push: refuses main, pushes the unit branch with upstream", async () => {
    const f = fixture();
    const main = { ...makeUnit("ticket", "PROJ-1", f.pair.clone, f.runDir), branch: "main" };
    const refused = await pushPhase(ctxFor(f, main));
    assert.match(
      (!refused.ok && refused.reason) || "",
      /^push: refused, main is a protected branch/,
    );
    const { ctx, ledger } = await claimed(f, "PROJ-1");
    commitFile(ledger.unit.worktree, "feature.txt", "x\n", "feat: add feature");
    const pushed = await pushPhase(ctx);
    assert.ok(pushed.ok, JSON.stringify(pushed));
    assert.match(
      git(f.pair.clone, ["ls-remote", "--heads", "origin", "PROJ-1"]),
      /refs\/heads\/PROJ-1/,
    );
    assert.equal(git(ledger.unit.worktree, ["rev-parse", "--abbrev-ref", "@{u}"]), "origin/PROJ-1");
  });

  // ---- pr ------------------------------------------------------------------------------------------

  test("pr: creates with title from the ticket and a body carrying link, commits, testing", async () => {
    const f = fixture(
      fakeExec((a) => {
        if (startsWith(a, "pr", "view")) return result("no pull requests found", 1);
        if (startsWith(a, "pr", "create")) return result("https://github.com/acme/r/pull/7\n");
        return undefined;
      }),
    );
    const { ctx, ledger } = await claimed(f, "PROJ-1");
    commitFile(ledger.unit.worktree, "feature.txt", "x\n", "feat: add feature");
    ctx.config.tickets = [{ ref: "PROJ-1", title: "Add the panel", url: "https://t/PROJ-1" }];
    const res = await prPhase(ctx);
    assert.ok(res.ok, JSON.stringify(res));
    assert.deepEqual(res.patch?.unit?.pr, { number: 7, url: "https://github.com/acme/r/pull/7" });
    const create = f.exec.gh.find((a) => startsWith(a, "pr", "create"));
    assert.ok(create);
    assert.equal(create[create.indexOf("--base") + 1], "main");
    assert.equal(create[create.indexOf("--head") + 1], "PROJ-1");
    assert.equal(create[create.indexOf("--title") + 1], "PROJ-1: Add the panel");
    const body = readFileSync(create[create.indexOf("--body-file") + 1] ?? "", "utf8");
    assert.match(body, /\[PROJ-1\]\(https:\/\/t\/PROJ-1\)/);
    assert.match(body, /- feat: add feature/);
    assert.match(body, /## Testing/);
    assert.ok(f.logs.some((l) => l.includes("body from the compact default")));
  });

  test("pr: refreshes an open PR, ends on merged / closed, uses the repo template", async () => {
    let state = "OPEN";
    const f = fixture(
      fakeExec((a) => {
        if (startsWith(a, "pr", "view"))
          return result(
            JSON.stringify({
              number: 7,
              url: "https://github.com/acme/r/pull/7",
              state,
              baseRefName: "main",
            }),
          );
        if (startsWith(a, "pr", "edit")) return result("");
        return undefined;
      }),
    );
    const { ctx } = await claimed(f, "PROJ-1");
    mkdirSync(join(f.pair.clone, ".github"));
    writeFileSync(
      join(f.pair.clone, ".github", "pull_request_template.md"),
      "## Summary\n<bullets>\n\n## Risk & rollout\n<flags>\n",
    );
    assert.equal(
      findPrTemplate(f.pair.clone),
      join(f.pair.clone, ".github", "pull_request_template.md"),
    );
    const open = await prPhase(ctx);
    assert.ok(open.ok);
    assert.equal(open.patch?.unit?.pr?.number, 7);
    const edit = f.exec.gh.find((a) => startsWith(a, "pr", "edit", "7", "--body-file"));
    assert.ok(edit, "existing PR gets its body refreshed, never recreated");
    assert.equal(
      f.exec.gh.some((a) => startsWith(a, "pr", "create")),
      false,
    );
    const body = readFileSync(edit[edit.indexOf("--body-file") + 1] ?? "", "utf8");
    assert.match(body, /## Summary\n- PROJ-1\n/);
    assert.match(body, /## Risk & rollout\n<flags>/);
    state = "MERGED";
    const merged = await prPhase(ctx);
    assert.equal(!merged.ok && merged.verdict, "merged");
    state = "CLOSED";
    const closed = await prPhase(ctx);
    assert.equal(!closed.ok && closed.verdict, "human-intervention");
    assert.match((!closed.ok && closed.reason) || "", /^pr-closed/);
  });

  // ---- request-review ------------------------------------------------------------------------------------

  test("request-review: attaches missing reviewers, skips present ones, never fails", async () => {
    const f = fixture(
      fakeExec((a) => {
        if (startsWith(a, "pr", "view", "7"))
          return result(JSON.stringify({ reviewRequests: [{ login: "alice" }] }));
        if (startsWith(a, "pr", "edit", "7", "--add-reviewer", "bob"))
          return result("", 1, "not found");
        if (startsWith(a, "pr", "edit")) return result("");
        return undefined;
      }),
    );
    const unit = {
      ...makeUnit("ticket", "PROJ-1", f.pair.clone, f.runDir),
      pr: { number: 7, url: "u" },
    };
    const res = await requestReviewPhase(
      ctxFor(f, unit, { config: { reviewers: ["alice", "copilot-pull-request-reviewer", "bob"] } }),
    );
    assert.ok(res.ok);
    const adds = f.exec.gh.filter((a) => startsWith(a, "pr", "edit")).map((a) => a.at(-1));
    assert.deepEqual(adds, ["copilot-pull-request-reviewer", "bob"]);
    assert.ok(f.logs.some((l) => l.includes("alice already requested")));
    assert.ok(f.logs.some((l) => l.includes("bob unavailable")));
    // No PR recorded: nothing to do, still ok.
    const none = await requestReviewPhase(
      ctxFor(f, makeUnit("ticket", "PROJ-2", f.pair.clone, f.runDir)),
    );
    assert.ok(none.ok);
  });

  // ---- cleanup ------------------------------------------------------------------------------------------

  test("cleanup: keeps the worktree unless merged with a PR, then removes worktree and branch", async () => {
    const f = fixture();
    const { ctx, ledger } = await claimed(f, "PROJ-1");
    ledger.verdict = "all-green";
    const kept = await cleanupPhase(ctx);
    assert.ok(kept.ok);
    assert.equal(kept.patch?.cleaned, false);
    assert.ok(existsSync(ledger.unit.worktree));
    assert.ok(f.logs.some((l) => l.includes(`kept worktree ${ledger.unit.worktree}`)));
    ledger.verdict = "merged";
    const noPr = await cleanupPhase(ctx);
    assert.equal(noPr.patch?.cleaned, false, "merged without a PR recorded is not cleaned");
    ledger.unit.pr = { number: 7, url: "u" };
    writeFileSync(join(ledger.unit.worktree, "leftover.txt"), "untracked\n");
    const cleaned = await cleanupPhase({ ...ctx, unit: ledger.unit });
    assert.equal(cleaned.patch?.cleaned, true);
    assert.equal(existsSync(ledger.unit.worktree), false);
    assert.throws(() =>
      git(f.pair.clone, ["show-ref", "--verify", "--quiet", "refs/heads/PROJ-1"]),
    );
  });
});
