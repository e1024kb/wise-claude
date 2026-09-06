// Port of plugins/wise/tests/test_effort_ceiling.py, test_low_profile_model.py
// and the resolve-team tests of test_tuning.py. Test names match the Python
// names; parametrized tests become loops.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { stringify as toYaml } from "yaml";
import {
  LOW_PROFILE_OPUS_MODEL,
  MODEL_EFFORT_SUPPORT,
  ResolveError,
  cmdListAgents,
  cmdResolveModel,
  cmdResolveTeam,
  effortCeiling,
  effortCeilings,
  effortFor,
  isSnapshotOf,
  modelFamily,
  parseFrontmatter,
  resolveModelDict,
  rosterAgents,
  rosterNames,
} from "../src/resolve.ts";
import type { ModelResolution, TeamMember, TeamResult } from "../src/resolve.ts";
import { EFFORTS, HARNESSES } from "../src/types.ts";
import type { Effort } from "../src/types.ts";

// Every test starts from the shipped table, not the developer's env.
const NO_ENV = {} as const;
const env = (value: string) => ({ WISE_EFFORT_CEILING: value });
const resolve = (pinned: string, effort = "", profile = ""): ModelResolution =>
  resolveModelDict(pinned, effort, profile, { env: NO_ENV });

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "wise-resolve-"));
}

function writeDef(dir: string, steps: unknown[]): string {
  const p = join(dir, "workflow.yaml");
  writeFileSync(p, toYaml({ version: 1, name: "t", description: "d", steps }));
  return p;
}

function teamDef(dir: string, qaModel?: string): string {
  const qa: Record<string, unknown> = { role: "qa-engineer" };
  if (qaModel) qa["model"] = qaModel;
  return writeDef(dir, [
    {
      id: "solo",
      type: "prompt",
      prompt: "x",
      agent: "architect",
      model: "opus",
      effort: "xhigh",
      depends_on: [],
    },
    {
      id: "panel",
      type: "prompt",
      prompt: "y",
      agent: [{ role: "architect", lead: true, model: "opus" }, qa],
      model: "opus",
      effort: "high",
      depends_on: [],
    },
  ]);
}

function team(path: string, stepId: string, opts = {}): TeamResult {
  return cmdResolveTeam(path, stepId, { env: NO_ENV, ...opts });
}

function first(data: TeamResult): TeamMember {
  const m = data.members[0];
  assert.ok(m, "expected at least one member");
  return m;
}

// ---- test_effort_ceiling.py: shipped table ------------------------------------

test("test_ceiling_table", () => {
  const cases: [string, string, string][] = [
    ["opus", "xhigh", "high"],
    ["opus", "max", "high"],
    ["opus", "high", "high"],
    ["opus", "low", "low"],
    ["claude-opus-5", "xhigh", "high"],
    ["claude-opus-5-20260401", "xhigh", "high"],
    ["claude-opus-50-20270101", "xhigh", "xhigh"],
    ["claude-opus-5-1", "xhigh", "xhigh"],
    ["claude-opus-5-1-20270101", "xhigh", "xhigh"],
    ["claude-opus-5-2026040", "xhigh", "xhigh"],
    ["claude-opus-4-8", "xhigh", "xhigh"],
    ["claude-opus-4-8", "max", "xhigh"],
    ["claude-opus-4-7", "xhigh", "xhigh"],
    ["sonnet", "xhigh", "xhigh"],
    ["fable", "max", "max"],
    ["inherit", "xhigh", "xhigh"],
  ];
  for (const [model, effort, expected] of cases) {
    assert.equal(resolve(model, effort).effort, expected, `${model} / ${effort}`);
  }
});

test("test_snapshot_match_requires_a_date_suffix", () => {
  assert.ok(isSnapshotOf("claude-opus-5-20260401", "claude-opus-5"));
  assert.ok(!isSnapshotOf("claude-opus-50-20270101", "claude-opus-5"));
  assert.ok(!isSnapshotOf("claude-opus-5-1", "claude-opus-5"));
  assert.ok(!isSnapshotOf("claude-opus-5", "claude-opus-5"));
});

test("test_ceiling_reason_is_surfaced", () => {
  const reason = resolve("opus", "xhigh").reason ?? "";
  assert.ok(
    reason.includes("xhigh") && reason.includes("high") && reason.includes("policy ceiling"),
  );
});

test("test_at_ceiling_has_no_reason", () => {
  assert.equal(resolve("opus", "high").reason, undefined);
});

