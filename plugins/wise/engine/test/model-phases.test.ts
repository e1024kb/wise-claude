import { after, describe, test } from "node:test";
import assert from "node:assert/strict";
import { PLUGIN_ROOT } from "../src/version.ts";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  rmSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { daemonPaths, findRunDir, listRunDirs } from "../src/daemon.ts";
import type { DaemonRuntime } from "../src/daemon.ts";
import { createExecutor } from "../src/executor.ts";
import type { Executor } from "../src/executor.ts";
import { initState, readEvents, readState, readUnit, utcNow } from "../src/ledger.ts";
import type { EventInput } from "../src/ledger.ts";
import {
  BOT_LOGINS,
  PHASE_MODE,
  PHASE_TOOLS,
  renderPhasePrompt,
  resolveUnitPhases,
} from "../src/phases/model.ts";
import { MODEL_PHASES } from "../src/prompts/units/schemas.ts";
import type { ModelPhase } from "../src/prompts/units/schemas.ts";
import { unresolvedPlaceholders } from "../src/render.ts";
import { domainError } from "../src/rpc.ts";
import type { CallContext } from "../src/rpc.ts";
import type { Harness, Phase, State, UnitsStep, Usage } from "../src/types.ts";
import { CAP_DEFAULTS, runUnitsStep } from "../src/units.ts";
import type { UnitsStepInput } from "../src/units.ts";
import { fakeAdapter, pause, conductRun } from "./fixtures/executor/fake.ts";
import { heldStarter } from "./fixtures/executor/held.ts";
import { fakeExec, git, makeRepoPair } from "./fixtures/git.ts";
import type { FakeExec, RepoPair } from "./fixtures/git.ts";
import {
  answer,
  happyGh,
  implementCommit,
  planFileFor,
  planReady,
  reviewApprove,
  scriptedStarter,
  watchGreen,
} from "./fixtures/units.ts";
import type { PhaseScript } from "./fixtures/units.ts";
import { commitFile } from "./fixtures/git.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const PROMPTS = join(HERE, "..", "src", "prompts", "units");
const EXEC_FIXTURES = join(HERE, "fixtures", "executor");

const roots: string[] = [];
const executors = new Set<Executor>();
function tmp(): string {
  const dir = mkdtempSync(join(tmpdir(), "model-phases-"));
  roots.push(dir);
  return dir;
}

const STEP: UnitsStep = {
  id: "process",
  type: "units",
  pipeline: "ticket",
  items: "PROJ-1",
  groups: { plan: "build", implement: "build", review: "build", watch: "build" },
  caps: ["max_fix_attempts", "max_review_cycles", "watch_minutes"],
};

type UnitEvent = Omit<EventInput, "run_id">;
type Fixture = {
  pair: RepoPair;
  runDir: string;
  state: State;
  events: UnitEvent[];
  exec: FakeExec;
  usage: { phase: Phase; harness: Harness; usage: Usage }[];
  sleeps: number[];
  input: (over?: Partial<UnitsStepInput>) => UnitsStepInput;
};

function fixture(exec: FakeExec = fakeExec(happyGh().rule)): Fixture {
  const root = tmp();
  const pair = makeRepoPair(root);
  const runDir = join(root, "run");
  mkdirSync(runDir);
  const state = initState({
    runDir,
    runId: "01RUNMODEL",
    workflow: { name: "units-two", version: 2, dir: EXEC_FIXTURES },
    stepIds: ["process"],
    cwd: pair.clone,
    profile: "medium",
  });
  state.context = {
    ticket: [{ ref: "PROJ-1", title: "First", url: "https://t/PROJ-1", body: "Do the thing." }],
    guidance: "keep it small",
  };
  state.caps = { max_fix_attempts: 3, max_review_cycles: 2, watch_minutes: 45 };
  const events: UnitEvent[] = [];
  const usage: Fixture["usage"] = [];
  const sleeps: number[] = [];
  const input = (over: Partial<UnitsStepInput> = {}): UnitsStepInput => ({
    runDir,
    cwd: pair.clone,
    stepRunId: "01STEPRUN",
    step: STEP,
    items: ["PROJ-1"],
    state,
    parentEnv: { PATH: process.env.PATH, HOME: root },
    exec,
    onUsage: (phase, harness, u) => {
      usage.push({ phase, harness, usage: u });
    },
    sleep: (ms) => {
      sleeps.push(ms);
      return Promise.resolve();
    },
    emit: (ev) => {
      events.push(ev);
    },
    ...over,
  });
  return { pair, runDir, state, events, exec, usage, sleeps, input };
}

