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
import type { WorkflowDef } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "..", "..", "workflows", "ticket-plan", "workflow.yaml");

function fixtureDef(): WorkflowDef {
  const { def, issues } = validateDef(loadDef(FIXTURE), FIXTURE);
  assert.ok(def, issues.map((i) => i.message).join("\n"));
  return def;
}

function resolvedModels(def: WorkflowDef, profile: string): Record<string, string> {
  const applied = applyAnswers(def, { profile });
  const out: Record<string, string> = {};
  for (const [group, t] of Object.entries(applied.tuning)) {
    out[group] = resolveModelDict(t.model ?? "", t.effort ?? "", applied.profile).model;
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
  // The bundled ticket-plan is v2 since M3.1 (ticket-auto follows in M4).
  const def = fixtureDef();
  for (const [group, model] of Object.entries(resolvedModels(def, "low"))) {
    assert.ok(!/^(opus|claude-opus-5)/.test(model), `${group} resolved to ${model} under low`);
  }
  for (const model of Object.values(resolvedModels(def, "medium"))) {
    assert.ok(model === "opus" || model === "sonnet", `medium keeps aliases, got ${model}`);
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