test("test_non_standard_effort_untouched", () => {
  assert.equal(resolve("opus", "bogus").effort, "bogus");
});

test("test_no_effort_no_ceiling", () => {
  assert.equal(resolve("opus", "").effort, "");
});

test("test_capability_and_policy_reasons_are_distinguishable", () => {
  const effortSupport = { ...MODEL_EFFORT_SUPPORT, sonnet: new Set(["low", "medium", "high"]) };
  const cap = resolveModelDict("sonnet", "xhigh", "", { env: NO_ENV, effortSupport });
  assert.equal(cap.effort, "high");
  assert.ok(cap.reason?.includes("capability ceiling"));
  const pol = resolveModelDict("opus", "xhigh", "", { env: NO_ENV, effortSupport });
  assert.ok(pol.reason?.includes("policy ceiling"));
  assert.ok(!pol.reason?.includes("capability"));
});

test("test_haiku_still_drops_effort", () => {
  const out = resolve("haiku", "xhigh");
  assert.equal(out.effort, "");
  assert.ok(out.reason?.includes("no effort control"));
});

test("test_ceiling_applies_after_retired_substitution", () => {
  const out = resolve("claude-opus-4-1-20250805", "xhigh");
  assert.deepEqual([out.model, out.effort], ["opus", "high"]);
  assert.ok(out.reason?.includes("deprecated") && out.reason.includes("policy ceiling"));
});

// ---- test_effort_ceiling.py: WISE_EFFORT_CEILING override ----------------------

test("test_env_off_disables_every_ceiling", () => {
  assert.equal(resolveModelDict("opus", "max", "", { env: env("off") }).effort, "max");
});

test("test_env_raises_one_model", () => {
  const e = env("opus=xhigh");
  assert.equal(resolveModelDict("opus", "max", "", { env: e }).effort, "xhigh");
  assert.equal(resolveModelDict("claude-opus-5", "xhigh", "", { env: e }).effort, "high");
});

test("test_env_lowers_one_model", () => {
  const e = env("claude-opus-5=medium");
  assert.equal(resolveModelDict("claude-opus-5", "xhigh", "", { env: e }).effort, "medium");
});

test("test_env_drops_one_entry", () => {
  const e = env("opus=off");
  assert.equal(resolveModelDict("opus", "max", "", { env: e }).effort, "max");
  assert.equal(resolveModelDict("claude-opus-5", "xhigh", "", { env: e }).effort, "high");
});

test("test_env_junk_is_ignored", () => {
  for (const value of ["junk", "opus", "=high", "opus=nonsense", " , "]) {
    assert.equal(resolveModelDict("opus", "xhigh", "", { env: env(value) }).effort, "high", value);
  }
});

test("test_memo_rebuilds_when_the_env_changes", () => {
  // No memo in the port: every call reads the env it is given.
  assert.equal(resolve("opus", "max").effort, "high");
  assert.equal(resolveModelDict("opus", "max", "", { env: env("off") }).effort, "max");
  assert.equal(resolveModelDict("opus", "max", "", { env: env("opus=xhigh") }).effort, "xhigh");
  assert.equal(resolve("opus", "max").effort, "high");
});

test("test_memo_is_not_mutated_by_callers", () => {
  const before = new Map(effortCeilings(NO_ENV));
  effortCeiling("claude-opus-5-20260401", NO_ENV);
  assert.deepEqual(effortCeilings(NO_ENV), before);
});

test("test_env_adds_an_untabled_model", () => {
  const e = env("sonnet=medium");
  assert.equal(resolveModelDict("sonnet", "xhigh", "", { env: e }).effort, "medium");
});

// ---- test_effort_ceiling.py: dispatch surfaces ---------------------------------

test("test_resolve_model_cli_emits_capped_effort", () => {
  const out = cmdResolveModel("opus", "xhigh", "", { env: NO_ENV });
  assert.equal(out.effort, "high");
  assert.ok(out.reason?.includes("policy ceiling"));
});

// ---- test_low_profile_model.py ------------------------------------------------

test("test_constant_is_opus_4_8", () => {
  assert.equal(LOW_PROFILE_OPUS_MODEL, "claude-opus-4-8");
});

test("test_low_profile_swaps_every_opus_pin", () => {
  const pins = [
    "opus",
    "claude-opus-5",
    "claude-opus-5-20260401",
    "claude-opus-5-1",
    "claude-opus-4-1-20250805",
  ];
  for (const pinned of pins) {
    const out = resolve(pinned, "high", "low");
    assert.equal(out.model, "claude-opus-4-8", pinned);
    assert.equal(out.effort, "high", pinned);
    assert.ok(out.reason?.includes("low profile:"), pinned);
    assert.ok(out.reason?.includes("claude-opus-4-8"), pinned);
    assert.equal(out.next_fallback, "sonnet", pinned);
  }
});