function phasesOf(events: UnitEvent[], unit = "PROJ-1"): string[] {
  return events.filter((e) => e.type === "unit.phase" && e.unit === unit).map((e) => e.phase ?? "");
}

function withAgent(
  f: Fixture,
  starter: ReturnType<typeof scriptedStarter>["starter"],
  over: Partial<UnitsStepInput> = {},
): UnitsStepInput {
  return f.input({ agent: { starter, stepToken: "tok-1" }, ...over });
}

const findingsOf = (req: { prompt: string }): string => {
  const m = /(?:Write|findings (?:to|in)) (\S+\.findings\.md)/.exec(req.prompt);
  if (!m?.[1]) throw new Error(`no findings path in prompt: ${req.prompt.slice(0, 80)}`);
  return m[1];
};

/** Review that requests changes on the first call (writing one finding) and approves after. */
const reviewOnce: PhaseScript = (req, nth) => {
  if (nth === 1) {
    writeFileSync(findingsOf(req), "1. src/a.ts:1 - off by one - use <= - warning\n", "utf8");
    return answer(
      { findings: 1, blocking: 1, verdict: "changes-requested" },
      { cursor: "sess-review-1" },
    );
  }
  return reviewApprove(req, nth);
};

const fixCommit: PhaseScript = (req, nth) => {
  commitFile(req.cwd, `fix-${nth}.txt`, `fixed ${nth}\n`, `fix: finding ${nth}`);
  return answer({ fixed: 1, skipped: 0, commits: 1 }, { cursor: "sess-fix" });
};

