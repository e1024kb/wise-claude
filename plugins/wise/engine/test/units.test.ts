import { after, describe, test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { daemonPaths, findRunDir, listRunDirs } from "../src/daemon.ts";
import type { DaemonRuntime } from "../src/daemon.ts";
import { createExecutor } from "../src/executor.ts";
import type { Executor } from "../src/executor.ts";
import { initState, readEvents, readState, readUnit, utcNow } from "../src/ledger.ts";
import type { EventInput } from "../src/ledger.ts";
import { domainError } from "../src/rpc.ts";
import type { CallContext } from "../src/rpc.ts";
import type { PhaseRunner } from "../src/phases/common.ts";
import type { Phase, State, UnitsStep } from "../src/types.ts";
import { configFor, isDone, NO_AGENT_RUNTIME, runUnitsStep } from "../src/units.ts";
import type { UnitsStepInput } from "../src/units.ts";
import { fakeAdapter, pause, schemaAnswer, conductRun } from "./fixtures/executor/fake.ts";
import { commitFile, fakeExec, git, makeRepoPair, startsWith } from "./fixtures/git.ts";
import type { FakeExec, RepoPair } from "./fixtures/git.ts";
import {
  happyGh,
  implementCommit,
  phaseOf,
  planReady,
  reviewApprove,
  watchGreen,
} from "./fixtures/units.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFS = join(HERE, "..", "..", "workflows");
const EXEC_FIXTURES = join(HERE, "fixtures", "executor");

const roots: string[] = [];
const executors = new Set<Executor>();
function tmp(): string {
  const dir = mkdtempSync(join(tmpdir(), "units-"));
  roots.push(dir);
  return dir;
}

const STEP: UnitsStep = {
  id: "process",
  type: "units",
  pipeline: "ticket",
  items: "PROJ-1,PROJ-2",
  groups: { plan: "build", implement: "build", watch: "build" },
  caps: ["max_fix_attempts", "max_review_cycles"],
};

type UnitEvent = Omit<EventInput, "run_id">;
type Harness = {
  pair: RepoPair;
  runDir: string;
  state: State;
  events: UnitEvent[];
  exec: FakeExec;
  input: (over?: Partial<UnitsStepInput>) => UnitsStepInput;
};

function harness(exec: FakeExec = fakeExec()): Harness {
  const root = tmp();
  const pair = makeRepoPair(root);
  const runDir = join(root, "run");
  mkdirSync(runDir);
  const state = initState({
    runDir,
    runId: "01RUNUNITS",
    workflow: { name: "units-two", version: 2, dir: EXEC_FIXTURES },
    stepIds: ["process"],
    cwd: pair.clone,
    profile: "low",
  });
  state.context = { ticket: [{ ref: "PROJ-1", title: "First", url: "https://t/PROJ-1" }] };
  state.caps = { max_fix_attempts: 3, max_review_cycles: 2 };
  const events: UnitEvent[] = [];
  const input = (over: Partial<UnitsStepInput> = {}): UnitsStepInput => ({
    runDir,
    cwd: pair.clone,
    stepRunId: "01STEPRUN",
    step: STEP,
    items: ["PROJ-1", "PROJ-2"],
    state,
    parentEnv: { PATH: process.env.PATH, HOME: root },
    exec,
    emit: (ev) => {
      events.push(ev);
    },
    ...over,
  });
  return { pair, runDir, state, events, exec, input };
}

function phasesOf(events: UnitEvent[], unit: string): string[] {
  return events.filter((e) => e.type === "unit.phase" && e.unit === unit).map((e) => e.phase ?? "");
}

describe("units", () => {
  after(() => {
    for (const e of executors) e.stop();
    for (const r of roots) rmSync(r, { recursive: true, force: true });
  });

  test("configFor: caps from the run state, default reviewers", () => {
    const h = harness();
    const cfg = configFor(STEP, h.state);
    assert.deepEqual(cfg.caps, { max_fix_attempts: 3, max_review_cycles: 2 });
    assert.deepEqual(cfg.reviewers, ["copilot-pull-request-reviewer"]);
    assert.equal(cfg.tickets[0]?.title, "First");
  });

  test("two spellings of one ticket collapse into a single unit", async () => {
    const h = harness();
    const res = await runUnitsStep(h.input({ items: ["PROJ-1", "https://t/browse/PROJ-1"] }));
    assert.equal(res.outputs.units.length, 1, "the url spelling maps to the same branch");
    assert.equal(res.outputs.units[0]?.unit.ref, "PROJ-1");
    assert.equal(res.verdict, "units=1 merged=0 open=0 failed=0 skipped=1");
  });

  test("two tickets, no agent runtime: claim + worktree real, model phases skipped, cleanup closes the ledger", async () => {
    const h = harness();
    const res = await runUnitsStep(h.input());
    assert.equal(res.verdict, "units=2 merged=0 open=0 failed=0 skipped=2");
    assert.equal(res.outputs.units.length, 2);
    for (const ref of ["PROJ-1", "PROJ-2"]) {
      const row = res.outputs.units.find((r) => r.unit.ref === ref);
      assert.equal(row?.verdict, "skipped");
      assert.equal(row?.reason, NO_AGENT_RUNTIME);
      assert.equal(row?.cleaned, false);
      const ledger = readUnit(h.runDir, ref);
      assert.ok(ledger, `units/${ref}.json written`);
      assert.equal(ledger.last_phase, "cleanup");
      assert.equal(ledger.cursors.claim, "owned");
      assert.deepEqual(ledger.caps, { max_fix_attempts: 3, max_review_cycles: 2 });
      assert.equal(ledger.plan_path, undefined);
      assert.equal(ledger.unit.base, "main");
      assert.equal(ledger.unit.worktree, join(h.runDir, "worktrees", ref));
      assert.ok(existsSync(join(ledger.unit.worktree, "README.md")), "worktree kept for a human");
      assert.ok(isDone(ledger));
      assert.deepEqual(phasesOf(h.events, ref), ["claim", "worktree", "plan", "cleanup"]);
      const done = h.events.find((e) => e.type === "unit.done" && e.unit === ref);
      assert.equal(done?.verdict, "skipped");
      assert.equal(done?.message, NO_AGENT_RUNTIME);
    }
    assert.ok(existsSync(res.log));
    assert.match(readFileSync(res.log, "utf8"), /\[PROJ-1\] \$ git fetch origin main -> ok/);
  });

  test("second run resumes: done units are skipped with their recorded verdict, no phase re-runs", async () => {
    const h = harness();
    await runUnitsStep(h.input());
    h.events.length = 0;
    const ghBefore = h.exec.gh.length;
    const res = await runUnitsStep(h.input());
    assert.equal(res.outputs.units.length, 2);
    assert.equal(res.outputs.units[0]?.verdict, "skipped");
    assert.equal(h.events.filter((e) => e.type === "unit.phase").length, 0);
    assert.deepEqual(
      h.events.map((e) => `${e.type}:${e.unit}:${e.verdict}`),
      ["unit.done:PROJ-1:skipped", "unit.done:PROJ-2:skipped"],
    );
    assert.equal(h.exec.gh.length, ghBefore, "no git or gh work on an already-closed unit");
  });

  test("ledger is persisted after every phase; a throwing runner fails the unit and cleanup still runs", async () => {
    const h = harness();
    const seen: string[] = [];
    const plan: PhaseRunner = (ctx) => {
      const onDisk = readUnit(ctx.runDir, ctx.unit.branch);
      seen.push(
        `${onDisk?.last_phase}/${String(onDisk?.cursors.claim)}/${String(onDisk?.cursors.worktree)}`,
      );
      throw new Error("boom");
    };
    const res = await runUnitsStep(h.input({ items: ["PROJ-1"], runners: { plan } }));
    assert.deepEqual(seen, ["worktree/owned/includes-done"]);
    assert.equal(res.outputs.units[0]?.verdict, "failed");
    assert.equal(res.outputs.units[0]?.reason, "plan: boom");
    assert.deepEqual(phasesOf(h.events, "PROJ-1"), ["claim", "worktree", "plan", "cleanup"]);
    assert.equal(readUnit(h.runDir, "PROJ-1")?.last_phase, "cleanup");
    assert.equal(res.verdict, "units=1 merged=0 open=0 failed=1 skipped=0");
  });

  test("full pipeline with injected model runners: push, PR create, reviewer attach, merge, cleanup", async () => {
    const gh = happyGh();
    const exec = fakeExec(gh.rule);
    const h = harness(exec);
    const okRunner: PhaseRunner = () => Promise.resolve({ ok: true });
    const implement: PhaseRunner = (ctx) => {
      commitFile(ctx.unit.worktree, "feature.txt", "x\n", "feat: implement PROJ-1");
      return Promise.resolve({ ok: true });
    };
    const review: PhaseRunner = () =>
      Promise.resolve({ ok: true, output: { findings: 0, blocking: 0, verdict: "approve" } });
    const watch: PhaseRunner = () =>
      Promise.resolve({
        ok: true,
        output: {
          ci: "green",
          bot_reviews: "resolved",
          human_comment: false,
          merged: false,
          verdict: "ready",
        },
      });
    const runners: Partial<Record<Phase, PhaseRunner>> = {
      plan: okRunner,
      implement,
      review,
      fix: okRunner,
      watch,
    };
    const res = await runUnitsStep(
      h.input({ items: ["PROJ-1"], runners, sleep: () => Promise.resolve() }),
    );
    assert.equal(res.verdict, "units=1 merged=1 open=0 failed=0 skipped=0");
    const row = res.outputs.units[0];
    assert.equal(row?.verdict, "merged");
    assert.equal(row?.cleaned, true);
    assert.deepEqual(row?.unit.pr, { number: 5, url: "https://github.com/a/r/pull/5" });
    assert.equal(gh.state.pr, "MERGED");
    assert.ok(exec.gh.some((a) => startsWith(a, "pr", "merge", "5", "--squash")));
    assert.deepEqual(phasesOf(h.events, "PROJ-1"), [
      "claim",
      "worktree",
      "plan",
      "implement",
      "review",
      "push",
      "pr",
      "request-review",
      "watch",
      "cleanup",
    ]);
    const ledger = readUnit(h.runDir, "PROJ-1");
    assert.deepEqual(ledger?.review, { converged: true, cycles: 1 });
    assert.deepEqual(ledger?.watch, { passes: 2, fix_attempts: 0, stable: 2 });
    assert.match(git(h.pair.clone, ["ls-remote", "--heads", "origin", "PROJ-1"]), /PROJ-1/);
    const create = exec.gh.find((a) => startsWith(a, "pr", "create"));
    assert.equal(create?.[create.indexOf("--title") + 1], "PROJ-1: First");
    assert.ok(
      exec.gh.some((a) =>
        startsWith(a, "pr", "edit", "5", "--add-reviewer", "copilot-pull-request-reviewer"),
      ),
    );
    assert.equal(
      existsSync(join(h.runDir, "worktrees", "PROJ-1")),
      false,
      "merged unit's worktree removed",
    );
    assert.throws(() =>
      git(h.pair.clone, ["show-ref", "--verify", "--quiet", "refs/heads/PROJ-1"]),
    );
  });

  test("parallel: two units at once, rows in item order; abort stops before the next unit", async () => {
    const h = harness();
    // Barrier: the first unit to reach `plan` waits for the second (bounded), so the assertion
    // holds only when both units really are in flight at once.
    const arrived = { count: 0, max: 0 };
    const plan: PhaseRunner = async () => {
      arrived.count++;
      const deadline = Date.now() + 3000;
      while (arrived.count < 2 && Date.now() < deadline) await pause(10);
      arrived.max = Math.max(arrived.max, arrived.count);
      return { ok: false, verdict: "skipped", reason: "stub" };
    };
    const res = await runUnitsStep(
      h.input({ step: { ...STEP, parallel: 2 }, items: ["PROJ-1", "PROJ-2"], runners: { plan } }),
    );
    assert.equal(arrived.max, 2);
    assert.deepEqual(
      res.outputs.units.map((r) => r.unit.ref),
      ["PROJ-1", "PROJ-2"],
    );
    const ac = new AbortController();
    const h2 = harness();
    const abortingPlan: PhaseRunner = () => {
      ac.abort();
      return Promise.resolve({ ok: false, verdict: "skipped", reason: "stub" });
    };
    const partial = await runUnitsStep(
      h2.input({ items: ["PROJ-1", "PROJ-2"], runners: { plan: abortingPlan }, signal: ac.signal }),
    );
    assert.equal(partial.outputs.units.length, 1, "second unit never started after abort");
  });

  test("plan pipeline: branch from the plan slug, missing file fails just that unit", async () => {
    const h = harness();
    mkdirSync(join(h.pair.clone, "docs", "plans"), { recursive: true });
    writeFileSync(join(h.pair.clone, "docs", "plans", "PLAN-Q-7.md"), "# Q-7 plan\n");
    const step: UnitsStep = { ...STEP, pipeline: "plan" };
    const res = await runUnitsStep(
      h.input({ step, items: ["docs/plans/PLAN-Q-7.md", "docs/plans/PLAN-GONE.md"] }),
    );
    assert.equal(res.outputs.units[0]?.unit.branch, "Q-7");
    assert.equal(res.outputs.units[0]?.verdict, "skipped");
    assert.ok(existsSync(join(h.runDir, "worktrees", "Q-7")));
    assert.equal(res.outputs.units[1]?.verdict, "failed");
    assert.match(res.outputs.units[1]?.reason ?? "", /^missing: plan file/);
  });

  // ---- executor end to end -------------------------------------------------------------------------------

  test(
    "executor: a v2 units step with two refs runs every phase on the fake harness, merges, reports",
    { timeout: 30_000 },
    async () => {
      const root = tmp();
      const pair = makeRepoPair(root);
      const env = { XDG_DATA_HOME: root, HOME: root, PATH: process.env.PATH ?? "" };
      const paths = daemonPaths({ env });
      writeFileSync(join(root, "engine.json"), "{}");
      const rt: DaemonRuntime = {
        paths,
        version: "9.9.9-test",
        pid: process.pid,
        started_at: utcNow(),
        log: () => {},
        listRunDirs: () => listRunDirs(paths.runsRoot),
        findRunDir: (id) => findRunDir(paths.runsRoot, id),
        requireRunDir: (id) => {
          const dir = findRunDir(paths.runsRoot, id);
          if (!dir) throw domainError("RUN_NOT_FOUND", `no such run: ${id}`, { run_id: id });
          return dir;
        },
      };
      const ctx: CallContext = {
        notify: () => {},
        signal: new AbortController().signal,
        connectionId: 0,
      };
      const exec = createExecutor(rt, {
        env,
        roots: { userRoot: EXEC_FIXTURES, bundledRoot: DEFS },
        configPath: join(root, "engine.json"),
        adapters: {
          claude: fakeAdapter("claude", (req) => {
            switch (phaseOf(req)) {
              case "plan":
                return planReady(req, 1);
              case "implement":
                return implementCommit(req, 1);
              case "review":
                return reviewApprove(req, 1);
              case "watch":
                return watchGreen(req, 1);
              default:
                return schemaAnswer(req);
            }
          }),
        },
        unitsExec: fakeExec(happyGh().rule),
      });
      executors.add(exec);
      const { run_id, status } = await conductRun(
        exec,
        {
          workflow: "units-two",
          cwd: pair.clone,
          answers: { "input.tickets": "PROJ-1, PROJ-2" },
          context: { ticket: [{ ref: "PROJ-1", title: "First" }] },
          inputs: {},
        },
        ctx,
      );
      assert.equal(status, "running");
      const runDir = rt.requireRunDir(run_id);
      const deadline = Date.now() + 20_000;
      while (Date.now() < deadline && !["completed", "failed"].includes(readState(runDir).status)) {
        await pause(20);
      }
      const state = readState(runDir);
      assert.equal(state.status, "completed", state.error);
      const step = state.steps.process;
      assert.equal(step?.status, "completed");
      assert.equal(step?.verdict, "units=2 merged=2 open=0 failed=0 skipped=0");
      const rows = state.outputs.units as {
        unit: { ref: string };
        verdict?: string;
        reason?: string;
      }[];
      assert.deepEqual(
        rows.map((r) => [r.unit.ref, r.verdict, r.reason]),
        [
          ["PROJ-1", "merged", undefined],
          ["PROJ-2", "merged", undefined],
        ],
      );
      // Model-phase usage lands in the run state and per harness (E14).
      assert.ok(state.usage.subscription.input > 0, "usage folded into run state");
      assert.ok((state.usage.by_harness.claude?.input ?? 0) > 0);
      // The pre-resolved phases carry over into the phase events.
      assert.equal(state.resolved["process.plan"]?.harness, "claude");
      const report = await exec.handlers.report({ run_id }, ctx);
      assert.equal(report.units.length, 2);
      assert.deepEqual(report.units.map((u) => u.unit.branch).toSorted(), ["PROJ-1", "PROJ-2"]);
      assert.equal(report.verdicts.process, step?.verdict);
      const types = readEvents(runDir).map(
        (e) => `${e.type}${e.unit ? `:${e.unit}` : ""}${e.phase ? `:${e.phase}` : ""}`,
      );
      assert.ok(types.includes("unit.phase:PROJ-1:claim"));
      assert.ok(types.includes("unit.phase:PROJ-2:worktree"));
      assert.ok(types.includes("unit.phase:PROJ-1:implement"));
      const planEv = readEvents(runDir).find(
        (e) => e.type === "unit.phase" && e.unit === "PROJ-1" && e.phase === "plan",
      );
      assert.equal(planEv?.harness, "claude");
      assert.ok(planEv?.model, "phase event names the model");
      assert.ok(types.includes("unit.done:PROJ-1") && types.includes("unit.done:PROJ-2"));
      assert.equal(types.at(-1), "run.done");
      assert.equal(readUnit(runDir, "PROJ-1")?.caps?.max_fix_attempts, 3, "declared caps stored");
      assert.equal(existsSync(join(runDir, "worktrees", "PROJ-2")), false, "merged: cleaned");
    },
  );
});