test("test_low_profile_leaves_opus_4_8_alone", () => {
  for (const pinned of ["claude-opus-4-8", "claude-opus-4-8-20260101"]) {
    const out = resolve(pinned, "high", "low");
    assert.equal(out.model, pinned);
    assert.equal(out.reason, undefined, pinned);
  }
});

test("test_low_profile_ignores_non_opus_families", () => {
  const cases: [string, string][] = [
    ["sonnet", "high"],
    ["haiku", ""],
    ["fable", "high"],
    ["inherit", "high"],
    ["", "high"],
  ];
  for (const [pinned, effort] of cases) {
    const out = resolve(pinned, effort, "low");
    assert.equal(out.model, pinned || "inherit");
    assert.ok(!(out.reason ?? "").startsWith("low profile"), pinned);
  }
});

test("test_other_profiles_keep_opus_5", () => {
  for (const profile of ["", "medium", "max", "MEDIUM"]) {
    const out = resolve("opus", "xhigh", profile);
    assert.equal(out.model, "opus", profile);
    assert.equal(out.effort, "high", profile);
    assert.ok(!out.reason?.includes("low profile"), profile);
  }
});

test("test_low_profile_effort_clamps_on_substituted_model", () => {
  let out = resolve("opus", "xhigh", "low");
  assert.deepEqual([out.model, out.effort], ["claude-opus-4-8", "xhigh"]);
  assert.ok(!out.reason?.includes("policy ceiling"));
  out = resolve("opus", "max", "low");
  assert.deepEqual([out.model, out.effort], ["claude-opus-4-8", "xhigh"]);
});

test("test_low_profile_case_insensitive", () => {
  assert.equal(resolve("opus", "high", "LOW").model, "claude-opus-4-8");
});

test("test_retired_id_reason_composes_with_rule", () => {
  const out = resolve("claude-opus-4-1-20250805", "high", "low");
  assert.equal(out.fell_back, true);
  assert.ok(out.reason?.includes("deprecated"));
  assert.ok(out.reason?.includes("low profile:"));
});

test("test_cmd_resolve_model_profile_flag", () => {
  const data = cmdResolveModel("opus", "high", "low", { env: NO_ENV });
  assert.equal(data.model, "claude-opus-4-8");
});

test("test_cmd_resolve_model_rejects_unknown_profile", () => {
  assert.throws(
    () => cmdResolveModel("opus", "high", "turbo", { env: NO_ENV }),
    (e: unknown) =>
      e instanceof ResolveError &&
      e.exitCode === 2 &&
      e.message.includes("INVALID:profile-level:turbo"),
  );
});

test("test_resolve_team_low_swaps_step_pin", () => {
  const path = teamDef(tmp(), "sonnet");
  const m = first(team(path, "solo", { profile: "low" }));
  assert.deepEqual([m.model, m.effort], ["claude-opus-4-8", "xhigh"]);
  assert.ok(m.reason?.includes("low profile:"));
});

test("test_resolve_team_low_swaps_only_opus_members", () => {
  const path = teamDef(tmp(), "sonnet");
  const data = team(path, "panel", { profile: "low" });
  const byRole = new Map(data.members.map((m) => [m.role, m]));
  assert.equal(byRole.get("architect")?.model, "claude-opus-4-8");
  assert.equal(byRole.get("qa-engineer")?.model, "sonnet");
  assert.equal(byRole.get("qa-engineer")?.reason, undefined);
});

test("test_resolve_team_low_swaps_override_too", () => {
  const path = teamDef(tmp(), "sonnet");
  const data = team(path, "panel", {
    modelOverride: "opus",
    effortOverride: "high",
    teamMode: "solo",
    profile: "low",
  });
  assert.equal(data.mode, "single");
  const m = first(data);
  assert.equal(m.model, "claude-opus-4-8");
  assert.ok(m.reason?.includes("run tuning override"));
  assert.ok(m.reason?.includes("low profile:"));
  assert.ok(m.reason?.includes("solo mode"));
});

test("test_resolve_team_medium_keeps_opus_5", () => {
  const path = teamDef(tmp(), "sonnet");
  const m = first(team(path, "solo", { profile: "medium" }));
  assert.deepEqual([m.model, m.effort], ["opus", "high"]);
});

