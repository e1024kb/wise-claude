// Port of the get-preflight / get-tuning / get-step-select / get-profiles / list-inputs cases
// of plugins/wise/tests/test_tuning.py (test names kept) onto the v2 shape, plus the
// buildQuestionary snapshot on the hand-migrated ticket-plan fixture and applyAnswers cases.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { loadDef, validateDef } from "../src/defs.ts";
import {
  applyAnswers,
  buildQuestionary,
  completeAnswers,
  optionalStepIds,
  resolveFromContext,
} from "../src/preflight.ts";
import { MODEL_CATALOG, catalogModel, defaultEffort, defaultModel } from "../src/models.ts";
import type { ValidationIssue, WorkflowDef } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "fixtures", "defs");
const BUNDLED = join(HERE, "..", "..", "workflows");

type Doc = Record<string, unknown>;

/** v2 counterpart of test_tuning.py `_write_def`: three steps a (agent), b (agent), c (bash). */
function doc(extra: Doc = {}, steps?: unknown[]): Doc {
  return {
    version: 2,
    name: "t",
    description: "d",
    steps: steps ?? [
      { id: "a", type: "agent", prompt: "x", model: "opus", effort: "xhigh", depends_on: [] },
      { id: "b", type: "agent", prompt: "y", depends_on: ["a"] },
      { id: "c", type: "bash", run: "true", depends_on: ["a"] },
    ],
    ...extra,
  };
}

function valid(d: Doc): WorkflowDef {
  const res = validateDef(d, "t.yaml");
  assert.ok(res.def, `expected a valid def, got issues: ${JSON.stringify(res.issues)}`);
  return res.def;
}
function issues(d: Doc): ValidationIssue[] {
  return validateDef(d, "t.yaml").issues;
}
function errorAt(list: ValidationIssue[], path: string, re?: RegExp): ValidationIssue {
  const hit = list.find(
    (i) =>
      i.level === "error" &&
      i.path === path &&
      (re === undefined || re.test(`${i.message} ${i.hint ?? ""}`)),
  );
  assert.ok(hit, `no error at ${path} in ${JSON.stringify(list)}`);
  return hit;
}
function questionIds(def: WorkflowDef): string[] {
  return buildQuestionary(def).questions.map((q) => q.id);
}

// ---- get-preflight ------------------------------------------------------------------------------

test("test_preflight_new_keys_default_skip", () => {
  // No `preflight:` block: nothing pinned, the questionary is still built.
  const def = valid(doc());
  assert.equal(def.preflight, undefined);
  assert.deepEqual(questionIds(def), []);
});

test("test_preflight_new_keys_accept_prompt", () => {
  // v2 has no opt-in pins: `tuning: prompt` / `step-select: prompt` are v1 with hints.
  const list = issues(doc({ preflight: { tuning: "prompt", "step-select": "prompt" } }));
  assert.match(errorAt(list, "preflight.tuning").hint ?? "", /drop it/);
  assert.match(errorAt(list, "preflight.step-select").hint ?? "", /drop it/);
  const def = valid(doc({ preflight: { "control-mode": "synchronous", worktree: "new" } }));
  assert.deepEqual(def.preflight, { "control-mode": "synchronous", worktree: "new" });
});

test("test_preflight_invalid_value_falls_back_to_key_default", () => {
  // v2 rejects unknown values instead of silently falling back.
  const list = issues(doc({ preflight: { "control-mode": "bogus", worktree: "bogus" } }));
  errorAt(list, "preflight.control-mode", /synchronous \| interactive/);
  errorAt(list, "preflight.worktree", /current \| new/);
});

test("test_preflight_invalid_value_warns", () => {
  // v1 values carry precise hints.
  const list = issues(doc({ preflight: { "control-mode": "auto-advance", worktree: "prompt" } }));
  assert.match(errorAt(list, "preflight.control-mode").hint ?? "", /control-mode: interactive/);
  assert.match(errorAt(list, "preflight.worktree").hint ?? "", /current.*new/);
  assert.match(
    errorAt(issues(doc({ preflight: { "control-mode": "wave-sync" } })), "preflight.control-mode")
      .hint ?? "",
    /interactive/,
  );
});

// ---- get-tuning ------------------------------------------------------------------------------------

test("test_get_tuning_empty_when_absent", () => {
  const def = valid(doc());
  assert.equal(def.tuning, undefined);
  assert.ok(!questionIds(def).some((id) => id.startsWith("tuning.")));
});

