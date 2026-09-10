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
  activeGroupIds,
  applyAnswers,
  buildQuestionary,
  completeAnswers,
  enabledStepIds,
  knownInputs,
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
const AUTO_CLAUDE = { "permissions.claude": "auto" } as const;

// ---- get-preflight ------------------------------------------------------------------------------

test("test_preflight_new_keys_default_skip", () => {
  // No `preflight:` block: nothing pinned, the questionary is still built.
  const def = valid(doc());
  assert.equal(def.preflight, undefined);
  assert.deepEqual(questionIds(def), ["permissions.claude"]);
});

test("a legacy permission answer must be a string", () => {
  const def = valid(doc());
  const malformed = { permissions: ["full"] };
  assert.deepEqual(
    buildQuestionary(def, {}, malformed).questions.map((question) => question.id),
    ["permissions.claude"],
  );
  assert.deepEqual(applyAnswers(def, malformed).providerPermissions, {});
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
  const [g0, g1] = buildQuestionary(def, {}, AUTO_CLAUDE).questions;
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
  assert.deepEqual(
    buildQuestionary(def).questions.map((q) => q.id),
    ["permissions.claude"],
    "only the provider permission question is present",
  );
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
  const asked = buildQuestionary(def, {}, { "step-select": ["a"], ...AUTO_CLAUDE }).questions;
  assert.equal(asked.find((q) => q.id === "model.authoring")?.default, "claude-sonnet-5");
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

/** The bundled ticket-plan's tuning groups, one per model step, in declaration order. */
const GROUPS = [
  "analyze-design",
  "research-context",
  "codebase-audit",
  "gap-analysis",
  "build-plan",
  "refine-plan",
  "implement",
] as const;
const stageIds = (stage: string): string[] => GROUPS.map((g) => `${stage}.${g}`);

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

const INPUT_IDS = [
  "input.ticket_id",
  "input.gap_mode",
  "input.review_mode",
  "input.branch_mode",
  "input.implement_mode",
];
const ALL_STAGES = ["analyze-design", "analyze-related", "research-context", "gap-analysis"];
/** Flow modes that keep every group's step in play (defaults rule refine-plan and implement out). */
const ALL_MODES = { "input.review_mode": "ask", "input.implement_mode": "now" };
/** The groups left when the flow modes sit on their defaults. */
const DEFAULT_MODE_GROUPS = GROUPS.filter((g) => g !== "refine-plan" && g !== "implement");

test("buildQuestionary order: step-select and inputs first, tuning stages once step-select is answered", () => {
  const def = ticketPlan();
  // First call: which steps run, and the stage-free inputs. No tuning yet: the selection decides
  // which groups are worth asking about.
  assert.deepEqual(questionIds(def), ["step-select", ...INPUT_IDS]);
  // step-select answered: the first tuning stage of every active group, inputs still open. The
  // mode inputs sit on their defaults (review auto, plan-only), which rule refine-plan and
  // implement out before their groups are asked.
  const selected = buildQuestionary(
    def,
    {},
    {
      "step-select": ALL_STAGES,
      ...AUTO_CLAUDE,
    },
  ).questions;
  assert.deepEqual(
    selected.map((q) => q.id),
    [...INPUT_IDS, ...DEFAULT_MODE_GROUPS.map((g) => `model.${g}`)],
  );
  // Modes that keep those steps in play bring every group back.
  const all = buildQuestionary(
    def,
    {},
    {
      "step-select": ALL_STAGES,
      ...ALL_MODES,
      ...AUTO_CLAUDE,
    },
  ).questions;
  assert.deepEqual(
    all.map((q) => q.id).filter((id) => !id.startsWith("input.")),
    stageIds("model"),
  );
});

test("buildQuestionary: a when: gate the known inputs settle false drops the step's group", () => {
  const def = ticketPlan();
  const base = { "step-select": ALL_STAGES, ...AUTO_CLAUDE };
  const ids = (answers: Record<string, string | string[]>): string[] =>
    buildQuestionary(def, {}, answers)
      .questions.map((q) => q.id)
      .filter((id) => !id.startsWith("input."));
  // review_mode: `refine-plan` is `review_mode == 'ask' && user_comments != '' && ...`; the
  // output half stays open, so `ask` keeps the group and `auto` drops it.
  assert.ok(ids({ ...base, "input.review_mode": "ask" }).includes("model.refine-plan"));
  assert.ok(!ids({ ...base, "input.review_mode": "auto" }).includes("model.refine-plan"));
  // implement_mode: `implement` is `implement_mode != 'plan-only' && implement_choice == 'yes'`;
  // `now` and `ask` both leave the output half open and keep the group.
  assert.ok(!ids({ ...base, "input.implement_mode": "plan-only" }).includes("model.implement"));
  assert.ok(ids({ ...base, "input.implement_mode": "now" }).includes("model.implement"));
  assert.ok(ids({ ...base, "input.implement_mode": "ask" }).includes("model.implement"));
  // An unanswered input takes its declared default; the run context pre-fill counts too.
  assert.deepEqual(knownInputs(def, {}, undefined), {
    gap_mode: "defaults",
    review_mode: "auto",
    branch_mode: "auto",
    implement_mode: "plan-only",
  });
  assert.equal(knownInputs(def, {}, { ticket: [{ ref: "LEC-1" }] }).ticket_id, "LEC-1");
  // A dropped group keeps its declared value and its step is still enabled for the scheduler,
  // whose own `when:` evaluation skips it at run time.
  const applied = applyAnswers(def, base);
  assert.deepEqual(applied.tuning.implement, {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  assert.ok(applied.enabledSteps.has("implement"));
  // With no inputs known, every gate stays open and every group is active.
  const enabled = enabledStepIds(def, ALL_STAGES);
  const active = activeGroupIds(def, enabled, { inputs: {}, answers: {} });
  assert.deepEqual([...active], [...GROUPS]);
  // A gate that does not parse never blocks a group.
  const typo = structuredClone(def);
  const impl = typo.steps.find((s) => s.id === "implement");
  assert.ok(impl);
  impl.when = "implement_mode ==";
  assert.ok(
    activeGroupIds(typo, enabled, { inputs: { implement_mode: "plan-only" }, answers: {} }).has(
      "implement",
    ),
  );
});

test("buildQuestionary: a group only deselected steps bind is not asked and keeps its declared value", () => {
  const def = ticketPlan();
  const some = buildQuestionary(
    def,
    {},
    { "step-select": ["analyze-related"], ...ALL_MODES, ...AUTO_CLAUDE },
  ).questions;
  assert.deepEqual(
    some.map((q) => q.id).filter((id) => !id.startsWith("input.")),
    ["model.codebase-audit", "model.build-plan", "model.refine-plan", "model.implement"],
    "analyze-design, research-context and gap-analysis were deselected",
  );
  // Nothing selected: the same, since the four optional steps are the deselected ones.
  const none = buildQuestionary(def, {}, { "step-select": [], ...AUTO_CLAUDE }).questions;
  assert.ok(!none.some((q) => q.id.endsWith(".analyze-design")));
  const applied = applyAnswers(def, { "step-select": [] });
  assert.deepEqual(applied.tuning["analyze-design"], {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  assert.ok(!applied.enabledSteps.has("analyze-design"));
  // A workflow without optional steps asks its tuning on the first call.
  const plain = valid(
    doc({ tuning: { groups: [{ id: "g", default: { model: "opus" } }] } }, [
      { id: "a", type: "agent", prompt: "x", group: "g" },
    ]),
  );
  assert.deepEqual(
    buildQuestionary(plain, {}, AUTO_CLAUDE).questions.map((q) => q.id),
    ["model.g"],
  );
  // A group no step binds is always asked.
  const unbound = valid(doc({ tuning: { groups: [{ id: "g", default: { model: "opus" } }] } }));
  assert.deepEqual(
    buildQuestionary(unbound, {}, AUTO_CLAUDE).questions.map((q) => q.id),
    ["model.g"],
  );
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
  const ready = ["claude", "codex", "cursor", "grok", "gemini"] as const;
  // Stage 0: step-select (and the inputs), nothing about any group yet.
  const s0 = buildQuestionary(def, { harnesses: ready });
  assert.deepEqual(
    s0.questions.map((q) => q.id),
    ["step-select", ...INPUT_IDS],
  );
  // Stage 1: harness per group, nothing else about the group yet.
  const a1 = { "step-select": ALL_STAGES, ...ALL_MODES };
  const s1 = buildQuestionary(def, { harnesses: ready }, a1);
  assert.deepEqual(
    s1.questions.map((q) => q.id).filter((id) => !id.startsWith("input.")),
    stageIds("harness"),
  );
  const hq = s1.questions.find((q) => q.id === "harness.analyze-design");
  assert.equal(hq?.label, "Which CLI runs: Design spec?");
  assert.deepEqual(
    hq?.options?.map((o) => o.value),
    [...ready],
  );
  assert.equal(hq?.default, "claude");
  assert.equal(s1.defaults["harness.analyze-design"], "claude");
  // Stage 2: one permission floor for each provider selected by a group or fallback.
  const a2 = {
    ...a1,
    ...Object.fromEntries(
      GROUPS.map((g) => [`harness.${g}`, g === "analyze-design" ? "codex" : "claude"]),
    ),
  };
  const s2 = buildQuestionary(def, { harnesses: ready }, a2);
  const ids2 = s2.questions.map((q) => q.id);
  assert.deepEqual(
    ids2.filter((id) => !id.startsWith("input.")),
    ["permissions.claude", "permissions.codex"],
  );
  const permission = s2.questions.find((q) => q.id === "permissions.codex");
  assert.equal(permission?.default, "auto");
  assert.deepEqual(
    permission?.options?.map((o) => [o.value, o.label]),
    [
      ["auto", "Auto (recommended)"],
      ["approval-required", "Approval required"],
      ["full-access", "Bypass permissions"],
    ],
  );
  // Stage 3: the model catalog of the harness each group picked.
  const aPermissions = {
    ...a2,
    "permissions.codex": "auto",
    "permissions.claude": "auto",
  };
  const modelStage = buildQuestionary(def, { harnesses: ready }, aPermissions);
  const modelIds = modelStage.questions.map((q) => q.id);
  assert.deepEqual(
    modelIds.filter((id) => !id.startsWith("input.")),
    stageIds("model"),
  );
  const codexQ = modelStage.questions.find((q) => q.id === "model.analyze-design");
  assert.equal(codexQ?.label, "Which codex model: Design spec?");
  assert.deepEqual(
    codexQ?.options?.map((o) => o.value),
    ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.5"],
  );
  assert.equal(codexQ?.default, "gpt-6-astra", "a Claude pin means nothing to codex: first entry");
  assert.equal(
    modelStage.questions.find((q) => q.id === "model.build-plan")?.default,
    "claude-opus-5",
  );
  // Stage 4: the efforts of the chosen model; a one-effort model asks nothing.
  const a3 = {
    ...aPermissions,
    ...Object.fromEntries(GROUPS.map((g) => [`model.${g}`, "claude-haiku-4-5"])),
    "model.analyze-design": "gpt-5.6-luna",
  };
  const s3 = buildQuestionary(def, { harnesses: ready }, a3);
  assert.deepEqual(
    s3.questions.map((q) => q.id).filter((id) => !id.startsWith("input.")),
    ["effort.analyze-design"],
  );
  const eq = s3.questions.find((q) => q.id === "effort.analyze-design");
  assert.equal(eq?.label, "Effort for GPT-5.6 Luna: Design spec?");
  assert.deepEqual(
    eq?.options?.map((o) => o.value),
    ["low", "medium", "high"],
  );
  assert.equal(eq?.default, "high", "the group's declared effort");
  // Everything answered: only the stage-free questions remain, minus the answered ones.
  const a4 = { ...a3, "effort.analyze-design": "medium" };
  assert.deepEqual(
    buildQuestionary(def, { harnesses: ready }, a4).questions.map((q) => q.id),
    INPUT_IDS.filter((id) => !(id in ALL_MODES)),
  );
});

test("buildQuestionary: a single installed harness or an unprobed context skips the harness stage", () => {
  const def = ticketPlan();
  const a = { "step-select": ALL_STAGES, ...ALL_MODES };
  for (const ctx of [{}, { harnesses: ["claude"] as const }, { harnesses: [] as const }] as const) {
    const ids = buildQuestionary(def, ctx, { ...a, ...AUTO_CLAUDE })
      .questions.map((q) => q.id)
      .filter((id) => !id.startsWith("input."));
    assert.ok(!ids.some((id) => id.startsWith("harness.")), JSON.stringify(ctx));
    assert.deepEqual(ids, stageIds("model"));
  }
  // grok has one catalog model and no efforts: the group settles with no further question.
  const grok = buildQuestionary(
    def,
    { harnesses: ["claude", "grok"] },
    { ...a, "harness.analyze-design": "grok", "permissions.grok": "auto", ...AUTO_CLAUDE },
  );
  assert.ok(!grok.questions.some((q) => q.id.endsWith(".analyze-design")));
  assert.deepEqual(
    applyAnswers(def, { "harness.analyze-design": "grok" }).tuning["analyze-design"],
    {
      harness: "grok",
      model: "grok-4.6",
    },
  );
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
  assert.equal(questions.find((q) => q.id === "input.ticket_id")?.optional, undefined);
  assert.equal(questions.find((q) => q.id === "input.config_prompt")?.optional, true);
  assert.equal("input.ticket_id" in defaults, false);
});

// ---- applyAnswers --------------------------------------------------------------------------------------------

test("applyAnswers: no answers resolves every unlocked group onto its catalog default", () => {
  const def = extendedTicketPlan();
  const base = applyAnswers(def, {});
  assert.equal(base.profile, "medium");
  assert.deepEqual(base.tuning["analyze-design"], {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  // `xhigh` is not in Opus 5's catalog efforts: the highest listed one below it.
  assert.deepEqual(base.tuning["build-plan"], {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  assert.deepEqual(base.caps, { max_refine_passes: 2 }, "medium caps apply");
  // Answers from a stage the questionary would not have asked are still honoured when valid.
  const picked = applyAnswers(def, {
    "model.build-plan": "claude-fable-5-1",
    "effort.build-plan": "low",
    "model.analyze-design": "claude-sonnet-5",
  });
  assert.deepEqual(picked.tuning["build-plan"], {
    harness: "claude",
    model: "claude-fable-5-1",
    effort: "low",
  });
  assert.deepEqual(picked.tuning["analyze-design"], {
    harness: "claude",
    model: "claude-sonnet-5",
    effort: "medium",
  });
  // Unknown answers fall back stage by stage: model to the default, effort to the model's default.
  const unknown = applyAnswers(def, {
    "model.analyze-design": "gpt-5.5",
    "effort.analyze-design": "ultra",
  });
  assert.deepEqual(unknown.tuning["analyze-design"], {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  // The alias form of a catalog id is accepted too.
  assert.equal(
    applyAnswers(def, { "model.analyze-design": "haiku" }).tuning["analyze-design"]?.model,
    "claude-haiku-4-5",
  );
});

test("completeAnswers: walks every stage to its defaults; explicit answers steer it", () => {
  const def = extendedTicketPlan();
  const ready = ["claude", "codex"] as const;
  // No answers: the modes take their defaults, which rule refine-plan and implement out.
  const done = completeAnswers(def, { harnesses: ready }, {});
  assert.deepEqual(
    Object.entries(done.answers).filter(([id]) => !id.startsWith("input.")),
    [
      ["step-select", ALL_STAGES],
      ...DEFAULT_MODE_GROUPS.map((g) => [`harness.${g}`, "claude"]),
      ["permissions.claude", "auto"],
      ...DEFAULT_MODE_GROUPS.map((g) => [`model.${g}`, "claude-opus-5"]),
      ...DEFAULT_MODE_GROUPS.map((g) => [`effort.${g}`, "high"]),
    ],
  );
  assert.deepEqual(done.missing, ["input.ticket_id"]);
  assert.ok(done.questions.some((q) => q.id === "effort.build-plan"));
  assert.ok(!done.questions.some((q) => q.id === "model.implement"));
  // Modes that keep every step in play walk all seven groups.
  const full = completeAnswers(def, { harnesses: ready }, ALL_MODES);
  assert.deepEqual(
    GROUPS.map((g) => full.answers[`effort.${g}`]),
    GROUPS.map(() => "high"),
  );
  const steered = completeAnswers(def, { harnesses: ready }, { "harness.analyze-design": "codex" });
  assert.equal(steered.answers["model.analyze-design"], "gpt-6-astra");
  assert.equal(steered.answers["effort.analyze-design"], "high");
  assert.deepEqual(applyAnswers(def, steered.answers).tuning["analyze-design"], {
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
  assert.deepEqual(
    MODEL_CATALOG.cursor.map((model) => model.id),
    ["grok-4.6", "composer-2.5"],
  );
  assert.equal(defaultModel("cursor").id, "grok-4.6");
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

test("harness.<group>: asked per active unlocked group when two or more harnesses are installed", () => {
  const def = extendedTicketPlan();
  const a = { "step-select": ALL_STAGES, ...ALL_MODES };
  const qs = buildQuestionary(def, { harnesses: ["claude", "codex", "grok"] }, a).questions;
  assert.deepEqual(
    qs.map((q) => q.id).filter((id) => id.startsWith("harness.")),
    stageIds("harness"), // presentation is locked
  );
  const hq = qs.find((q) => q.id === "harness.analyze-design");
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
  const noClaude = buildQuestionary(def, { harnesses: ["codex", "grok"] }, a).questions.find(
    (q) => q.id === "harness.analyze-design",
  );
  assert.ok(noClaude);
  assert.deepEqual(
    noClaude.options?.map((o) => o.value),
    ["claude", "codex", "grok"],
  );
  assert.equal(noClaude.default, "claude");
  // An installed but logged-out CLI is offered, flagged with its login command.
  const flagged = buildQuestionary(
    def,
    { harnesses: ["codex", "grok"], loggedOut: ["grok"] },
    a,
  ).questions.find((q) => q.id === "harness.analyze-design");
  assert.deepEqual(
    flagged?.options?.map((o) => o.description),
    [
      "the workflow's default",
      "run these steps on codex",
      "run these steps on grok; not logged in, run `grok login` first",
    ],
  );
});

test("applyAnswers: harness.<group> swaps the harness onto its catalog, keeps the declared effort", () => {
  const def = extendedTicketPlan();
  const swapped = applyAnswers(def, { "harness.analyze-design": "codex" });
  assert.deepEqual(swapped.tuning["analyze-design"], {
    harness: "codex",
    model: "gpt-6-astra",
    effort: "high",
  });
  assert.deepEqual(swapped.tuning["build-plan"], {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  const same = applyAnswers(def, { "harness.analyze-design": "claude" });
  assert.deepEqual(same.tuning["analyze-design"], {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  const unknown = applyAnswers(def, { "harness.analyze-design": "bard" });
  assert.deepEqual(unknown.tuning["analyze-design"], {
    harness: "claude",
    model: "claude-opus-5",
    effort: "high",
  });
  const locked = applyAnswers(def, { "harness.presentation": "codex" });
  assert.equal(locked.tuning.presentation?.harness, "claude");
  // gemini: no effort control, the effort is dropped.
  assert.deepEqual(applyAnswers(def, { "harness.build-plan": "gemini" }).tuning["build-plan"], {
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