test("test_resolve_team_unknown_profile_is_an_error", () => {
  const path = teamDef(tmp(), "sonnet");
  const data = team(path, "solo", { profile: "turbo" });
  assert.ok(data.errors.some((e) => e.includes("--profile")));
  assert.equal(first(data).model, "opus");
});

// ---- test_tuning.py: resolve-team overrides -----------------------------------

test("test_resolve_team_no_override_uses_step_pin", () => {
  const path = teamDef(tmp());
  const m = first(team(path, "solo"));
  assert.deepEqual([m.model, m.effort], ["opus", "high"]);
  assert.ok(m.reason?.includes("policy ceiling"));
  assert.ok(!m.reason?.includes("run tuning override"));
});

test("test_resolve_team_override_wins_over_step_pin", () => {
  const path = teamDef(tmp());
  const m = first(team(path, "solo", { modelOverride: "sonnet", effortOverride: "high" }));
  assert.deepEqual([m.model, m.effort], ["sonnet", "high"]);
  assert.ok(m.reason?.includes("run tuning override"));
});

test("test_resolve_team_override_wins_over_member_pin", () => {
  const path = teamDef(tmp());
  const data = team(path, "panel", { modelOverride: "sonnet", effortOverride: "high" });
  assert.equal(data.mode, "team");
  assert.equal(data.members.length, 2);
  for (const m of data.members) {
    assert.deepEqual([m.model, m.effort], ["sonnet", "high"]);
    assert.ok(m.reason?.includes("run tuning override"));
  }
});

test("test_resolve_team_override_still_clamped", () => {
  const path = teamDef(tmp());
  const m = first(team(path, "solo", { modelOverride: "haiku", effortOverride: "xhigh" }));
  assert.equal(m.model, "haiku");
  assert.equal(m.effort, "");
  assert.ok(m.reason?.includes("no effort control"));
});

// ---- test_tuning.py: resolve-team --team-mode ---------------------------------

test("test_resolve_team_solo_collapses_to_lead", () => {
  const path = teamDef(tmp());
  const data = team(path, "panel", { teamMode: "solo" });
  assert.equal(data.mode, "single");
  assert.equal(data.lead, "architect");
  assert.equal(data.members.length, 1);
  assert.equal(first(data).role, "architect");
  assert.deepEqual(data.collapsed, { from: 2, dropped: ["qa-engineer"] });
  assert.ok(first(data).reason?.includes("team collapsed to lead (solo mode)"));
});

test("test_resolve_team_solo_no_lead_keeps_first_member", () => {
  const path = writeDef(tmp(), [
    {
      id: "panel",
      type: "prompt",
      prompt: "y",
      agent: ["architect", "qa-engineer", "product-manager"],
      model: "opus",
      effort: "high",
      depends_on: [],
    },
  ]);
  const data = team(path, "panel", { teamMode: "solo" });
  assert.equal(data.mode, "single");
  assert.equal(data.lead, null);
  assert.equal(first(data).role, "architect");
  assert.equal(data.collapsed?.from, 3);
  assert.deepEqual(data.collapsed?.dropped, ["qa-engineer", "product-manager"]);
  assert.ok(first(data).reason?.includes("no declared lead"));
});

test("test_resolve_team_solo_noop_on_single", () => {
  const path = teamDef(tmp());
  const data = team(path, "solo", { teamMode: "solo" });
  assert.equal(data.mode, "single");
  assert.ok(!("collapsed" in data));
});

test("test_resolve_team_solo_composes_with_override", () => {
  const path = teamDef(tmp());
  const data = team(path, "panel", {
    modelOverride: "sonnet",
    effortOverride: "high",
    teamMode: "solo",
  });
  const m = first(data);
  assert.deepEqual([m.model, m.effort], ["sonnet", "high"]);
  assert.ok(m.reason?.includes("run tuning override"));
  assert.equal(data.collapsed?.from, 2);
});

test("test_resolve_team_full_mode_shape_unchanged", () => {
  const path = teamDef(tmp());
  const data = team(path, "panel");
  assert.equal(data.mode, "team");
  assert.ok(!("collapsed" in data));
  assert.deepEqual(Object.keys(data).toSorted(), ["errors", "lead", "members", "mode"]);
});

test("test_resolve_team_unknown_team_mode_is_error_not_collapse", () => {
  const path = teamDef(tmp());
  const data = team(path, "panel", { teamMode: "duo" });
  assert.ok(data.errors.some((e) => e.includes("--team-mode")));
  assert.equal(data.mode, "team");
  assert.ok(!("collapsed" in data));
});