test("test_get_tuning_emits_step_defaults", () => {
  const def = valid(
    doc({
      tuning: {
        groups: [
          {
            id: "authoring",
            label: "Plan authoring",
            default: { harness: "claude", model: "opus", effort: "xhigh" },
          },
          { id: "plan", label: "Plan phase", default: { model: "opus", effort: "xhigh" } },
        ],
      },
    }),
  );
  // One harness ready (none given): the harness stage is silent, the model stage asks.
  const [g0, g1] = buildQuestionary(def).questions;
  assert.equal(g0?.id, "model.authoring");
  assert.equal(g0?.label, "Which claude model: Plan authoring?");
  assert.deepEqual(
    g0?.options?.map((o) => o.value),
    MODEL_CATALOG.claude.map((m) => m.id),
  );
  assert.equal(g0?.default, "claude-opus-5", "the `opus` alias maps to its catalog entry");
  assert.equal(g1?.id, "model.plan");
  assert.equal(g1?.default, "claude-opus-5");
});

const TUNING_INVALID: [string, Doc, string, RegExp][] = [
  // v1 `steps:` binding on a group -> hint to bind with `group:` on the steps
  [
    "tuning-unknown-step",
    { tuning: { groups: [{ id: "g", steps: ["nope"], default: {} }] } },
    "tuning.groups[0].steps",
    /group: g/,
  ],
  [
    "tuning-group-empty",
    { tuning: { groups: [{ id: "g" }] } },
    "tuning.groups[0].default",
    /needs a `default/,
  ],
  [
    "tuning-group-id",
    { tuning: { groups: [{ id: "Bad_Id", default: {} }] } },
    "tuning.groups[0].id",
    /must match/,
  ],
  [
    "tuning-group-steps-and-default",
    { tuning: { groups: [{ id: "g", steps: ["a"], default: "opus / high" }] } },
    "tuning.groups[0].default",
    /v1 string/,
  ],
  [
    "tuning-steps-not-list",
    { tuning: { groups: [{ id: "g", steps: "a", default: {} }] } },
    "tuning.groups[0].steps",
    /v1 `steps:`/,
  ],
];
for (const [marker, extra, path, re] of TUNING_INVALID) {
  test(`test_get_tuning_invalid[${marker}]`, () => {
    errorAt(issues(doc(extra)), path, re);
  });
}
test("test_get_tuning_invalid[tuning-non-prompt-step]", () => {
  // A `group:` on a bash step is accepted but has no effect: warning, not error.
  const d = doc({ tuning: { groups: [{ id: "g", default: { model: "opus" } }] } });
  (d.steps as Doc[])[2]!.group = "g";
  const list = issues(d);
  assert.ok(list.some((i) => i.level === "warning" && i.path === "steps[2].group"));
  assert.ok(!list.some((i) => i.level === "error"));
  // Unknown group on a step is an error.
  const d2 = doc({ tuning: { groups: [{ id: "g", default: {} }] } });
  (d2.steps as Doc[])[0]!.group = "nope";
  errorAt(issues(d2), "steps[0].group", /does not name a tuning group/);
});

test("test_get_tuning_duplicate_group_id", () => {
  const list = issues(
    doc({
      tuning: {
        groups: [
          { id: "g", default: {} },
          { id: "g", default: {} },
        ],
      },
    }),
  );
  errorAt(list, "tuning.groups[1].id", /duplicate tuning group/);
});

// ---- get-step-select --------------------------------------------------------------------------------

test("test_get_step_select_empty_when_absent", () => {
  const def = valid(doc());
  assert.deepEqual(optionalStepIds(def), []);
  assert.ok(!questionIds(def).includes("step-select"));
});

test("test_get_step_select_full_shape", () => {
  const def = valid(doc({ "step-select": { prompt: "Stages?", optional: ["a", "b", "c"] } }));
  const q = buildQuestionary(def).questions.find((x) => x.id === "step-select");
  assert.equal(q?.kind, "multi");
  assert.equal(q?.label, "Stages?");
  assert.deepEqual(
    q?.options?.map((o) => o.value),
    ["a", "b", "c"],
  );
  assert.deepEqual(q?.default, ["a", "b", "c"]);
  // Without `step-select:`, every `optional: true` step is offered.
  const d = doc();
  (d.steps as Doc[])[1]!.optional = true;
  (d.steps as Doc[])[1]!.description = "Step B";
  const q2 = buildQuestionary(valid(d)).questions.find((x) => x.id === "step-select");
  assert.deepEqual(q2?.options, [{ value: "b", label: "Step B", description: "b" }]);
});

const STEP_SELECT_INVALID: [string, Doc, string, RegExp][] = [
  [
    "step-select-no-steps",
    { optional: [{ id: "x" }] },
    "step-select.optional[0]",
    /v1 step-select entry/,
  ],
  ["step-select-unknown-step", { optional: ["nope"] }, "step-select.optional[0]", /unknown step/],
  ["duplicate-step-select-id", { optional: ["a", "a"] }, "step-select.optional[1]", /duplicate/],
  [
    "preset-unknown-optional",
    { optional: ["a"], presets: [{ id: "p", skip: ["nope"] }] },
    "step-select.presets",
    /v1 `presets:`/,
  ],
  [
    "step-select-steps-not-list",
    { optional: [{ id: "x", steps: "b" }] },
    "step-select.optional[0]",
    /v1 step-select entry/,
  ],
  [
    "preset-skip-not-list",
    { optional: ["a"], presets: [{ id: "p", skip: "a" }] },
    "step-select.presets",
    /v1 `presets:`/,
  ],
  [
    "duplicate-step-select-preset",
    { optional: ["a"], presets: [{ id: "p" }, { id: "p" }] },
    "step-select.presets",
    /v1 `presets:`/,
  ],
];
for (const [marker, block, path, re] of STEP_SELECT_INVALID) {
  test(`test_get_step_select_invalid[${marker}]`, () => {
    errorAt(issues(doc({ "step-select": block })), path, re);
  });
}
test("v1 step-select entry hint lists the step ids to keep", () => {
  const list = issues(
    doc({
      "step-select": {
        optional: [{ id: "pair", label: "Both", steps: ["b", "c"], "ask-group": "Flow" }],
      },
    }),
  );
  assert.match(errorAt(list, "step-select.optional[0]").hint ?? "", /optional: \[b, c\]/);
});

// ---- list-inputs ---------------------------------------------------------------------------------------

test("test_list_inputs_options_passthrough", () => {
  // Text input passes through; a v1 choice input is rejected with the derived membership regex.
  const def = valid(doc({ inputs: [{ name: "ticket_id", prompt: "Which ticket?" }] }));
  const q = buildQuestionary(def).questions.find((x) => x.id === "input.ticket_id");
  assert.deepEqual(q, { id: "input.ticket_id", kind: "text", label: "Which ticket?" });
  const list = issues(
    doc({
      inputs: [
        {
          name: "gap_mode",
          prompt: "Gap handling?",
          options: [{ value: "defaults", label: "Proceed" }, "ask"],
          default: "defaults",
        },
      ],
    }),
  );
  assert.match(errorAt(list, "inputs[0].options").hint ?? "", /\^\(defaults\|ask\)\$/);
});

test("test_list_inputs_derives_membership_validate", () => {
  const list = issues(
    doc({ inputs: [{ name: "mode", prompt: "?", options: ["plan-only", "now"] }] }),
  );
  const hint = errorAt(list, "inputs[0].options").hint ?? "";
  const derived = /validate: "([^"]+)"/.exec(hint)?.[1] ?? "";
  const re = new RegExp(derived);
  assert.ok(re.test("plan-only"));
  assert.ok(re.test("now"));
  assert.ok(!re.test("later"));
});

test("test_list_inputs_explicit_validate_wins", () => {
  const def = valid(
    doc({ inputs: [{ name: "mode", prompt: "?", validate: "^.*$", default: "a" }] }),
  );
  assert.equal(def.inputs?.[0]?.validate, "^.*$");
  assert.equal(def.inputs?.[0]?.default, "a");
});

test("test_list_inputs_scalar_options_rejected", () => {
  errorAt(
    issues(doc({ inputs: [{ name: "mode", prompt: "?", options: "fast" }] })),
    "inputs[0].options",
    /v1 choice input/,
  );
});

// ---- get-profiles ----------------------------------------------------------------------------------------

function profilesDoc(profiles: unknown): Doc {
  return doc({
    tuning: {
      groups: [
        {
          id: "authoring",
          label: "Authoring",
          default: { harness: "claude", model: "opus", effort: "high" },
        },
        { id: "plan", label: "Plan", default: { model: "opus", effort: "high" } },
      ],
    },
    "step-select": { optional: ["a"] },
    profiles,
  });
}

test("test_get_profiles_empty_when_absent", () => {
  const def = valid(doc());
  assert.equal(def.profiles, undefined);
  assert.deepEqual(buildQuestionary(def).questions, [], "no profile question in v2");
});

test("test_get_profiles_full_shape", () => {
  const def = valid(
    profilesDoc({
      low: {
        tuning: { authoring: { model: "sonnet", effort: "medium" } },
        caps: { max_review_cycles: 2 },
      },
      medium: {},
      max: { description: "everything" },
    }),
  );
  assert.deepEqual(def.profiles?.low, {
    tuning: { authoring: { model: "sonnet", effort: "medium" } },
    caps: { max_review_cycles: 2 },
  });
  assert.deepEqual(def.profiles?.medium, { tuning: {}, caps: {} });
  assert.equal(def.profiles?.max?.description, "everything");
  // `low` and `max` parse but never apply: a run takes `medium` (the declared defaults).
  const applied = applyAnswers(def, { profile: "low" });
  assert.equal(applied.profile, "medium");
  assert.deepEqual(applied.tuning.authoring, {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  assert.deepEqual(applied.caps, {});
});

test("test_get_profiles_model_only_tuning_value", () => {
  const def = valid(profilesDoc({ medium: { tuning: { authoring: { model: "sonnet" } } } }));
  assert.deepEqual(def.profiles?.medium?.tuning?.authoring, { model: "sonnet" });
  // A partial `medium` override keeps the group's other fields (P2 example) and seeds the model
  // question's default; `sonnet` caps at `medium` effort in the catalog.
  assert.equal(buildQuestionary(def).questions[0]?.default, "claude-sonnet-5");
  assert.deepEqual(applyAnswers(def, {}).tuning.authoring, {
    harness: "claude",
    model: "claude-sonnet-5",
    effort: "medium",
  });
});

const PROFILES_INVALID: [string, unknown, string, RegExp][] = [
  ["profile-level", { turbo: {} }, "profiles.turbo", /low \| medium \| max/],
  ["profile-entry-expected-mapping", { low: [] }, "profiles.low", /expected a mapping/],
  [
    "profile-tuning-unknown-group",
    { low: { tuning: { nope: { model: "sonnet" } } } },
    "profiles.low.tuning.nope",
    /unknown tuning group/,
  ],
  [
    "profile-tuning-bad-value",
    { low: { tuning: { authoring: "sonnet / high / extra" } } },
    "profiles.low.tuning.authoring",
    /v1 string/,
  ],
  [
    "profile-step-preset-unknown",
    { low: { "step-preset": "nope" } },
    "profiles.low.step-preset",
    /v1/,
  ],
  [
    "profile-step-preset-and-skip",
    { low: { "step-preset": "minimal", skip: ["a"] } },
    "profiles.low.skip",
    /v1/,
  ],
  ["profile-skip-unknown-optional", { low: { skip: ["nope"] } }, "profiles.low.skip", /v1/],
  ["profile-team-mode", { low: { "team-mode": "duo" } }, "profiles.low.team-mode", /v1/],
  [
    "profile-cap-name",
    { low: { caps: { "Bad-Name": 3 } } },
    "profiles.low.caps.Bad-Name",
    /must match/,
  ],
  [
    "profile-cap-not-positive-int[0]",
    { low: { caps: { max_fix_attempts: 0 } } },
    "profiles.low.caps.max_fix_attempts",
    /positive integer/,
  ],
  [
    "profile-cap-not-positive-int[three]",
    { low: { caps: { max_fix_attempts: "three" } } },
    "profiles.low.caps.max_fix_attempts",
    /positive integer/,
  ],
  [
    "profile-cap-not-positive-int[true]",
    { low: { caps: { max_fix_attempts: true } } },
    "profiles.low.caps.max_fix_attempts",
    /positive integer/,
  ],
];
for (const [marker, profiles, path, re] of PROFILES_INVALID) {
  test(`test_get_profiles_invalid[${marker}]`, () => {
    errorAt(issues(profilesDoc(profiles)), path, re);
  });
}
test("test_get_profiles_invalid[mapping tuning value is the v2 form]", () => {
  // Python rejected `{model: sonnet}`; in v2 it is the only accepted shape.
  const def = valid(profilesDoc({ low: { tuning: { authoring: { model: "sonnet" } } } }));
  assert.deepEqual(def.profiles?.low?.tuning?.authoring, { model: "sonnet" });
  errorAt(
    issues(profilesDoc({ low: { tuning: { authoring: "default" } } })),
    "profiles.low.tuning.authoring",
    /v1 `"default"`/,
  );
});

test("test_get_profiles_block_not_mapping", () => {
  errorAt(issues(doc({ profiles: ["low"] })), "profiles", /expected a mapping/);
});

test("test_get_profiles_tolerates_malformed_sibling_blocks", () => {
  const list = issues(
    doc({
      tuning: ["not", "a", "mapping"],
      "step-select": "nope",
      profiles: { low: { tuning: { authoring: { model: "sonnet" } } } },
    }),
  );
  errorAt(list, "tuning", /expected a mapping/);
  errorAt(list, "step-select", /expected a mapping/);
  errorAt(list, "profiles.low.tuning.authoring", /unknown tuning group/);

  const list2 = issues(doc({ tuning: "nope", profiles: { low: {} } }));
  errorAt(list2, "tuning");
  assert.ok(!list2.some((i) => i.path.startsWith("profiles")));
});

test("test_get_profiles_bare_level_and_null_fields_are_defaults", () => {
  const def = valid(profilesDoc({ medium: null, low: { tuning: null, caps: null } }));
  assert.deepEqual(def.profiles?.medium, { tuning: {}, caps: {} });
  assert.deepEqual(def.profiles?.low, { tuning: {}, caps: {} });
});

test("test_get_profiles_valid_skip_list", () => {
  // v1 `skip:` is a hint even when every id exists; v2 disables steps via the step-select answer.
  const list = issues(profilesDoc({ low: { skip: ["a"] } }));
  assert.match(errorAt(list, "profiles.low.skip").hint ?? "", /step-select/);
});

// ---- buildQuestionary --------------------------------------------------------------------------------------

function ticketPlan(): WorkflowDef {
  const path = join(BUNDLED, "ticket-plan", "workflow.yaml");
  const res = validateDef(loadDef(path), path);
  assert.deepEqual(res.issues, []);
  assert.ok(res.def);
  return res.def;
}

/**
 * The bundled ticket-plan plus the v2 constructs it does not use: a locked tuning group,
 * `medium` caps, and an optional `from-context: guidance` input. Exercises the engine paths
 * the bundled file leaves alone.
 */
function extendedTicketPlan(): WorkflowDef {
  const def = structuredClone(ticketPlan());
  def.tuning?.groups.push({
    id: "presentation",
    label: "Presentation and summaries",
    default: { harness: "claude", model: "sonnet", effort: "low" },
    locked: true,
  });
  def.profiles = { medium: { caps: { max_refine_passes: 2 } } };
  def.inputs?.push({
    name: "config_prompt",
    prompt: "Extra guidance for the run?",
    optional: true,
    "from-context": "guidance",
  });
  return def;
}

test("buildQuestionary snapshot: bundled ticket-plan", () => {
  const expected: unknown = JSON.parse(
    readFileSync(join(FIXTURES, "ticket-plan.v2.questionary.json"), "utf8"),
  );
  assert.deepEqual(JSON.parse(JSON.stringify(buildQuestionary(ticketPlan()))), expected);
});

test("buildQuestionary order: tuning group stages, step-select, inputs", () => {
  const ids = questionIds(ticketPlan());
  assert.deepEqual(ids, [
    "model.evidence",
    "model.authoring",
    "step-select",
    "input.ticket_id",
    "input.gap_mode",
    "input.review_mode",
    "input.branch_mode",
    "input.implement_mode",
  ]);
});

test("buildQuestionary: locked groups ask nothing and keep their declared value", () => {
  const def = extendedTicketPlan();
  const ids = questionIds(def);
  assert.ok(!ids.some((id) => id.endsWith(".presentation")));
  assert.deepEqual(
    applyAnswers(def, { "model.presentation": "claude-opus-5" }).tuning.presentation,
    {
      harness: "claude",
      model: "sonnet",
      effort: "low",
    },
  );
});

test("buildQuestionary: stages unlock one at a time and answered questions are not repeated", () => {
  const def = ticketPlan();
  const ready = ["claude", "codex", "grok", "gemini"] as const;
  // Stage 1: harness per group, nothing else about the group yet.
  const s1 = buildQuestionary(def, { harnesses: ready });
  assert.deepEqual(
    s1.questions.map((q) => q.id).filter((id) => !id.startsWith("input.")),
    ["harness.evidence", "harness.authoring", "step-select"],
  );
  const hq = s1.questions[0];
  assert.equal(
    hq?.label,
    "Which CLI runs: Evidence & research (design spec, deep-dive sweep, codebase audit)?",
  );
  assert.deepEqual(
    hq?.options?.map((o) => o.value),
    [...ready],
  );
  assert.equal(hq?.default, "claude");
  assert.equal(s1.defaults["harness.evidence"], "claude");
  // Stage 2: the model catalog of the harness each group picked.
  const a2 = { "harness.evidence": "codex", "harness.authoring": "claude" };
  const s2 = buildQuestionary(def, { harnesses: ready }, a2);
  const ids2 = s2.questions.map((q) => q.id);
  assert.deepEqual(
    ids2.filter((id) => !id.startsWith("input.")),
    ["model.evidence", "model.authoring", "step-select"],
  );
  const codexQ = s2.questions.find((q) => q.id === "model.evidence");
  assert.equal(
    codexQ?.label,
    "Which codex model: Evidence & research (design spec, deep-dive sweep, codebase audit)?",
  );
  assert.deepEqual(
    codexQ?.options?.map((o) => o.value),
    ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5"],
  );
  assert.equal(codexQ?.default, "gpt-6-astra", "a Claude pin means nothing to codex: first entry");
  assert.equal(s2.questions.find((q) => q.id === "model.authoring")?.default, "claude-opus-5");
  // Stage 3: the efforts of the chosen model; a one-effort model asks nothing.
  const a3 = { ...a2, "model.evidence": "gpt-5.6-luna", "model.authoring": "claude-haiku-4-5" };
  const s3 = buildQuestionary(def, { harnesses: ready }, a3);
  assert.deepEqual(
    s3.questions.map((q) => q.id).filter((id) => !id.startsWith("input.")),
    ["effort.evidence", "step-select"],
  );
  const eq = s3.questions[0];
  assert.equal(
    eq?.label,
    "Effort for GPT-5.6 Luna: Evidence & research (design spec, deep-dive sweep, codebase audit)?",
  );
  assert.deepEqual(
    eq?.options?.map((o) => o.value),
    ["low", "medium", "high"],
  );
  assert.equal(eq?.default, "high", "the group's declared effort");
  // Everything answered: only the stage-free questions remain, minus the answered ones.
  const a4 = { ...a3, "effort.evidence": "medium", "step-select": ["analyze-design"] };
  assert.deepEqual(
    buildQuestionary(def, { harnesses: ready }, a4).questions.map((q) => q.id),
    [
      "input.ticket_id",
      "input.gap_mode",
      "input.review_mode",
      "input.branch_mode",
      "input.implement_mode",
    ],
  );
});

test("buildQuestionary: a single ready harness or an unprobed context skips the harness stage", () => {
  const def = ticketPlan();
  for (const ctx of [{}, { harnesses: ["claude"] as const }, { harnesses: [] as const }] as const) {
    const ids = buildQuestionary(def, ctx).questions.map((q) => q.id);
    assert.ok(!ids.some((id) => id.startsWith("harness.")), JSON.stringify(ctx));
    assert.deepEqual(ids.slice(0, 2), ["model.evidence", "model.authoring"]);
  }
  // grok has one catalog model and no efforts: the group settles with no further question.
  const grok = buildQuestionary(
    def,
    { harnesses: ["claude", "grok"] },
    { "harness.evidence": "grok" },
  );
  assert.ok(!grok.questions.some((q) => q.id.endsWith(".evidence")));
  assert.deepEqual(applyAnswers(def, { "harness.evidence": "grok" }).tuning.evidence, {
    harness: "grok",
    model: "grok-4.6",
  });
});

test("buildQuestionary: from-context pre-fills inputs (E1)", () => {
  const context = {
    ticket: [{ ref: "LEC-772", title: "T" }, { ref: "LEC-773" }],
    guidance: "  keep it small  ",
    links: ["https://a", "https://b"],
    decisions: { db: "sqlite" },
  };
  const { questions, defaults } = buildQuestionary(extendedTicketPlan(), { context });
  assert.equal(defaults["input.ticket_id"], "LEC-772, LEC-773");
  assert.equal(defaults["input.config_prompt"], "keep it small");
  assert.equal(questions.find((q) => q.id === "input.ticket_id")?.default, "LEC-772, LEC-773");
  assert.equal(resolveFromContext("ticket[].title", context), "T");
  assert.equal(resolveFromContext("links[]", context), "https://a\nhttps://b");
  assert.equal(resolveFromContext("decisions.db", context), "sqlite");
  assert.equal(resolveFromContext("decisions.missing", context), undefined);
  assert.equal(resolveFromContext("guidance", undefined), undefined);
});

test("buildQuestionary: optional input without context defaults to empty, required has no default", () => {
  const { questions, defaults } = buildQuestionary(extendedTicketPlan());
  assert.equal(defaults["input.config_prompt"], "");
  assert.equal(questions.find((q) => q.id === "input.ticket_id")?.default, undefined);
  assert.equal("input.ticket_id" in defaults, false);
});

// ---- applyAnswers --------------------------------------------------------------------------------------------

test("applyAnswers: no answers resolves every unlocked group onto its catalog default", () => {
  const def = extendedTicketPlan();
  const base = applyAnswers(def, {});
  assert.equal(base.profile, "medium");
  assert.deepEqual(base.tuning.evidence, {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  // `xhigh` is not in Opus 5's catalog efforts: the highest listed one below it.
  assert.deepEqual(base.tuning.authoring, {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  assert.deepEqual(base.caps, { max_refine_passes: 2 }, "medium caps apply");
  // Answers from a stage the questionary would not have asked are still honoured when valid.
  const picked = applyAnswers(def, {
    "model.authoring": "claude-fable-5-1",
    "effort.authoring": "low",
    "model.evidence": "claude-sonnet-5",
  });
  assert.deepEqual(picked.tuning.authoring, {
    harness: "claude",
    model: "claude-fable-5-1",
    effort: "low",
  });
  assert.deepEqual(picked.tuning.evidence, {
    harness: "claude",
    model: "claude-sonnet-5",
    effort: "medium",
  });
  // Unknown answers fall back stage by stage: model to the default, effort to the model's default.
  const unknown = applyAnswers(def, { "model.evidence": "gpt-5.5", "effort.evidence": "ultra" });
  assert.deepEqual(unknown.tuning.evidence, {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  // The alias form of a catalog id is accepted too.
  assert.equal(
    applyAnswers(def, { "model.evidence": "haiku" }).tuning.evidence?.model,
    "claude-haiku-4-5",
  );
});

test("completeAnswers: walks every stage to its defaults; explicit answers steer it", () => {
  const def = extendedTicketPlan();
  const ready = ["claude", "codex"] as const;
  const done = completeAnswers(def, { harnesses: ready }, {});
  assert.deepEqual(
    Object.entries(done.answers).filter(([id]) => !id.startsWith("input.")),
    [
      ["harness.evidence", "claude"],
      ["harness.authoring", "claude"],
      ["step-select", ["analyze-design", "analyze-related", "research-context", "gap-analysis"]],
      ["model.evidence", "claude-opus-5"],
      ["model.authoring", "claude-opus-5"],
      ["effort.evidence", "high"],
      ["effort.authoring", "high"],
    ],
  );
  assert.deepEqual(done.missing, ["input.ticket_id"]);
  assert.ok(done.questions.some((q) => q.id === "effort.authoring"));
  const steered = completeAnswers(def, { harnesses: ready }, { "harness.evidence": "codex" });
  assert.equal(steered.answers["model.evidence"], "gpt-6-astra");
  assert.equal(steered.answers["effort.evidence"], "high");
  assert.deepEqual(applyAnswers(def, steered.answers).tuning.evidence, {
    harness: "codex",
    model: "gpt-6-astra",
    effort: "high",
  });
});

test("models: catalog helpers", () => {
  assert.equal(catalogModel("claude", "opus")?.id, "claude-opus-5");
  assert.equal(catalogModel("claude", "CLAUDE-SONNET-5")?.id, "claude-sonnet-5");
  assert.equal(catalogModel("codex", "opus"), undefined);
  assert.equal(defaultModel("codex", "opus").id, "gpt-6-astra");
  assert.equal(defaultModel("gemini").id, "gemini-3.8-flash");
  const sonnet = defaultModel("claude", "sonnet");
  assert.equal(defaultEffort(sonnet, "xhigh"), "medium");
  assert.equal(defaultEffort(sonnet, "low"), "low");
  assert.equal(defaultEffort(sonnet, undefined), "low");
  assert.equal(defaultEffort(defaultModel("claude", "haiku"), "high"), "medium");
  assert.equal(defaultEffort(defaultModel("grok"), "high"), undefined);
  for (const [harness, models] of Object.entries(MODEL_CATALOG)) {
    assert.ok(models.length > 0, harness);
    assert.equal(new Set(models.map((m) => m.id)).size, models.length, `${harness}: unique ids`);
  }
});

test("harness.<group>: asked per unlocked group when two or more harnesses are ready", () => {
  const def = extendedTicketPlan();
  const qs = buildQuestionary(def, { harnesses: ["claude", "codex", "grok"] }).questions;
  assert.deepEqual(
    qs.map((q) => q.id).filter((id) => id.startsWith("harness.")),
    ["harness.evidence", "harness.authoring"], // presentation is locked
  );
  const hq = qs.find((q) => q.id === "harness.evidence");
  assert.equal(hq?.kind, "choice");
  assert.equal(hq?.default, "claude");
  assert.deepEqual(
    hq?.options?.map((o) => [o.value, o.description]),
    [
      ["claude", "the workflow's default"],
      ["codex", "run these steps on codex"],
      ["grok", "run these steps on grok"],
    ],
  );
  // The default harness is offered even when the probe list omits it (the run probes it anyway).
  const noClaude = buildQuestionary(def, { harnesses: ["codex", "grok"] }).questions[0];
  assert.equal(noClaude?.id, "harness.evidence");
  assert.deepEqual(
    noClaude?.options?.map((o) => o.value),
    ["claude", "codex", "grok"],
  );
  assert.equal(noClaude?.default, "claude");
});

test("applyAnswers: harness.<group> swaps the harness onto its catalog, keeps the declared effort", () => {
  const def = extendedTicketPlan();
  const swapped = applyAnswers(def, { "harness.evidence": "codex" });
  assert.deepEqual(swapped.tuning.evidence, {
    harness: "codex",
    model: "gpt-6-astra",
    effort: "high",
  });
  assert.deepEqual(swapped.tuning.authoring, {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  const same = applyAnswers(def, { "harness.evidence": "claude" });
  assert.deepEqual(same.tuning.evidence, {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  const unknown = applyAnswers(def, { "harness.evidence": "bard" });
  assert.deepEqual(unknown.tuning.evidence, {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  const locked = applyAnswers(def, { "harness.presentation": "codex" });
  assert.equal(locked.tuning.presentation?.harness, "claude");
  // gemini: no effort control, the effort is dropped.
  assert.deepEqual(applyAnswers(def, { "harness.authoring": "gemini" }).tuning.authoring, {
    harness: "gemini",
    model: "gemini-3.8-flash",
  });
});

test("applyAnswers: step-select disables only deselected optional steps", () => {
  const def = ticketPlan();
  const all = applyAnswers(def, {});
  assert.equal(all.enabledSteps.size, def.steps.length);
  const some = applyAnswers(def, { "step-select": ["analyze-design"] });
  assert.ok(some.enabledSteps.has("analyze-design"));
  assert.ok(!some.enabledSteps.has("research-context"));
  assert.ok(!some.enabledSteps.has("gap-analysis"));
  assert.ok(some.enabledSteps.has("build-plan"));
  // resolve-gaps is not optional: it follows gap-analysis through its `when:`.
  assert.ok(some.enabledSteps.has("resolve-gaps"));
  const none = applyAnswers(def, { "step-select": [] });
  assert.equal(none.enabledSteps.size, def.steps.length - 4);
  const csv = applyAnswers(def, { "step-select": "analyze-design, gap-analysis" });
  assert.ok(csv.enabledSteps.has("gap-analysis") && !csv.enabledSteps.has("analyze-related"));
});

test("applyAnswers: inputs take the answer, else the declared default", () => {
  const def = ticketPlan();
  const applied = applyAnswers(def, { "input.ticket_id": "LEC-1", "input.gap_mode": "ask" });
  assert.deepEqual(applied.inputs, {
    ticket_id: "LEC-1",
    gap_mode: "ask",
    review_mode: "auto",
    branch_mode: "auto",
    implement_mode: "plan-only",
  });
});
