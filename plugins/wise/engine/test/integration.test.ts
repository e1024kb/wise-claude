// Cross-module tests ported from test_tuning.py (get-profiles low rule) and
// test_neutralization.py (off-Claude resume via the synthetic session id).
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { loadDef, validateDef } from "../src/defs.ts";
import { applyAnswers } from "../src/preflight.ts";
import { LOW_PROFILE_OPUS_MODEL, resolveModelDict } from "../src/resolve.ts";
import { findRunsBySession, initState, startRun, updateRun } from "../src/ledger.ts";
import { syntheticSessionId } from "../src/profile.ts";
import { resolveUnitPhases } from "../src/units.ts";
import type { WorkflowDef } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const WORKFLOWS = join(HERE, "..", "..", "workflows");
/** Every bundled workflow is v2 since M4.4; the two `units` workflows carry per-phase tiers. */
const BUNDLED_V2 = ["ticket-plan", "example-workflow", "ticket-auto", "impl-plan-auto"];
const UNITS_WORKFLOWS = ["ticket-auto", "impl-plan-auto"];
const OPUS_5 = /^(opus|claude-opus-5)/;

function bundledDef(name: string): WorkflowDef {
  const path = join(WORKFLOWS, name, "workflow.yaml");
  const { def, issues } = validateDef(loadDef(path), path);
  assert.ok(def, `${name}: ${issues.map((i) => i.message).join("\n")}`);
  return def;
}

function fixtureDef(): WorkflowDef {
  return bundledDef("ticket-plan");
}

function resolvedModels(def: WorkflowDef, profile: string): Record<string, string> {
  const applied = applyAnswers(def, { profile });
  const out: Record<string, string> = {};
  for (const [group, t] of Object.entries(applied.tuning)) {
    out[group] = resolveModelDict(t.model ?? "", t.effort ?? "", applied.profile).model;
  }
  return out;
}

/** `<step>.<phase>` -> resolved model for every `units` step of `def` under `profile`. */
function resolvedPhaseModels(def: WorkflowDef, profile: string): Record<string, string> {
  const applied = applyAnswers(def, { profile });
  const out: Record<string, string> = {};
  for (const step of def.steps) {
    if (step.type !== "units") continue;
    const phases = resolveUnitPhases(step, applied.tuning, applied.profile, {});
    for (const [phase, r] of Object.entries(phases)) out[`${step.id}.${phase}`] = r.model;
  }
  return out;
}

test("test_get_profiles_low_applies_rule", () => {
  const def = fixtureDef();
  const models = resolvedModels(def, "low");
  // `evidence` is pinned to sonnet by the low profile; `authoring` is Opus and must be 4.8.
  assert.equal(models.evidence, "sonnet");
  assert.equal(models.authoring, LOW_PROFILE_OPUS_MODEL);
});

test("test_get_profiles_low_explicit_4_8_is_a_noop", () => {
  const def = fixtureDef();
  const low = def.profiles?.low;
  assert.ok(low?.tuning?.authoring);
  assert.equal(low.tuning.authoring.model, LOW_PROFILE_OPUS_MODEL);
  const r = resolveModelDict(LOW_PROFILE_OPUS_MODEL, "high", "low");
  assert.equal(r.model, LOW_PROFILE_OPUS_MODEL);
  assert.equal(r.reason, undefined);
});

test("test_bundled_low_profiles_never_resolve_to_opus_5", () => {
  for (const name of BUNDLED_V2) {
    const def = bundledDef(name);
    for (const [group, model] of Object.entries(resolvedModels(def, "low"))) {
      assert.ok(!OPUS_5.test(model), `${name}.${group} resolved to ${model} under low`);
    }
    for (const [key, model] of Object.entries(resolvedPhaseModels(def, "low"))) {
      assert.ok(!OPUS_5.test(model), `${name} unit phase ${key} resolved to ${model} under low`);
    }
    for (const [group, model] of Object.entries(resolvedModels(def, "medium"))) {
      assert.ok(
        model === "opus" || model === "sonnet" || model === "haiku",
        `${name}.${group}: medium keeps aliases, got ${model}`,
      );
    }
  }
});

test("units workflows: low pins Opus 4.8 for plan and review, sonnet for implement, fix and watch", () => {
  for (const name of UNITS_WORKFLOWS) {
    const def = bundledDef(name);
    const low = resolvedPhaseModels(def, "low");
    assert.equal(low["process.plan"], LOW_PROFILE_OPUS_MODEL, name);
    assert.equal(low["process.review"], LOW_PROFILE_OPUS_MODEL, name);
    assert.equal(low["process.implement"], "sonnet", name);
    assert.equal(low["process.fix"], "sonnet", `${name}: fix follows the implement group`);
    assert.equal(low["process.watch"], "sonnet", name);
    const medium = resolvedPhaseModels(def, "medium");
    for (const phase of ["plan", "implement", "review", "fix"]) {
      assert.equal(medium[`process.${phase}`], "opus", `${name}.${phase} under medium`);
    }
    assert.equal(medium["process.watch"], "sonnet", name);
    // Every cap the step names is set by every declared profile.
    const step = def.steps.find((s) => s.id === "process");
    assert.ok(step && step.type === "units");
    for (const level of ["low", "medium", "max"] as const) {
      const caps = applyAnswers(def, { profile: level }).caps;
      for (const cap of step.caps ?? []) {
        assert.ok(caps[cap] !== undefined, `${name}: cap ${cap} unset under ${level}`);
      }
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