test("test_resolve_team_solo_keeps_errors_of_dropped_members", () => {
  const path = writeDef(tmp(), [
    {
      id: "panel",
      type: "prompt",
      prompt: "y",
      agent: [{ role: "architect", lead: true }, { role: "not-a-role" }],
      model: "opus",
      effort: "high",
      depends_on: [],
    },
  ]);
  const data = team(path, "panel", { teamMode: "solo" });
  assert.equal(data.mode, "single");
  assert.equal(first(data).role, "architect");
  assert.ok(data.errors.some((e) => e.includes("not-a-role")));
  assert.deepEqual(data.collapsed?.dropped, ["not-a-role"]);
});

test("test_resolve_team_solo_noop_on_auto_and_unset", () => {
  const path = writeDef(tmp(), [
    { id: "routed", type: "prompt", prompt: "x", agent: "auto", depends_on: [] },
    { id: "plain", type: "prompt", prompt: "y", depends_on: [] },
  ]);
  for (const [sid, mode] of [
    ["routed", "auto"],
    ["plain", "unset"],
  ] as const) {
    const data = team(path, sid, { teamMode: "solo" });
    assert.equal(data.mode, mode);
    assert.ok(!("collapsed" in data));
  }
});

// ---- v2 additions (no Python counterpart) --------------------------------------

test("every resolution carries the harness, defaulting to claude", () => {
  assert.equal(resolve("opus", "high").harness, "claude");
  assert.equal(
    resolveModelDict("opus", "high", "", { env: NO_ENV, harness: "codex" }).harness,
    "codex",
  );
  assert.equal(first(team(teamDef(tmp()), "solo")).harness, "claude");
});

test("effortFor: identity for claude/codex/grok, none for gemini", () => {
  for (const harness of HARNESSES) {
    for (const effort of EFFORTS) {
      const expected: Effort | undefined = harness === "gemini" ? undefined : effort;
      assert.equal(effortFor(harness, effort), expected, `${harness}/${effort}`);
    }
  }
});

test("modelFamily normalises aliases, ids and unknowns", () => {
  assert.equal(modelFamily(""), "inherit");
  assert.equal(modelFamily("inherit"), "inherit");
  assert.equal(modelFamily("Opus"), "opus");
  assert.equal(modelFamily("claude-sonnet-4-6"), "sonnet");
  assert.equal(modelFamily("haiku-latest"), "haiku");
  assert.equal(modelFamily("gpt-5"), "");
});

test("rosterAgents reads the bundled roster", () => {
  const roster = rosterAgents();
  assert.ok(roster.length >= 13);
  const architect = roster.find((a) => a.name === "architect");
  assert.ok(architect);
  assert.ok(architect.tools.includes("Write"));
  assert.equal(architect.model, "inherit");
  assert.equal(architect.effort, "high");
  assert.ok(architect.description?.length);
  assert.ok(rosterNames().has("qa-engineer"));
  assert.deepEqual(cmdListAgents(), roster);
});

test("rosterAgents tolerates a missing dir, bad frontmatter and defaults", () => {
  const dir = join(tmp(), "agents");
  assert.deepEqual(rosterAgents(dir), []);
  mkdirSync(dir);
  writeFileSync(join(dir, "plain.md"), "# no frontmatter\n");
  writeFileSync(join(dir, "broken.md"), "---\nname: [\n---\n");
  writeFileSync(
    join(dir, "listed.md"),
    "---\nname: custom\ntools:\n  - Read\n  - Bash\nmodel: sonnet\n---\nbody\n",
  );
  writeFileSync(join(dir, "notes.txt"), "ignored");
  assert.deepEqual(parseFrontmatter(join(dir, "plain.md")), {});
  assert.deepEqual(parseFrontmatter(join(dir, "missing.md")), {});
  assert.deepEqual(rosterAgents(dir), [
    { name: "broken", description: null, tools: [], model: "inherit", effort: null },
    { name: "custom", description: null, tools: ["Read", "Bash"], model: "sonnet", effort: null },
    { name: "plain", description: null, tools: [], model: "inherit", effort: null },
  ]);
});

test("resolveTeam with an empty roster skips role validation", () => {
  const path = writeDef(tmp(), [
    { id: "p", type: "prompt", prompt: "y", agent: ["anyone", "else"], depends_on: [] },
  ]);
  const data = team(path, "p", { rosterDir: join(tmp(), "none") });
  assert.deepEqual(data.errors, []);
  assert.equal(data.mode, "team");
  assert.equal(first(data).model, "inherit");
});