describe("model phases", () => {
  after(() => {
    for (const e of executors) e.stop();
    for (const r of roots) rmSync(r, { recursive: true, force: true });
  });

  test("prompts: every template renders with no placeholder left, and a missing var throws", () => {
    const sample = {
      ref: "PROJ-1",
      branch: "PROJ-1",
      base: "main",
      worktree: "/tmp/wt",
      "run.dir": "/tmp/run",
      "project.path": "/tmp/repo",
      "project.kind": "node",
      guidance: "none",
      decisions: "(none)",
      plan_path: "/tmp/run/plans/PLAN-PROJ-1.md",
      findings_path: "/tmp/run/units/PROJ-1.findings.md",
      pr_number: 5,
      pr_url: "https://github.com/a/r/pull/5",
      seed_plan: "/tmp/repo/docs/plans/PLAN-PROJ-1.md",
      reviewers: "copilot-pull-request-reviewer",
      bot_logins: BOT_LOGINS.join(", "),
      bot_grace_minutes: 15,
      ticket: "PROJ-1: First",
      shape: "panel (3 lenses)",
      cycle: 1,
      lenses: "(a) correctness",
      effort: "high",
      verification: "",
      source: "the pre-push review",
      instructions: "apply",
      pass: 1,
      head_sha: "abc123",
      run_started: "2026-09-05T00:00:00Z",
    };
    const templates: [string, ModelPhase][] = [];
    for (const pipeline of readdirSync(PROMPTS, { withFileTypes: true })) {
      if (!pipeline.isDirectory()) continue;
      for (const file of readdirSync(join(PROMPTS, pipeline.name))) {
        if (file.endsWith(".md"))
          templates.push([pipeline.name, file.replace(/\.md$/, "") as ModelPhase]);
      }
    }
    assert.ok(templates.length >= 6, `found ${templates.length} templates`);
    for (const [dir, phase] of templates) {
      const pipeline = dir === "shared" ? "ticket" : (dir as "ticket" | "plan");
      const text = renderPhasePrompt(pipeline, phase, sample);
      assert.deepEqual(unresolvedPlaceholders(text), [], `${dir}/${phase}.md`);
      assert.match(text, /^# wise unit phase: /, `${dir}/${phase}.md starts with the phase line`);
      assert.match(
        text,
        /Return the fields directly, no wrapping/,
        `${dir}/${phase}.md ends plainly`,
      );
    }
    const { plan_path: _dropped, ...partial } = sample;
    assert.throws(() => renderPhasePrompt("ticket", "plan", partial), /unresolved/);
    // A `{{...}}` inside an injected VALUE is data (a ticket about mustache templating), not an
    // authoring placeholder: it must survive rendering instead of failing the unit.
    const braces = { ...sample, guidance: "render {{name}} with the mustache template" };
    const rendered = renderPhasePrompt("ticket", "plan", braces);
    assert.match(rendered, /render \{\{name\}\} with the mustache template/);
  });

  test("resolveUnitPhases: groups per phase, fix follows implement, review/watch defaults, step override", () => {
    const tuning = {
      build: { harness: "claude" as const, model: "haiku", effort: "low" as const },
      guard: { harness: "claude" as const, model: "sonnet", effort: "medium" as const },
    };
    const r = resolveUnitPhases(
      { ...STEP, groups: { plan: "build", implement: "build", review: "guard" } },
      tuning,
      "medium",
      {},
    );
    assert.equal(r.plan.harness, "claude");
    assert.match(r.plan.model, /haiku/);
    assert.equal(r.fix.model, r.implement.model, "fix inherits the implement group");
    assert.match(r.review.model, /sonnet/);
    assert.match(r.watch.model, /sonnet/, "watch defaults to sonnet");
    const low = resolveUnitPhases({ ...STEP, groups: {} }, {}, "low", {});
    assert.equal(low.plan.model, "claude-opus-4-8", "low profile never dispatches Opus 5");
    assert.equal(low.review.effort, "medium");
    const pinned = resolveUnitPhases({ ...STEP, model: "haiku", effort: "low" }, tuning, "max", {});
    for (const phase of MODEL_PHASES) assert.match(pinned[phase].model, /haiku/);
  });

  test("happy path: plan, implement, review approve, push, pr, request-review, watch green, merged, cleanup", async () => {
    const gh = happyGh();
    const f = fixture(fakeExec(gh.rule));
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      watch: watchGreen,
    });
    const res = await runUnitsStep(withAgent(f, s.starter));
    assert.equal(res.verdict, "units=1 merged=1 open=0 failed=0 skipped=0");
    assert.deepEqual(
      s.calls.map((c) => c.phase),
      ["plan", "implement", "review", "watch", "watch"],
    );
    assert.deepEqual(phasesOf(f.events), [
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
    const ledger = readUnit(f.runDir, "PROJ-1");
    assert.ok(ledger);
    assert.equal(ledger.verdict, "merged");
    assert.equal(ledger.cleaned, true);
    assert.equal(ledger.plan_path, join(f.runDir, "plans", "PLAN-PROJ-1.md"));
    assert.ok(existsSync(ledger.plan_path ?? ""), "engine plan path written by the child");
    assert.deepEqual(ledger.review, { converged: true, cycles: 1 });
    assert.equal(ledger.watch?.stable, CAP_DEFAULTS.watch_stable_passes);
    assert.equal(gh.state.pr, "MERGED");
    assert.deepEqual(f.sleeps, [CAP_DEFAULTS.watch_poll_seconds * 1000]);

    // Cursors stored per phase; usage folded into the ledger and reported upward.
    assert.equal(ledger.cursors.plan, "sess-plan");
    assert.equal(ledger.cursors.implement, "sess-impl");
    assert.equal(ledger.cursors.review, "sess-review");
    assert.equal(ledger.usage.input, 100 * s.calls.length);
    // M6.1: the per-phase split sums to the unit total; watch ran twice and accumulates.
    const byPhase = Object.values(ledger.usage_by_phase ?? {});
    assert.equal(
      byPhase.reduce((n, u) => n + (u?.input ?? 0), 0),
      ledger.usage.input,
    );
    assert.equal(ledger.usage_by_phase?.watch?.input, 200);
    assert.equal(ledger.usage.cost_source, "none");
    assert.equal(f.usage.length, s.calls.length);
    assert.deepEqual(
      f.usage.map((u) => `${u.phase}/${u.harness}`),
      ["plan/claude", "implement/claude", "review/claude", "watch/claude", "watch/claude"],
    );

    // Child requests: cwd, add_dirs, mode, tools, schema, token.
    const worktree = ledger.unit.worktree;
    for (const c of s.calls) {
      assert.equal(c.req.cwd, worktree);
      assert.deepEqual(c.req.add_dirs, [f.runDir, PLUGIN_ROOT, worktree]);
      assert.equal(c.req.mode, PHASE_MODE[c.phase]);
      assert.deepEqual(
        c.req.allowed_tools?.slice(0, PHASE_TOOLS[c.phase].length),
        PHASE_TOOLS[c.phase],
      );
      assert.ok(c.req.schema, "schema attached");
      assert.equal(c.req.step_token, "tok-1");
      assert.equal(c.req.resume, undefined, "fresh by default");
    }
    const review = s.ofPhase("review")[0];
    assert.ok(
      !review?.req.allowed_tools?.some((t) => /^(Edit|MultiEdit|Bash\(git commit)/.test(t)),
    );
    assert.match(review?.req.prompt ?? "", /panel \(3 lenses\)/);
    assert.match(s.ofPhase("plan")[0]?.req.prompt ?? "", /Do the thing\./, "ticket body injected");
    assert.match(s.ofPhase("plan")[0]?.req.prompt ?? "", /keep it small/, "guidance injected");

    // Phase events name the resolution once known.
    const planEv = f.events.find((e) => e.type === "unit.phase" && e.phase === "plan");
    assert.equal(planEv?.harness, "claude");
    assert.ok(planEv?.model);
    const done = f.events.find((e) => e.type === "unit.done");
    assert.equal(done?.verdict, "merged");
  });

  test("review/fix cycle converges on the second review; `resume: unit` hands the fixer the reviewer cursor", async () => {
    const f = fixture();
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewOnce,
      fix: fixCommit,
      watch: watchGreen,
    });
    const res = await runUnitsStep(withAgent(f, s.starter, { step: { ...STEP, resume: "unit" } }));
    assert.equal(res.outputs.units[0]?.verdict, "merged");
    assert.deepEqual(
      s.calls.map((c) => c.phase),
      ["plan", "implement", "review", "fix", "review", "watch", "watch"],
    );
    assert.deepEqual(readUnit(f.runDir, "PROJ-1")?.review, { converged: true, cycles: 2 });
    assert.deepEqual(phasesOf(f.events).slice(2, 7), [
      "plan",
      "implement",
      "review",
      "fix",
      "review",
    ]);
    const fix = s.ofPhase("fix")[0];
    assert.equal(fix?.req.resume, "sess-review-1", "E8: fixer resumes the reviewer session");
    assert.match(fix?.req.prompt ?? "", /the pre-push review/);
    assert.equal(fix?.req.mode, "full-access");
    // Two commits ahead of main: the feature and the fix.
    const wt = join(f.runDir, "worktrees", "PROJ-1");
    assert.equal(existsSync(wt), false, "merged worktree removed");
    assert.match(git(f.pair.clone, ["log", "--oneline", "origin/PROJ-1"]), /fix: finding 1/);
  });

  test("`resume: unit` across harnesses: the fixer starts clean when review ran on another CLI", async () => {
    const f = fixture();
    f.state.resolved["process.review"] = {
      harness: "codex",
      model: "gpt-5.6-sol",
      effort: "medium",
    };
    f.state.resolved["process.fix"] = { harness: "grok", model: "grok-4.6", effort: "high" };
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewOnce,
      fix: fixCommit,
      watch: watchGreen,
    });
    const res = await runUnitsStep(withAgent(f, s.starter, { step: { ...STEP, resume: "unit" } }));
    assert.equal(res.outputs.units[0]?.verdict, "merged");
    const fix = s.ofPhase("fix")[0];
    assert.equal(fix?.req.resume, undefined, "a codex cursor never reaches grok --resume");
    assert.match(
      readFileSync(res.log, "utf8"),
      /fix: fresh session \(review ran on codex, fix on grok/,
      "the fresh start is logged with both harnesses",
    );
  });

  test("state.permissions = full: every model phase runs full-access", async () => {
    const f = fixture();
    f.state.permissions = "full";
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewOnce,
      fix: fixCommit,
      watch: watchGreen,
    });
    const res = await runUnitsStep(withAgent(f, s.starter));
    assert.equal(res.outputs.units[0]?.verdict, "merged");
    assert.ok(s.calls.length >= 5);
    for (const c of s.calls) assert.equal(c.req.mode, "full-access", c.phase);
  });

  test("units `mcp: engine-only` reaches every model child's request", async () => {
    const f = fixture();
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewOnce,
      fix: fixCommit,
      watch: watchGreen,
    });
    const res = await runUnitsStep(
      withAgent(f, s.starter, { step: { ...STEP, mcp: "engine-only" } }),
    );
    assert.equal(res.outputs.units[0]?.verdict, "merged");
    assert.ok(s.calls.length >= 5);
    for (const c of s.calls) assert.equal(c.req.mcp_policy, "engine-only", c.phase);
    const dflt = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewOnce,
      fix: fixCommit,
      watch: watchGreen,
    });
    await runUnitsStep(withAgent(fixture(), dflt.starter));
    for (const c of dflt.calls) assert.equal(c.req.mcp_policy, undefined, c.phase);
  });

  test("a ticket persisted to a file: the plan prompt names the file, not the body", async () => {
    const f = fixture();
    f.state.context = {
      ticket: [
        {
          ref: "PROJ-1",
          title: "First",
          url: "https://t/PROJ-1",
          path: "/run/context/tickets/PROJ-1.md",
        },
      ],
    };
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewOnce,
      fix: fixCommit,
      watch: watchGreen,
    });
    await runUnitsStep(withAgent(f, s.starter));
    const prompt = s.ofPhase("plan")[0]?.req.prompt ?? "";
    assert.match(prompt, /file: \/run\/context\/tickets\/PROJ-1\.md/);
    assert.match(prompt, /Read it/);
    assert.equal(prompt.includes("Do the thing."), false);
  });

  test("default `resume: fresh`: the fixer gets no cursor", async () => {
    const f = fixture();
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewOnce,
      fix: fixCommit,
      watch: watchGreen,
    });
    await runUnitsStep(withAgent(f, s.starter));
    assert.equal(s.ofPhase("fix")[0]?.req.resume, undefined);
  });

  test("review cap exhausted: converged=false recorded, the unit still pushes and opens its PR", async () => {
    const f = fixture();
    const alwaysChanges: PhaseScript = (req, nth) => {
      writeFileSync(
        findingsOf(req),
        `1. src/a.ts:${nth} - still wrong - fix it - warning\n`,
        "utf8",
      );
      return answer({ findings: 1, blocking: 1, verdict: "changes-requested" });
    };
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: alwaysChanges,
      fix: fixCommit,
      watch: watchGreen,
    });
    const res = await runUnitsStep(withAgent(f, s.starter));
    assert.equal(res.outputs.units[0]?.verdict, "merged");
    assert.deepEqual(
      s.calls.map((c) => c.phase),
      ["plan", "implement", "review", "fix", "review", "watch", "watch"],
      "max_review_cycles=2: two reviews, one fix, then push",
    );
    const ledger = readUnit(f.runDir, "PROJ-1");
    assert.deepEqual(ledger?.review, { converged: false, cycles: 2 });
    assert.ok(ledger?.unit.pr, "PR opened despite non-convergence");
  });

  test("watch: red CI -> fix -> push -> green -> merged; fix attempts counted", async () => {
    const gh = happyGh();
    const f = fixture(fakeExec(gh.rule));
    const watch: PhaseScript = (req, nth) => {
      if (nth === 1) {
        writeFileSync(findingsOf(req), "1. tests - tests - FAIL src/a.test.ts\n", "utf8");
        return answer({
          ci: "red",
          bot_reviews: "pending",
          human_comment: false,
          merged: false,
          verdict: "fix",
        });
      }
      return watchGreen(req, nth);
    };
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      fix: fixCommit,
      watch,
    });
    const res = await runUnitsStep(withAgent(f, s.starter));
    assert.equal(res.outputs.units[0]?.verdict, "merged");
    assert.deepEqual(
      s.calls.map((c) => c.phase),
      ["plan", "implement", "review", "watch", "fix", "watch", "watch"],
    );
    assert.match(s.ofPhase("fix")[0]?.req.prompt ?? "", /failing CI checks/);
    const ledger = readUnit(f.runDir, "PROJ-1");
    assert.deepEqual(ledger?.watch, { passes: 3, fix_attempts: 1, stable: 2 });
    assert.deepEqual(phasesOf(f.events).slice(-5), [
      "request-review",
      "watch",
      "fix",
      "push",
      "cleanup",
    ]);
    assert.match(git(f.pair.clone, ["log", "--oneline", "origin/PROJ-1"]), /fix: finding 1/);
    // Head sha of the pushed branch reaches the second watch prompt.
    const head = git(f.pair.clone, ["rev-parse", "origin/PROJ-1"]);
    assert.match(s.ofPhase("watch")[1]?.req.prompt ?? "", new RegExp(head));
  });

  test("watch: fix attempts cap -> exhausted; a fix without a commit -> partial", async () => {
    const red: PhaseScript = (req) => {
      writeFileSync(findingsOf(req), "1. lint - lint - error\n", "utf8");
      return answer({
        ci: "red",
        bot_reviews: "pending",
        human_comment: false,
        merged: false,
        verdict: "fix",
      });
    };
    const f = fixture();
    f.state.caps.max_fix_attempts = 1;
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      fix: fixCommit,
      watch: red,
    });
    const res = await runUnitsStep(withAgent(f, s.starter));
    assert.equal(res.outputs.units[0]?.verdict, "exhausted");
    assert.match(res.outputs.units[0]?.reason ?? "", /max_fix_attempts \(1\)/);
    assert.equal(s.ofPhase("fix").length, 1);
    assert.equal(res.verdict, "units=1 merged=0 open=0 failed=1 skipped=0");

    const f2 = fixture();
    const noCommit: PhaseScript = () => answer({ fixed: 0, skipped: 1, commits: 0 });
    const s2 = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      fix: noCommit,
      watch: red,
    });
    const res2 = await runUnitsStep(withAgent(f2, s2.starter));
    assert.equal(res2.outputs.units[0]?.verdict, "partial");
    assert.ok(existsSync(join(f2.runDir, "worktrees", "PROJ-1")), "worktree kept for a human");
  });

  test("watch: human comment -> human-intervention, worktree and branch kept", async () => {
    const f = fixture();
    const human: PhaseScript = () =>
      answer({
        ci: "green",
        bot_reviews: "resolved",
        human_comment: true,
        merged: false,
        verdict: "needs-human",
      });
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      watch: human,
    });
    const res = await runUnitsStep(withAgent(f, s.starter));
    const row = res.outputs.units[0];
    assert.equal(row?.verdict, "human-intervention");
    assert.match(row?.reason ?? "", /human commented/);
    assert.equal(row?.cleaned, false);
    assert.ok(existsSync(join(f.runDir, "worktrees", "PROJ-1")));
    assert.equal(res.verdict, "units=1 merged=0 open=1 failed=0 skipped=0");
    assert.equal(s.ofPhase("watch").length, 1);
  });

  test("watch: stuck bot -> one substitute review per head, then merge; watch_minutes cap -> all-green", async () => {
    const f = fixture();
    const stuck: PhaseScript = () =>
      answer({
        ci: "green",
        bot_reviews: "stuck",
        human_comment: false,
        merged: false,
        verdict: "wait",
      });
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      watch: stuck,
    });
    const res = await runUnitsStep(withAgent(f, s.starter));
    assert.equal(res.outputs.units[0]?.verdict, "merged");
    assert.deepEqual(
      s.calls.map((c) => c.phase),
      ["plan", "implement", "review", "watch", "review", "watch"],
    );
    const sub = s.ofPhase("review")[1];
    assert.match(sub?.req.prompt ?? "", /universal/);
    assert.ok(readUnit(f.runDir, "PROJ-1")?.watch?.fallback_sha);

    // Time runs out while CI stays pending: the loop stands down with the PR open.
    const f2 = fixture();
    let now = 0;
    const pending: PhaseScript = () =>
      answer({
        ci: "pending",
        bot_reviews: "pending",
        human_comment: false,
        merged: false,
        verdict: "wait",
      });
    const s2 = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      watch: pending,
    });
    const res2 = await runUnitsStep(
      withAgent(f2, s2.starter, {
        now: () => now,
        sleep: () => {
          now += 20 * 60_000;
          return Promise.resolve();
        },
      }),
    );
    assert.equal(res2.outputs.units[0]?.verdict, "exhausted");
    assert.match(res2.outputs.units[0]?.reason ?? "", /watch_minutes \(45\)/);
    assert.equal(s2.ofPhase("watch").length, 3);
  });

  test("merge: squash refused -> merge commit fallback; both refused -> all-green with reason", async () => {
    const gh = happyGh({ squashFails: true });
    const f = fixture(fakeExec(gh.rule));
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      watch: watchGreen,
    });
    const res = await runUnitsStep(withAgent(f, s.starter));
    assert.equal(res.outputs.units[0]?.verdict, "merged");
    assert.ok(f.exec.gh.some((a) => a.join(" ").startsWith("pr merge 5 --merge")));

    const f2 = fixture(
      fakeExec((a) => {
        if (a[0] === "pr" && a[1] === "merge")
          return {
            code: 1,
            stdout: "",
            stderr: "protected branch: review required",
            timedOut: false,
          };
        return happyGh().rule(a);
      }),
    );
    const s2 = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      watch: watchGreen,
    });
    const res2 = await runUnitsStep(withAgent(f2, s2.starter));
    assert.equal(res2.outputs.units[0]?.verdict, "all-green");
    assert.match(res2.outputs.units[0]?.reason ?? "", /merge blocked: protected branch/);
  });

  test("plan: insufficient-context -> failed with the blueprint in the ledger, nothing implemented", async () => {
    const f = fixture();
    const plan: PhaseScript = (req) => {
      const blueprint = join(dirname(planFileFor(req)), "BLUEPRINT-PROJ-1.md");
      mkdirSync(dirname(blueprint), { recursive: true });
      writeFileSync(blueprint, "# questions\n", "utf8");
      return answer({ plan_path: "", status: "insufficient-context", blueprint_path: blueprint });
    };
    const s = scriptedStarter({ plan, implement: implementCommit });
    const res = await runUnitsStep(withAgent(f, s.starter));
    const row = res.outputs.units[0];
    assert.equal(row?.verdict, "failed");
    assert.equal(row?.reason, "plan-insufficient-context");
    const ledger = readUnit(f.runDir, "PROJ-1");
    assert.equal(ledger?.blueprint, join(f.runDir, "plans", "BLUEPRINT-PROJ-1.md"));
    assert.equal(ledger?.last_phase, "cleanup");
    assert.deepEqual(phasesOf(f.events), ["claim", "worktree", "plan", "cleanup"]);
    assert.equal(s.ofPhase("implement").length, 0);
    assert.equal(ledger?.usage.input, 100, "the plan child's usage still counted");
  });

  test("plan: `no-access` and a plan child that writes nothing both fail the unit", async () => {
    const f = fixture();
    const s = scriptedStarter({ plan: () => answer({ plan_path: "", status: "no-access" }) });
    const res = await runUnitsStep(withAgent(f, s.starter));
    assert.equal(res.outputs.units[0]?.reason, "plan-no-access");
    const f2 = fixture();
    const s2 = scriptedStarter({
      plan: () => answer({ plan_path: "/nowhere.md", status: "ready" }),
    });
    const res2 = await runUnitsStep(withAgent(f2, s2.starter));
    assert.match(res2.outputs.units[0]?.reason ?? "", /no plan file written/);
  });

  test("implement: done=0 or no new commit fails the unit before review", async () => {
    const f = fixture();
    const s = scriptedStarter({
      plan: planReady,
      implement: () => answer({ waves: 1, tasks: 2, done: 0, failed: 2, commits: 0 }),
      review: reviewApprove,
    });
    const res = await runUnitsStep(withAgent(f, s.starter));
    assert.equal(res.outputs.units[0]?.reason, "implement: done=0");
    assert.equal(s.ofPhase("review").length, 0);
    const f2 = fixture();
    const s2 = scriptedStarter({
      plan: planReady,
      implement: () => answer({ waves: 1, tasks: 1, done: 1, failed: 0, commits: 1 }),
    });
    const res2 = await runUnitsStep(withAgent(f2, s2.starter));
    assert.equal(res2.outputs.units[0]?.reason, "implement: no commits on the branch");
  });

  test("plan pipeline: the plan file is the unit ref and seeds the replan prompt", async () => {
    const f = fixture();
    mkdirSync(join(f.pair.clone, "docs", "plans"), { recursive: true });
    writeFileSync(join(f.pair.clone, "docs", "plans", "PLAN-Q-7.md"), "# Q-7 plan\n");
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      watch: watchGreen,
    });
    const res = await runUnitsStep(
      withAgent(f, s.starter, {
        step: { ...STEP, pipeline: "plan" },
        items: ["docs/plans/PLAN-Q-7.md"],
      }),
    );
    assert.equal(res.outputs.units[0]?.unit.branch, "Q-7");
    assert.equal(res.outputs.units[0]?.verdict, "merged");
    const plan = s.ofPhase("plan")[0];
    assert.match(plan?.req.prompt ?? "", /PLAN-Q-7\.md/, "seed plan named");
    assert.match(plan?.req.prompt ?? "", /^# wise unit phase: plan/);
    assert.equal(readUnit(f.runDir, "Q-7")?.plan_path, join(f.runDir, "plans", "PLAN-Q-7.md"));
  });

  test("cancel mid-phase kills the child; the unit records the cancellation", async () => {
    const f = fixture();
    const { starter, held } = heldStarter();
    const ac = new AbortController();
    const tracked: string[] = [];
    const run = runUnitsStep(
      f.input({
        signal: ac.signal,
        agent: {
          starter,
          stepToken: "tok",
          track: (key) => {
            tracked.push(key);
            return () => tracked.push(`-${key}`);
          },
        },
      }),
    );
    const deadline = Date.now() + 5000;
    while (held.length === 0 && Date.now() < deadline) await pause(5);
    assert.equal(held.length, 1, "plan child in flight");
    assert.deepEqual(tracked, ["PROJ-1/plan"]);
    ac.abort();
    const res = await run;
    assert.deepEqual(held[0]?.kills, ["SIGTERM"]);
    assert.deepEqual(tracked, ["PROJ-1/plan", "-PROJ-1/plan"]);
    assert.equal(res.outputs.units[0]?.verdict, "failed");
    assert.equal(res.outputs.units[0]?.reason, "plan: cancelled");
    assert.equal(readUnit(f.runDir, "PROJ-1")?.cursors.plan, "sess-held", "cursor kept for resume");
  });

  test("acquire: every child waits for a slot and releases it", async () => {
    const f = fixture();
    let inFlight = 0;
    let max = 0;
    const acquired: string[] = [];
    const s = scriptedStarter({
      plan: planReady,
      implement: implementCommit,
      review: reviewApprove,
      watch: watchGreen,
    });
    const res = await runUnitsStep(
      withAgent(f, s.starter, {
        agent: {
          starter: s.starter,
          stepToken: "tok",
          acquire: (harness) => {
            acquired.push(harness);
            inFlight++;
            max = Math.max(max, inFlight);
            return Promise.resolve(() => {
              inFlight--;
            });
          },
        },
      }),
    );
    assert.equal(res.outputs.units[0]?.verdict, "merged");
    assert.equal(acquired.length, s.calls.length);
    assert.equal(inFlight, 0, "every slot released");
    assert.equal(max, 1);
  });

  // ---- executor ---------------------------------------------------------------------------------------

  function executorFixture(): {
    rt: DaemonRuntime;
    ctx: CallContext;
    pair: RepoPair;
    root: string;
    env: Record<string, string>;
  } {
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
    return { rt, ctx, pair, root, env };
  }

  async function settled(runDir: string, ms = 20_000): Promise<State> {
    const deadline = Date.now() + ms;
    while (Date.now() < deadline) {
      const st = readState(runDir).status;
      if (st === "completed" || st === "failed" || st === "cancelled") break;
      await pause(20);
    }
    return readState(runDir);
  }

  test(
    "executor: two parallel units share one claude slot; children never exceed the harness cap",
    { timeout: 30_000 },
    async () => {
      const { rt, ctx, pair, root, env } = executorFixture();
      const s = scriptedStarter({
        plan: async (req, n) => {
          await pause(30);
          return planReady(req, n);
        },
        implement: async (req, n) => {
          await pause(30);
          return implementCommit(req, n);
        },
        review: reviewApprove,
        watch: watchGreen,
      });
      let inFlight = 0;
      let max = 0;
      const starter: typeof s.starter = (harness, req, onEvent) => {
        inFlight++;
        max = Math.max(max, inFlight);
        const h = s.starter(harness, req, onEvent);
        void h.done.finally(() => {
          inFlight--;
        });
        return h;
      };
      const exec = createExecutor(rt, {
        env,
        roots: { userRoot: EXEC_FIXTURES, bundledRoot: join(HERE, "..", "..", "workflows") },
        configPath: join(root, "engine.json"),
        adapters: { claude: fakeAdapter("claude", () => answer({})) },
        startAgent: starter,
        concurrency: { global: 4, harness: { claude: 1 } },
        unitsExec: fakeExec(happyGh().rule),
      });
      executors.add(exec);
      const { run_id } = await conductRun(
        exec,
        {
          workflow: "units-parallel",
          cwd: pair.clone,
          answers: { profile: "low", "input.tickets": "PROJ-1, PROJ-2" },
          context: {},
          inputs: {},
        },
        ctx,
      );
      const state = await settled(rt.requireRunDir(run_id));
      assert.equal(state.status, "completed", state.error);
      assert.equal(state.steps.process?.verdict, "units=2 merged=2 open=0 failed=0 skipped=0");
      assert.equal(max, 1, "harness cap held across both units");
      assert.equal(s.calls.length, 8, "plan, implement, review, watch per unit");
      assert.ok(state.usage.subscription.input >= 800);
      assert.equal(exec.isBusy(), false);
    },
  );

  test("executor: cancel kills the in-flight unit child", { timeout: 30_000 }, async () => {
    const { rt, ctx, pair, root, env } = executorFixture();
    const { starter, held } = heldStarter();
    const exec = createExecutor(rt, {
      env,
      roots: { userRoot: EXEC_FIXTURES, bundledRoot: join(HERE, "..", "..", "workflows") },
      configPath: join(root, "engine.json"),
      adapters: { claude: fakeAdapter("claude", () => answer({})) },
      startAgent: starter,
      unitsExec: fakeExec(happyGh().rule),
    });
    executors.add(exec);
    const { run_id } = await conductRun(
      exec,
      {
        workflow: "units-two",
        cwd: pair.clone,
        answers: { profile: "low", "input.tickets": "PROJ-1" },
        context: {},
        inputs: {},
      },
      ctx,
    );
    const deadline = Date.now() + 10_000;
    while (held.length === 0 && Date.now() < deadline) await pause(10);
    assert.equal(held.length, 1, "plan child started through the executor");
    assert.ok((held[0]?.req.step_token?.length ?? 0) > 0, "child carries the step token");
    exec.handlers.cancel({ run_id }, ctx);
    // Killed through `live.children` (the tracked child) and the step's abort signal: both SIGTERM.
    assert.ok((held[0]?.kills.length ?? 0) >= 1);
    assert.ok(held[0]?.kills.every((k) => k === "SIGTERM"));
    const state = await settled(rt.requireRunDir(run_id), 5000);
    assert.equal(state.status, "cancelled");
    // The killed child settles a tick later and gives its slot back.
    const idle = Date.now() + 5000;
    while (exec.isBusy() && Date.now() < idle) await pause(5);
    assert.equal(exec.isBusy(), false, "slot released after the kill");
    const types = readEvents(rt.requireRunDir(run_id)).map((e) => e.type);
    assert.ok(types.includes("unit.phase"));
  });
});
