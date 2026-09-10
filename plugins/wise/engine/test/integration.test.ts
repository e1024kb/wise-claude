// Cross-module tests: the bundled workflows against the model catalog and resolve, plus
// test_neutralization.py (off-Claude resume via the synthetic session id).
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { loadDef, validateDef } from "../src/defs.ts";
import { applyAnswers } from "../src/preflight.ts";
import { catalogFor } from "../src/models.ts";
import { LOW_PROFILE_OPUS_MODEL, resolveModelDict } from "../src/resolve.ts";
import { findRunsBySession, initState, startRun, updateRun } from "../src/ledger.ts";
import { syntheticSessionId } from "../src/profile.ts";
import { resolveUnitPhases } from "../src/units.ts";
import type { WorkflowDef } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const WORKFLOWS = join(HERE, "..", "..", "workflows");
/** Every bundled workflow is v2 since M4.4; the two `units` workflows carry per-phase tiers. */
const BUNDLED_V2 = [
  "ticket-plan",
  "example-workflow",
  "ticket-auto",
  "impl-plan-auto",
  "code-review",
];
const UNITS_WORKFLOWS = ["ticket-auto", "impl-plan-auto"];
const OPUS_5 = /^(opus|claude-opus-5)/;

function bundledDef(name: string): WorkflowDef {
  const path = join(WORKFLOWS, name, "workflow.yaml");
  const { def, issues } = validateDef(loadDef(path), path);
  assert.ok(def, `${name}: ${issues.map((i) => i.message).join("\n")}`);
  return def;
}

function resolvedModels(def: WorkflowDef): Record<string, string> {
  const applied = applyAnswers(def, {});
  const out: Record<string, string> = {};
  for (const [group, t] of Object.entries(applied.tuning)) {
    out[group] = resolveModelDict(t.model ?? "", t.effort ?? "", applied.profile).model;
  }
  return out;
}

/** `<step>.<phase>` -> resolved model for every `units` step of `def` at the defaults. */
function resolvedPhaseModels(def: WorkflowDef): Record<string, string> {
  const applied = applyAnswers(def, {});
  const out: Record<string, string> = {};
  for (const step of def.steps) {
    if (step.type !== "units") continue;
    const phases = resolveUnitPhases(step, applied.tuning, applied.profile, {});
    for (const [phase, r] of Object.entries(phases)) out[`${step.id}.${phase}`] = r.model;
  }
  return out;
}

test("the low-profile Opus rule still resolves when asked for directly", () => {
  // `resolve.ts` keeps the rule for callers that pass a profile; workflows always pass `medium`.
  assert.equal(resolveModelDict("opus", "high", "low").model, LOW_PROFILE_OPUS_MODEL);
  const r = resolveModelDict(LOW_PROFILE_OPUS_MODEL, "high", "low");
  assert.equal(r.model, LOW_PROFILE_OPUS_MODEL);
  assert.equal(r.reason, undefined);
  assert.equal(resolveModelDict("claude-opus-5", "high", "medium").model, "claude-opus-5");
});

test("bundled workflows: every unlocked group resolves to a catalog id of its harness", () => {
  for (const name of BUNDLED_V2) {
    const def = bundledDef(name);
    const applied = applyAnswers(def, {});
    for (const group of def.tuning?.groups ?? []) {
      const t = applied.tuning[group.id];
      assert.ok(t, `${name}.${group.id}`);
      const harness = t.harness ?? "claude";
      const ids = catalogFor(harness).map((m) => m.id);
      assert.ok(
        ids.includes(t.model ?? ""),
        `${name}.${group.id}: ${t.model} not in ${harness} catalog`,
      );
      assert.ok(!OPUS_5.test(t.model ?? "") || t.model === "claude-opus-5", `${name}.${group.id}`);
    }
    for (const [group, model] of Object.entries(resolvedModels(def))) {
      assert.ok(model.startsWith("claude-"), `${name}.${group}: resolved ${model}`);
    }
  }
});

test("code-review waits for every lens and gates missing reports before curation", () => {
  const def = bundledDef("code-review");
  assert.equal(def.preflight?.["control-mode"], "interactive");
  const health = def.steps.find((step) => step.id === "review-health");
  assert.equal(health?.type, "bash");
  assert.deepEqual(health?.depends_on, ["review-correctness", "review-security", "review-tests"]);
  assert.equal(health?.["trigger-rule"], "all-done");
  const gate = def.steps.find((step) => step.id === "review-errors");
  assert.equal(gate?.type, "ask");
  assert.equal(gate?.when, "missing_reviews != 'none'");
  const curate = def.steps.find((step) => step.id === "curate");
  assert.deepEqual(curate?.depends_on, ["review-health", "review-errors"]);
  assert.match(String(curate?.when), /review_failure_action/);
});

test("units workflows: plan, implement, review and fix run on Opus 5, watch on Sonnet 5; every cap is set", () => {
  for (const name of UNITS_WORKFLOWS) {
    const def = bundledDef(name);
    const models = resolvedPhaseModels(def);
    for (const phase of ["plan", "implement", "review", "fix"]) {
      assert.equal(models[`process.${phase}`], "claude-opus-5", `${name}.${phase}`);
    }
    assert.equal(models["process.watch"], "claude-sonnet-5", name);
    const step = def.steps.find((s) => s.id === "process");
    assert.ok(step && step.type === "units");
    const caps = applyAnswers(def, {}).caps;
    for (const cap of step.caps ?? []) {
      assert.ok(caps[cap] !== undefined, `${name}: cap ${cap} unset`);
    }
  }
});

test("test_find_runs_by_session_matches_synthetic_id", () => {
  const runsRoot = mkdtempSync(join(tmpdir(), "wise-int-"));
  const cwd = mkdtempSync(join(tmpdir(), "wise-ws-"));
  const sid = syntheticSessionId({ cwd, env: {} });
  assert.match(sid, /^local-/);
  const runDir = join(runsRoot, "01RUN");
  initState({
    runDir,
    runId: "01RUN",
    workflow: { name: "ticket-plan", version: 2, dir: "/wf" },
    stepIds: ["a"],
    cwd,
    harnessSession: sid,
  });
  startRun(runDir, {});
  updateRun(runDir, { status: "failed" });
  const rows = findRunsBySession(runsRoot, sid, {});
  assert.equal(rows.length, 1);
  assert.equal(rows[0]?.run_id, "01RUN");
  assert.equal(rows[0]?.fresh, true);
  assert.equal(findRunsBySession(runsRoot, "other-session", {}).length, 0);
});
