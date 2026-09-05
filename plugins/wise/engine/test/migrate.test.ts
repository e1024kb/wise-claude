// migrate.ts: the v1 -> v2 rewrite rules one by one, the four bundled v1 workflows round-tripped
// through `migrateDef` + `validateDef`, YAML rendering, and the CLI `migrate` command.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "yaml";
import { enumFromUntil, migrateDef, renderDef } from "../src/migrate.ts";
import type { MigrationNote } from "../src/migrate.ts";
import { validateDef } from "../src/defs.ts";
import { main } from "../src/cli.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "fixtures", "migrate");
const BUNDLED = join(HERE, "..", "..", "workflows");

type Doc = Record<string, unknown>;
type Step = Record<string, unknown>;

function load(path: string): Doc {
  return parse(readFileSync(path, "utf8")) as Doc;
}
function v1(extra: Doc = {}, steps: unknown[] = [{ id: "a", type: "prompt", prompt: "x" }]): Doc {
  return { version: 1, name: "t", steps, ...extra };
}
function migrate(doc: Doc): { def: Doc; notes: MigrationNote[]; steps: Step[] } {
  const r = migrateDef(doc, "t.yaml");
  const def = r.def as Doc;
  return { def, notes: r.notes, steps: (def.steps as Step[]) ?? [] };
}
function noteAt(
  notes: readonly MigrationNote[],
  path: string,
  kind?: MigrationNote["kind"],
  re?: RegExp,
): MigrationNote {
  const hit = notes.find(
    (n) =>
      n.path === path &&
      (kind === undefined || n.kind === kind) &&
      (re === undefined || re.test(n.message)),
  );
  assert.ok(hit, `no ${kind ?? ""} note at ${path} in ${JSON.stringify(notes, null, 1)}`);
  return hit;
}
function noNoteAt(notes: readonly MigrationNote[], path: string): void {
  assert.equal(
    notes.find((n) => n.path === path),
    undefined,
    `unexpected note at ${path}`,
  );
}
function errors(def: unknown) {
  return validateDef(def, "t.yaml").issues.filter((i) => i.level === "error");
}
function counts(notes: readonly MigrationNote[]): Record<MigrationNote["kind"], number> {
  const out = { rewritten: 0, warning: 0, manual: 0 };
  for (const n of notes) out[n.kind]++;
  return out;
}
function stepById(steps: readonly Step[], id: string): Step {
  const s = steps.find((x) => x.id === id);
  assert.ok(s, `no step ${id}`);
  return s;
}

const V1_STEP_KEYS = [
  "max_iterations",
  "agent",
  "payload",
  "command",
  "success",
  "cwd",
  "question",
  "header",
  "skip_label",
  "confirm_label",
  "confirm_value",
  "surface",
];

// ---- bundled v1 workflows -----------------------------------------------------------------

type Bundled = {
  name: string;
  path: string;
  counts: Record<MigrationNote["kind"], number>;
  expect: (notes: MigrationNote[], steps: Step[], def: Doc) => void;
};

const BUNDLED_V1: Bundled[] = [
  {
    name: "ticket-auto",
    path: join(BUNDLED, "ticket-auto", "workflow.yaml"),
    counts: { rewritten: 28, warning: 5, manual: 1 },
    expect(notes, steps, def) {
      noteAt(notes, "agents", "rewritten");
      noteAt(notes, "preflight.tuning", "rewritten");
      noteAt(notes, "preflight.rename_session", "rewritten");
      noteAt(notes, "tuning.groups[0].default", "rewritten", /opus \/ high/);
      noteAt(notes, "profiles.low.tuning.plan", "rewritten");
      noteAt(notes, "inputs[0].from-context", "rewritten", /ticket\[\]\.ref/);
      noteAt(notes, "inputs[1].from-context", "rewritten", /guidance/);
      noteAt(notes, "steps[1].until", "warning", /not a plain enum/);
      noteAt(notes, "steps[2].success", "warning", /stdout_matches/);
      noteAt(notes, "steps[3].until", "rewritten", /enum \[ok, blocked\]/);
      noteAt(notes, "steps[5].type", "manual", /wise_ask/);
      noteAt(notes, "steps[6].agent", "rewritten", /qa-engineer/);
      const access = stepById(steps, "ensure-access");
      assert.deepEqual(access.schema, {
        type: "object",
        properties: { access_status: { type: "string", enum: ["ok", "blocked"] } },
        required: ["access_status"],
        additionalProperties: false,
      });
      assert.deepEqual(access.outputs, ["access_status"]);
      assert.equal(access.max_turns, 1);
      assert.match(access.prompt as string, /`access_status` = one of ok \| blocked\.\n$/);
      assert.equal(stepById(steps, "process-tickets").type, "agent");
      assert.match(
        stepById(steps, "report").prompt as string,
        /^Act as the wise `qa-engineer` agent/,
      );
      assert.deepEqual(def.preflight, { "control-mode": "synchronous", worktree: "current" });
      assert.deepEqual((def.tuning as Doc).groups, [
        {
          id: "plan",
          label: "Plan phase (architect — the run's decision-maker)",
          default: { harness: "claude", model: "opus", effort: "high" },
        },
        {
          id: "implement",
          label: "Implement + fix phases (software-engineer executors)",
          default: { harness: "claude", model: "opus", effort: "high" },
        },
        {
          id: "watch",
          label: "CI watch + fix conductor",
          default: { harness: "claude", model: "sonnet" },
        },
      ]);
      assert.deepEqual((def.profiles as Doc).low, {
        tuning: {
          plan: { model: "claude-opus-4-8", effort: "high" },
          implement: { model: "sonnet" },
          watch: { model: "sonnet" },
        },
        caps: { max_fix_attempts: 3, max_review_cycles: 2 },
      });
    },
  },
  {
    name: "impl-plan-auto",
    path: join(BUNDLED, "impl-plan-auto", "workflow.yaml"),
    counts: { rewritten: 13, warning: 4, manual: 1 },
    expect(notes, steps) {
      noteAt(notes, "steps[3].type", "manual", /interactive/);
      noteAt(notes, "steps[1].until", "warning");
      noteAt(notes, "steps[3].until", "warning");
      noteAt(notes, "steps[4].until", "warning");
      noteAt(notes, "steps[2].success", "warning");
      assert.match(stepById(steps, "preflight-checks").run as string, /^set -e\n/);
      assert.equal(stepById(steps, "preflight-checks").cwd, undefined);
    },
  },
  {
    name: "ticket-plan",
    path: join(FIXTURES, "ticket-plan.v1.yaml"),
    counts: { rewritten: 74, warning: 11, manual: 8 },
    expect(notes, steps, def) {
      noteAt(
        notes,
        "tuning.groups[0].default",
        "rewritten",
        /derived .* from step "analyze-design"/,
      );
      noteAt(notes, "tuning.groups[1].default", "rewritten", /derived .* from step "gap-analysis"/);
      noteAt(notes, "tuning.groups[0].steps", "rewritten");
      noteAt(notes, "step-select.optional[3]", "manual", /covered 2 steps/);
      noteAt(notes, "step-select.presets", "warning");
      noteAt(notes, "profiles.low.step-preset", "warning");
      noteAt(notes, "profiles.low.team-mode", "rewritten");
      noteAt(notes, "inputs[1].options", "rewritten", /\^\(defaults\|ask\)\$/);
      noteAt(notes, "preflight.control-mode", "rewritten", /auto-advance.*interactive/);
      for (const i of [1, 13, 14]) noteAt(notes, `steps[${i}].type`, "manual", /interactive/);
      noteAt(notes, "steps[8].agent", "manual", /team \[software-engineer, architect\]/);
      noteAt(notes, "steps[9].agent", "manual", /led by architect/);
      noteAt(notes, "steps[7].skip_label", "manual", /gap_answers/);
      // the evidence group carries the pins the steps used to declare
      assert.deepEqual((def.tuning as Doc).groups, [
        {
          id: "evidence",
          label: "Evidence & research (design spec, deep-dive sweep, codebase audit)",
          default: { harness: "claude", model: "opus", effort: "high" },
        },
        {
          id: "authoring",
          label: "Plan authoring (gap analysis, build plan, refine)",
          default: { harness: "claude", model: "opus", effort: "xhigh" },
        },
      ]);
      const design = stepById(steps, "analyze-design");
      assert.equal(design.group, "evidence");
      assert.equal(design.model, undefined);
      assert.equal(design.effort, undefined);
      assert.equal(design.description, "Design analysis (Figma / design links)");
      assert.match(
        design.prompt as string,
        /^Act as the wise `ux-designer` agent \(see \$\{CLAUDE_PLUGIN_ROOT\}\/agents\/ux-designer\.md\)\.\n\n/,
      );
      // codebase-audit pinned sonnet but the group carries opus: dropped with a warning
      noteAt(notes, "steps[8].model", "warning", /sonnet.*opus/);
      assert.equal(stepById(steps, "codebase-audit").group, "evidence");
      assert.deepEqual(def["step-select"], {
        optional: [
          "analyze-design",
          "analyze-related",
          "research-context",
          "gap-analysis",
          "resolve-gaps",
        ],
      });
      const gaps = stepById(steps, "resolve-gaps");
      assert.equal(gaps.when, "readiness == 'gaps' && gap_mode == 'ask'");
      assert.deepEqual(gaps.options, ["Proceed on the stated defaults"]);
      assert.equal(gaps.allow_text, true);
      assert.equal(gaps.output, "gap_answers");
      assert.equal(gaps.header, undefined);
      assert.equal(typeof gaps.message, "string");
      const inputs = def.inputs as Doc[];
      assert.equal(inputs[0]?.["from-context"], "ticket[].ref");
      assert.equal(inputs[1]?.validate, "^(defaults|ask)$");
      assert.match(
        inputs[1]?.prompt as string,
        /\(defaults: Proceed on defaults \| ask: Pause and ask me\)$/,
      );
      assert.equal(inputs[3]?.validate, "^(auto|current|ask)$");
      assert.deepEqual(def.preflight, { "control-mode": "interactive" });
      assert.deepEqual((def.profiles as Doc).low, {
        tuning: {
          evidence: { model: "sonnet", effort: "medium" },
          authoring: { model: "claude-opus-4-8", effort: "high" },
        },
      });
      // ensure-access had no outputs: the enum output is named after the step
      const access = stepById(steps, "ensure-access");
      assert.deepEqual(access.outputs, ["ensure_access"]);
      assert.deepEqual((access.schema as Doc).required, ["ensure_access"]);
    },
  },
  {
    name: "example-workflow",
    path: join(FIXTURES, "example-workflow.v1.yaml"),
    counts: { rewritten: 29, warning: 1, manual: 1 },
    expect(notes, steps, def) {
      noteAt(notes, "project-selection", "rewritten", /"prompt" -> "ask"/);
      noteAt(notes, "steps[0].skill", "rewritten", /sugar/);
      noteAt(notes, "steps[0].payload", "rewritten", /empty payload/);
      noteAt(notes, "steps[1].until", "rewritten", /enum \[frontend, backend, fullstack, other\]/);
      noteAt(notes, "steps[4].until", "warning", /\^\.\+\$/);
      noteAt(notes, "steps[5].agent", "manual", /team \[architect, product-manager, qa-engineer\]/);
      noteAt(notes, "steps[6].cwd", "rewritten", /dropped cwd/);
      noteAt(notes, "steps[6].success", "rewritten", /exit code 0/);
      assert.equal(def["project-selection"], "ask");
      assert.deepEqual(stepById(steps, "list-workflows"), {
        id: "list-workflows",
        type: "agent",
        skill: "wise:wise-workflow-list",
        depends_on: [],
      });
      const classify = stepById(steps, "classify");
      assert.deepEqual(classify.schema, {
        type: "object",
        properties: {
          release_kind: { type: "string", enum: ["frontend", "backend", "fullstack", "other"] },
        },
        required: ["release_kind"],
        additionalProperties: false,
      });
      assert.deepEqual(classify.outputs, ["release_kind"]);
      assert.equal(classify.max_turns, 2);
      assert.equal(classify.model, "haiku");
      assert.equal(classify.until, undefined);
      const emoji = stepById(steps, "pick-emoji");
      assert.equal(emoji.until, "^.+$");
      assert.deepEqual(emoji.outputs, ["project_emoji"]);
      assert.match(
        stepById(steps, "team-verdict").prompt as string,
        /^Act as the wise `architect` agent leading this step and cover the `product-manager`, `qa-engineer` lenses too/,
      );
      assert.equal(stepById(steps, "echo-pwd").run, "sleep $((RANDOM % 3 + 1))\npwd\n");
    },
  },
];

for (const c of BUNDLED_V1) {
  test(`bundled v1 ${c.name} migrates to a document validateDef accepts`, () => {
    const raw = load(c.path);
    const { def, notes } = migrateDef(raw, c.path);
    const doc = def as Doc;
    const res = validateDef(def, c.path);
    assert.ok(
      res.def,
      `errors: ${JSON.stringify(
        res.issues.filter((i) => i.level === "error"),
        null,
        1,
      )}`,
    );
    assert.equal(doc.version, 2);
    const text = renderDef(def);
    assert.deepEqual(parse(text), def, "render round-trips");
    assert.equal(renderDef(def), text, "deterministic");
    assert.deepEqual(migrateDef(raw, c.path), { def, notes }, "deterministic");
    if (raw.version === 2) {
      // the tree copy was migrated in the meantime: the no-op contract is all that is left to check
      assert.equal(notes.length, 1);
      return;
    }
    const steps = doc.steps as Step[];
    for (const s of steps) {
      for (const k of V1_STEP_KEYS)
        assert.equal(k in s, false, `${String(s.id)} keeps v1 key ${k}`);
      assert.ok(
        ["agent", "bash", "approval", "ask", "units"].includes(s.type as string),
        String(s.id),
      );
    }
    for (const n of notes) assert.match(n.path, /^[a-z]/);
    assert.deepEqual(counts(notes), c.counts);
    c.expect(notes, steps, doc);
  });
}

// ---- until -> schema ------------------------------------------------------------------------------

test("enumFromUntil accepts plain enums (anchors, framing text, one group, bare alternation)", () => {
  assert.deepEqual(enumFromUntil("^(a|b|c)$"), ["a", "b", "c"]);
  assert.deepEqual(enumFromUntil("(yes|no)"), ["yes", "no"]);
  assert.deepEqual(enumFromUntil("^ACCESS: (ok|blocked)$"), ["ok", "blocked"]);
  assert.deepEqual(enumFromUntil("^(?:plan-only|now)$"), ["plan-only", "now"]);
  assert.deepEqual(enumFromUntil("^ready|gaps$"), ["ready", "gaps"]);
  assert.deepEqual(enumFromUntil("^(a|a|b)$"), ["a", "b"]);
  for (const re of [
    "^.+$",
    "^(a|b) (c|d)$",
    "count=(\\d+)",
    "^(a.b|c)$",
    "^(ok|blocked)? maybe$",
    "",
  ]) {
    assert.equal(enumFromUntil(re), undefined, re);
  }
});

test("a plain-enum until becomes a schema + outputs and the prompt asks for the structured result", () => {
  const { steps, notes } = migrate(
    v1({}, [
      {
        id: "a",
        type: "prompt",
        prompt: "Pick one.\n",
        until: "^(patch|minor|major)$",
        outputs: ["kind"],
      },
      {
        id: "b",
        type: "prompt",
        prompt: "no outputs",
        until: "^STATE: (on|off)$",
        depends_on: ["a"],
      },
    ]),
  );
  const [a, b] = steps as [Step, Step];
  assert.deepEqual(a.schema, {
    type: "object",
    properties: { kind: { type: "string", enum: ["patch", "minor", "major"] } },
    required: ["kind"],
    additionalProperties: false,
  });
  assert.deepEqual(a.outputs, ["kind"]);
  assert.equal(a.until, undefined);
  assert.equal(
    a.prompt,
    "Pick one.\n\nReturn the field directly as the structured result (no wrapping, no JSON-in-a-string): `kind` = one of patch | minor | major.\n",
  );
  noteAt(notes, "steps[0].until", "rewritten", /enum \[patch, minor, major\]/);
  // no outputs: named after the step id in snake_case, outputs added right after the schema
  assert.deepEqual(Object.keys(b), ["id", "type", "prompt", "schema", "outputs", "depends_on"]);
  assert.deepEqual(b.outputs, ["b"]);
  assert.deepEqual((b.schema as Doc).properties, { b: { type: "string", enum: ["on", "off"] } });
  assert.match(b.prompt as string, /`b` = one of on \| off\.$/);
  assert.deepEqual(errors({ version: 2, name: "t", steps }), []);
});

test("a non-enum until stays as until with a warning that carries the schema hint", () => {
  const { steps, notes } = migrate(
    v1({}, [
      {
        id: "a",
        type: "prompt",
        prompt: "x",
        until: "DONE: n=(\\d+) list=(\\S+)",
        outputs: ["n", "list"],
      },
      { id: "b", type: "prompt", prompt: "x", until: "^(ok|no)$", outputs: ["x", "y"] },
    ]),
  );
  const [a, b] = steps as [Step, Step];
  assert.equal(a.until, "DONE: n=(\\d+) list=(\\S+)");
  assert.equal(a.schema, undefined);
  assert.deepEqual(a.outputs, ["n", "list"]);
  noteAt(notes, "steps[0].until", "warning", /n: \{ type: string \}, list: \{ type: string \}/);
  // an enum group with two declared outputs cannot be mapped one-to-one: kept too
  assert.equal(b.until, "^(ok|no)$");
  noteAt(notes, "steps[1].until", "warning");
  const issues = validateDef({ version: 2, name: "t", steps }, "t.yaml").issues;
  assert.equal(issues.filter((i) => i.level === "error").length, 0);
  assert.equal(issues.filter((i) => i.path.endsWith(".until") && i.level === "warning").length, 2);
});

// ---- step types -------------------------------------------------------------------------------------

test("interactive and supervised-prompt become agent steps with manual notes", () => {
  const { steps, notes } = migrate(
    v1({}, [
      { id: "a", type: "interactive", prompt: "ask things" },
      { id: "b", type: "supervised-prompt", prompt: "long", depends_on: ["a"] },
    ]),
  );
  assert.equal(steps[0]?.type, "agent");
  assert.equal(steps[1]?.type, "agent");
  noteAt(notes, "steps[0].type", "rewritten", /"interactive" -> "agent"/);
  noteAt(notes, "steps[0].type", "manual", /wise_ask/);
  noteAt(notes, "steps[1].type", "manual", /timeout/);
  assert.deepEqual(errors({ version: 2, name: "t", steps }), []);
});

test("skill steps keep skill: sugar; a non-empty payload becomes a prompt with harness claude", () => {
  const { steps, notes } = migrate(
    v1({}, [
      { id: "a", type: "skill", skill: "/wise:wise-commit", payload: {} },
      {
        id: "b",
        type: "skill",
        skill: "wise-pr-create",
        payload: { base: "main" },
        depends_on: ["a"],
      },
      { id: "c", type: "skill", skill: "wise-report", payload: "--full", depends_on: ["b"] },
    ]),
  );
  assert.deepEqual(steps[0], { id: "a", type: "agent", skill: "wise:wise-commit" });
  assert.deepEqual(steps[1], {
    id: "b",
    type: "agent",
    prompt: 'Run /wise-pr-create with: {"base":"main"}',
    harness: "claude",
    depends_on: ["a"],
  });
  assert.equal(steps[2]?.prompt, "Run /wise-report with: --full");
  noteAt(notes, "steps[0].skill", "rewritten", /sugar/);
  noteAt(notes, "steps[0].payload", "rewritten", /empty/);
  noteAt(notes, "steps[1].skill", "rewritten", /payload -> prompt/);
  noNoteAt(notes, "steps[1].payload");
  assert.deepEqual(errors({ version: 2, name: "t", steps }), []);
});

test("bash: command -> run, success dropped, cwd folded into run when it is not the project path", () => {
  const { steps, notes } = migrate(
    v1({}, [
      {
        id: "a",
        type: "bash",
        command: "make test\n",
        cwd: "{{project.path}}",
        success: { exit_code: 0 },
        timeout: 30,
      },
      {
        id: "b",
        type: "bash",
        command: "ls\n",
        cwd: "{{project.path}}/sub",
        success: { exit_code: 0, stdout_matches: "OK" },
        depends_on: ["a"],
      },
      { id: "c", type: "bash", command: "false", success: { exit_code: 1 }, depends_on: ["b"] },
    ]),
  );
  assert.deepEqual(steps[0], { id: "a", type: "bash", run: "make test\n", timeout: 30 });
  assert.equal(steps[1]?.run, 'cd "{{project.path}}/sub" || exit 1\nls\n');
  noteAt(notes, "steps[0].command", "rewritten", /command -> run/);
  noteAt(notes, "steps[0].success", "rewritten", /exit code 0/);
  noteAt(notes, "steps[0].cwd", "rewritten", /dropped cwd/);
  noteAt(notes, "steps[1].cwd", "rewritten", /`cd` at the top/);
  noteAt(notes, "steps[1].success", "warning", /stdout_matches/);
  noteAt(notes, "steps[2].success", "warning", /exit_code/);
  assert.deepEqual(errors({ version: 2, name: "t", steps }), []);
});

test("ask: question -> message, skip / confirm labels -> options, header dropped", () => {
  const { steps, notes } = migrate(
    v1({}, [
      {
        id: "free",
        type: "ask",
        question: "Comments?",
        header: "Review",
        output: "comments",
        skip_label: "Skip",
      },
      {
        id: "binary",
        type: "ask",
        question: "Watch?",
        output: "watch",
        skip_label: "No",
        confirm_label: "Yes",
        confirm_value: "yes",
        depends_on: ["free"],
      },
    ]),
  );
  assert.deepEqual(steps[0], {
    id: "free",
    type: "ask",
    message: "Comments?",
    output: "comments",
    options: ["Skip"],
    allow_text: true,
  });
  assert.deepEqual(steps[1], {
    id: "binary",
    type: "ask",
    message: "Watch?",
    output: "watch",
    options: ["No", "Yes"],
    depends_on: ["free"],
  });
  noteAt(notes, "steps[0].question", "rewritten");
  noteAt(notes, "steps[0].header", "rewritten");
  noteAt(notes, "steps[0].skip_label", "manual", /comments to ''/);
  noteAt(notes, "steps[1].skip_label", "rewritten", /\["No","Yes"\]/);
  assert.deepEqual(errors({ version: 2, name: "t", steps }), []);
});

test("agent: role -> 'Act as' line; off / auto dropped; a team folds into its lead", () => {
  const { steps, notes } = migrate(
    v1({}, [
      { id: "a", type: "prompt", prompt: "Do it.\n", agent: "architect" },
      { id: "b", type: "prompt", prompt: "x", agent: "off", depends_on: ["a"] },
      { id: "c", type: "prompt", prompt: "x", agent: "auto", depends_on: ["a"] },
      {
        id: "d",
        type: "prompt",
        prompt: "Team.\n",
        agent: [
          { role: "product-manager" },
          { role: "wise:architect", lead: true, model: "opus" },
          "qa-engineer",
        ],
        depends_on: ["a"],
      },
      { id: "e", type: "prompt", prompt: "x", agent: ["software-engineer"], depends_on: ["a"] },
    ]),
  );
  assert.equal(
    steps[0]?.prompt,
    "Act as the wise `architect` agent (see ${CLAUDE_PLUGIN_ROOT}/agents/architect.md).\n\nDo it.\n",
  );
  assert.equal(steps[1]?.prompt, "x");
  assert.equal(steps[2]?.prompt, "x");
  assert.equal(
    steps[3]?.prompt,
    "Act as the wise `architect` agent leading this step and cover the `product-manager`, `qa-engineer` lenses too (see ${CLAUDE_PLUGIN_ROOT}/agents/architect.md, product-manager.md, qa-engineer.md).\n\nTeam.\n",
  );
  assert.match(steps[4]?.prompt as string, /^Act as the wise `software-engineer` agent \(see/);
  for (const s of steps) assert.equal("agent" in s, false);
  noteAt(notes, "steps[0].agent", "rewritten", /architect/);
  noteAt(notes, "steps[1].agent", "rewritten", /agent: off/);
  noteAt(notes, "steps[2].agent", "rewritten", /agent: auto/);
  noteAt(notes, "steps[3].agent", "manual", /led by architect/);
  noteAt(notes, "steps[4].agent", "manual", /team \[software-engineer\]/);
});

test("max_iterations <= 10 becomes max_turns, larger values are dropped with a warning", () => {
  const { steps, notes } = migrate(
    v1({}, [
      { id: "a", type: "prompt", prompt: "x", max_iterations: 3 },
      { id: "b", type: "prompt", prompt: "x", max_iterations: 20, depends_on: ["a"] },
      { id: "c", type: "prompt", prompt: "x", max_iterations: 2, max_turns: 8, depends_on: ["a"] },
    ]),
  );
  assert.equal(steps[0]?.max_turns, 3);
  assert.equal(steps[1]?.max_turns, undefined);
  assert.equal(steps[2]?.max_turns, 8);
  noteAt(notes, "steps[0].max_iterations", "rewritten", /max_turns 3/);
  noteAt(notes, "steps[1].max_iterations", "warning", /dropped max_iterations 20/);
  noteAt(notes, "steps[2].max_iterations", "warning");
  for (const s of steps) assert.equal("max_iterations" in s, false);
});

test("when: list is joined with &&; model: inherit is dropped; surface is dropped with a warning", () => {
  const { steps, notes } = migrate(
    v1({}, [
      {
        id: "a",
        type: "prompt",
        prompt: "x",
        when: ["mode == 'ask'", "x != ''"],
        model: "inherit",
        surface: { file: "p" },
      },
    ]),
  );
  assert.deepEqual(steps[0], {
    id: "a",
    type: "agent",
    prompt: "x",
    when: "mode == 'ask' && x != ''",
  });
  noteAt(notes, "steps[0].when", "rewritten", /&&/);
  noteAt(notes, "steps[0].model", "rewritten", /inherit/);
  noteAt(notes, "steps[0].surface", "warning");
});

// ---- top-level blocks --------------------------------------------------------------------------------

test("tuning: string defaults become mappings; steps: bindings become group: on the steps", () => {
  const { def, steps, notes } = migrate(
    v1(
      {
        tuning: {
          groups: [
            { id: "plan", label: "Plan", default: "opus / high" },
            { id: "watch", label: "Watch", default: "sonnet" },
            { id: "bound", label: "Bound", steps: ["a", "b"] },
            { id: "blank", steps: ["c"] },
            { id: "map", default: { model: "opus" } },
          ],
        },
      },
      [
        { id: "a", type: "prompt", prompt: "x", model: "opus", effort: "high" },
        {
          id: "b",
          type: "prompt",
          prompt: "x",
          model: "sonnet",
          effort: "high",
          depends_on: ["a"],
        },
        { id: "c", type: "prompt", prompt: "x", depends_on: ["a"] },
      ],
    ),
  );
  assert.deepEqual((def.tuning as Doc).groups, [
    { id: "plan", label: "Plan", default: { harness: "claude", model: "opus", effort: "high" } },
    { id: "watch", label: "Watch", default: { harness: "claude", model: "sonnet" } },
    { id: "bound", label: "Bound", default: { harness: "claude", model: "opus", effort: "high" } },
    { id: "blank", default: { harness: "claude" } },
    { id: "map", default: { harness: "claude", model: "opus" } },
  ]);
  assert.deepEqual(steps[0], { id: "a", type: "agent", group: "bound", prompt: "x" });
  assert.deepEqual(steps[1], {
    id: "b",
    type: "agent",
    group: "bound",
    prompt: "x",
    depends_on: ["a"],
  });
  assert.equal(steps[2]?.group, "blank");
  noteAt(notes, "tuning.groups[0].default", "rewritten");
  noteAt(notes, "tuning.groups[2].steps", "rewritten", /group: bound/);
  noteAt(notes, "tuning.groups[2].default", "rewritten", /derived/);
  noteAt(notes, "tuning.groups[3].default", "manual", /set model/);
  noteAt(notes, "tuning.groups[4].default", "rewritten", /harness: claude/);
  noteAt(notes, "steps[0].model", "rewritten", /carried by tuning group/);
  noteAt(notes, "steps[1].model", "warning", /"sonnet".*"opus"/);
  noteAt(notes, "steps[1].effort", "rewritten");
  assert.deepEqual(errors(def), []);
});

test("profiles: tuning strings become mappings, default / presets / team-mode are dropped, caps stay", () => {
  const { def, notes } = migrate(
    v1({
      tuning: { groups: [{ id: "g", default: "sonnet" }] },
      profiles: {
        low: {
          tuning: { g: "claude-opus-4-8 / high" },
          "step-preset": "minimal",
          "team-mode": "solo",
          caps: { max_fix_attempts: 3 },
        },
        medium: {},
        max: { tuning: { g: "default" }, skip: ["x"], description: "all in" },
      },
    }),
  );
  assert.deepEqual(def.profiles, {
    low: {
      tuning: { g: { model: "claude-opus-4-8", effort: "high" } },
      caps: { max_fix_attempts: 3 },
    },
    medium: {},
    max: { description: "all in" },
  });
  noteAt(notes, "profiles.low.tuning.g", "rewritten");
  noteAt(notes, "profiles.low.step-preset", "warning");
  noteAt(notes, "profiles.low.team-mode", "rewritten");
  noteAt(notes, "profiles.max.tuning.g", "rewritten", /"default"/);
  noteAt(notes, "profiles.max.skip", "warning");
  assert.deepEqual(errors(def), []);
});

test("inputs: options become validate + a prompt menu; ticket / guidance names gain from-context", () => {
  const { def, notes } = migrate(
    v1({
      inputs: [
        { name: "ticket_ids", prompt: "Which tickets?" },
        { name: "config_prompt", prompt: "Guidance?", optional: true },
        {
          name: "mode",
          prompt: "Mode?",
          default: "auto",
          options: [{ value: "auto", label: "Automatic", description: "d" }, "ask"],
        },
        { name: "kind", prompt: "Kind?", options: ["a.b", "c"], validate: "^.*$" },
        { name: "plain", prompt: "Plain?", "from-context": "guidance" },
      ],
    }),
  );
  assert.deepEqual(def.inputs, [
    { name: "ticket_ids", prompt: "Which tickets?", "from-context": "ticket[].ref" },
    { name: "config_prompt", prompt: "Guidance?", optional: true, "from-context": "guidance" },
    {
      name: "mode",
      prompt: "Mode? (auto: Automatic | ask)",
      default: "auto",
      validate: "^(auto|ask)$",
    },
    { name: "kind", prompt: "Kind? (a.b | c)", validate: "^.*$" },
    { name: "plain", prompt: "Plain?", "from-context": "guidance" },
  ]);
  noteAt(notes, "inputs[0].from-context", "rewritten", /ticket\[\]\.ref/);
  noteAt(notes, "inputs[1].from-context", "rewritten", /guidance/);
  noteAt(notes, "inputs[2].options", "rewritten", /\^\(auto\|ask\)\$/);
  noteAt(notes, "inputs[3].options", "rewritten", /\^\.\*\$/);
  noNoteAt(notes, "inputs[4].from-context");
  assert.deepEqual(errors(def), []);
});

test("step-select: entries flatten to step ids, labels become descriptions, presets are dropped", () => {
  const { def, steps, notes } = migrate(
    v1(
      {
        "step-select": {
          optional: [
            { id: "a", label: "Stage A", "ask-group": "Research" },
            { id: "gaps", label: "Gaps", steps: ["b", "c"], "ask-group": "Research" },
            "d",
          ],
          presets: [{ id: "quick", label: "Quick", skip: ["a"] }],
        },
      },
      [
        { id: "a", type: "prompt", prompt: "x" },
        { id: "b", type: "prompt", prompt: "x", description: "Own words" },
        { id: "c", type: "prompt", prompt: "x" },
        { id: "d", type: "prompt", prompt: "x" },
      ],
    ),
  );
  assert.deepEqual(def["step-select"], { optional: ["a", "b", "c", "d"] });
  assert.equal(steps[0]?.description, "Stage A");
  assert.equal(steps[1]?.description, "Own words");
  assert.equal(steps[2]?.description, "Gaps");
  assert.equal(steps[3]?.description, undefined);
  noteAt(notes, "step-select.optional[0]", "rewritten");
  noteAt(notes, "step-select.optional[1]", "manual", /covered 2 steps/);
  noteAt(notes, "step-select.presets", "warning");
  noteAt(notes, "steps[0].description", "rewritten", /Stage A/);
  assert.deepEqual(errors(def), []);
});

test("preflight: v1 control modes -> interactive, worktree prompt -> current, pins dropped, empty block omitted", () => {
  const a = migrate(
    v1({
      preflight: {
        "control-mode": "auto-advance",
        worktree: "prompt",
        rename_session: "skip",
        tuning: "prompt",
        "step-select": "prompt",
      },
    }),
  );
  assert.deepEqual(a.def.preflight, { "control-mode": "interactive", worktree: "current" });
  noteAt(a.notes, "preflight.control-mode", "rewritten", /auto-advance/);
  noteAt(a.notes, "preflight.worktree", "warning", /"prompt" -> "current"/);
  noteAt(a.notes, "preflight.rename_session", "rewritten");
  noteAt(a.notes, "preflight.tuning", "rewritten");
  noteAt(a.notes, "preflight.step-select", "rewritten");
  const b = migrate(v1({ preflight: { rename_session: "skip" } }));
  assert.equal("preflight" in b.def, false);
  const c = migrate(v1({ preflight: { "control-mode": "wave-sync", worktree: "new" } }));
  assert.deepEqual(c.def.preflight, { "control-mode": "interactive", worktree: "new" });
  assert.deepEqual(errors(a.def), []);
});

test("project-selection, agents and requires are rewritten", () => {
  const { def, notes } = migrate(
    v1({
      "project-selection": "prompt",
      agents: "auto",
      requires: [{ plugin: "wise" }, { skill: "skill-creator:skill-creator" }, { plugin: "wise" }],
    }),
  );
  assert.equal(def["project-selection"], "ask");
  assert.equal("agents" in def, false);
  assert.deepEqual(def.requires, { plugins: ["wise", "skill-creator"] });
  noteAt(notes, "project-selection", "rewritten");
  noteAt(notes, "agents", "rewritten");
  noteAt(notes, "requires", "rewritten");
  assert.equal(migrate(v1({ "project-selection": "any" })).def["project-selection"], "none");
  assert.deepEqual(errors(def), []);
});

test("unknown keys are kept and flagged, never silently dropped", () => {
  const { def, steps, notes } = migrate(
    v1(
      {
        mystery: 1,
        tuning: { groups: [{ id: "g", default: "sonnet", colour: "red" }], extra: true },
      },
      [{ id: "a", type: "prompt", prompt: "x", bogus: "y" }],
    ),
  );
  assert.equal(def.mystery, 1);
  assert.equal(steps[0]?.bogus, "y");
  assert.equal(((def.tuning as Doc).groups as Doc[])[0]?.colour, "red");
  assert.equal((def.tuning as Doc).extra, true);
  noteAt(notes, "mystery", "warning", /unknown top-level key/);
  noteAt(notes, "steps[0].bogus", "warning", /unknown step key/);
  noteAt(notes, "tuning.groups[0].colour", "warning");
  noteAt(notes, "tuning.extra", "warning");
});

test("key order follows the source with version first and group right after type", () => {
  const { def, steps } = migrate(
    v1({ author: "me", tuning: { groups: [{ id: "g", steps: ["a"] }] }, description: "d" }, [
      {
        id: "a",
        type: "prompt",
        depends_on: [],
        prompt: "x",
        model: "opus",
        until: "^(a|b)$",
        outputs: ["o"],
      },
    ]),
  );
  assert.deepEqual(Object.keys(def), [
    "version",
    "name",
    "steps",
    "author",
    "tuning",
    "description",
  ]);
  assert.deepEqual(Object.keys(steps[0] as Step), [
    "id",
    "type",
    "group",
    "depends_on",
    "prompt",
    "schema",
    "outputs",
  ]);
});

test("migrating a v2 document is a no-op with one note; a non-mapping gets one warning", () => {
  const v2 = { version: 2, name: "t", steps: [{ id: "a", type: "agent", prompt: "x" }] };
  const r = migrateDef(v2, "t.yaml");
  assert.equal(r.def, v2);
  assert.deepEqual(r.notes, [
    { path: "version", kind: "warning", message: "already version 2; nothing to migrate" },
  ]);
  const bad = migrateDef("nope", "t.yaml");
  assert.equal(bad.def, "nope");
  assert.equal(bad.notes.length, 1);
  assert.equal(bad.notes[0]?.kind, "warning");
  // a missing version is treated as v1 and gets `version: 2`
  const noVersion = migrate({ name: "t", steps: [{ id: "a", type: "prompt", prompt: "x" }] });
  assert.equal(noVersion.def.version, 2);
  noteAt(noVersion.notes, "version", "rewritten", /added/);
});

// ---- rendering ----------------------------------------------------------------------------------------

test("renderDef writes block scalars for prompts, flow lists for short sequences, and quotes YAML 1.1 words", () => {
  const def = {
    version: 2,
    name: "t",
    tuning: {
      groups: [{ id: "g", default: { harness: "claude", model: "opus", effort: "high" } }],
    },
    profiles: { low: { tuning: { g: { model: "sonnet" } } } },
    steps: [
      {
        id: "a",
        type: "agent",
        group: "g",
        prompt: "Line one\n\n  indented {{x}}\nLast\n",
        schema: {
          type: "object",
          properties: { v: { type: "string", enum: ["yes", "no"] } },
          required: ["v"],
          additionalProperties: false,
        },
        outputs: ["v"],
        until: "DONE: n=(\\d+)",
        depends_on: ["one", "two", "three", "four"],
      },
    ],
  };
  const text = renderDef(def);
  assert.deepEqual(parse(text), def);
  assert.equal(renderDef(def), text);
  assert.match(
    text,
    /^    prompt: \|\n      Line one\n\n        indented \{\{x\}\}\n      Last\n/m,
  );
  assert.match(text, /enum: \[ ?"yes", "no" ?\]/);
  assert.match(text, /required: \[ ?v ?\]/);
  assert.match(text, /outputs: \[ ?v ?\]/);
  assert.match(text, /default: \{ ?harness: claude, model: opus, effort: high ?\}/);
  assert.match(text, /g: \{ ?model: sonnet ?\}/);
  assert.match(text, /v: \{ ?type: string, enum: /);
  assert.match(text, /depends_on:\n      - one\n      - two\n      - three\n      - four\n/);
  assert.ok(!text.includes("\\\\d"), "regex is not double-escaped");
});

// ---- CLI ----------------------------------------------------------------------------------------------

async function run(argv: string[]): Promise<{ code: number; out: string; err: string }> {
  let out = "";
  let err = "";
  const code = await main(argv, { out: (s) => (out += s), err: (s) => (err += s), env: {} });
  return { code, out, err };
}

type CliResult = {
  workflow: string;
  path: string;
  already_v2: boolean;
  dry_run: boolean;
  ok: boolean;
  written: string[];
  backup: string | null;
  notes: MigrationNote[];
  issues: { level: string; path: string }[];
  yaml: string;
};

function tempCopy(name: string, source: string): { dir: string; file: string } {
  const dir = mkdtempSync(join(tmpdir(), "wise-migrate-"));
  const file = join(dir, `${name}.yaml`);
  writeFileSync(file, source);
  return { dir, file };
}

test("cli migrate dry run: JSON carries notes, issues and the rendered yaml; nothing is written", async () => {
  const src = readFileSync(join(FIXTURES, "example-workflow.v1.yaml"), "utf8");
  const { file } = tempCopy("example-workflow", src);
  const r = await run(["migrate", file]);
  assert.equal(r.code, 0, r.err);
  const j = JSON.parse(r.out) as CliResult;
  assert.equal(j.already_v2, false);
  assert.equal(j.dry_run, true);
  assert.equal(j.ok, true);
  assert.deepEqual(j.written, []);
  assert.equal(j.backup, null);
  assert.ok(j.notes.some((n) => n.kind === "manual" && /comments in the source/.test(n.message)));
  assert.equal(j.issues.filter((i) => i.level === "error").length, 0);
  assert.equal((parse(j.yaml) as Doc).version, 2);
  assert.equal(readFileSync(file, "utf8"), src, "dry run leaves the file alone");
  const t = await run(["migrate", file, "--text"]);
  assert.equal(t.code, 0);
  assert.match(
    t.out,
    /migrated to v2, \d+ rewritten, \d+ warning\(s\), \d+ manual; dry run, nothing written/,
  );
  assert.match(t.out, /result validates with no errors/);
});

test("cli migrate --out writes the v2 file elsewhere and leaves the source untouched", async () => {
  const src = readFileSync(join(FIXTURES, "ticket-plan.v1.yaml"), "utf8");
  const { dir, file } = tempCopy("ticket-plan", src);
  const out = join(dir, "nested", "ticket-plan.v2.yaml");
  const r = await run(["migrate", file, "--out", out]);
  assert.equal(r.code, 0, r.err);
  const j = JSON.parse(r.out) as CliResult;
  assert.equal(j.dry_run, false);
  assert.deepEqual(j.written, [out]);
  assert.equal(j.backup, null);
  assert.equal(readFileSync(file, "utf8"), src);
  const written = load(out);
  assert.equal(written.version, 2);
  assert.ok(validateDef(written, out).def);
  assert.equal(readFileSync(out, "utf8"), j.yaml);
});

test("cli migrate --write rewrites in place after backing up; the second run is a no-op", async () => {
  const src = readFileSync(join(BUNDLED, "ticket-auto", "workflow.yaml"), "utf8");
  const { file } = tempCopy("ticket-auto", src);
  const first = await run(["migrate", file, "--write"]);
  assert.equal(first.code, 0, first.err);
  const j = JSON.parse(first.out) as CliResult;
  const alreadyV2 = (parse(src) as Doc).version === 2;
  if (alreadyV2) {
    assert.equal(j.already_v2, true);
    assert.deepEqual(j.written, []);
    return;
  }
  assert.equal(j.backup, `${file}.v1.bak`);
  assert.deepEqual(j.written, [file]);
  assert.equal(readFileSync(`${file}.v1.bak`, "utf8"), src);
  assert.equal(readFileSync(file, "utf8"), j.yaml);
  assert.ok(validateDef(load(file), file).def);
  const second = await run(["migrate", file, "--write"]);
  assert.equal(second.code, 0);
  const k = JSON.parse(second.out) as CliResult;
  assert.equal(k.already_v2, true);
  assert.equal(k.notes.length, 1);
  assert.deepEqual(k.written, []);
  assert.equal(readFileSync(`${file}.v1.bak`, "utf8"), src, "backup untouched");
  assert.equal(readFileSync(file, "utf8"), j.yaml, "file untouched");
  const text = await run(["migrate", file, "--text"]);
  assert.match(text.out, /already v2, nothing to migrate/);
});

test("cli migrate exits 1 when the result still has validation errors, 2 for a missing file", async () => {
  const { dir, file } = tempCopy(
    "weird",
    "version: 1\nname: weird\nsteps:\n  - id: a\n    type: weird\n",
  );
  const r = await run(["migrate", file, "--out", join(dir, "weird.v2.yaml")]);
  assert.equal(r.code, 1);
  const j = JSON.parse(r.out) as CliResult;
  assert.equal(j.ok, false);
  assert.ok(j.issues.some((i) => i.level === "error" && i.path === "steps[0].type"));
  assert.ok(existsSync(join(dir, "weird.v2.yaml")), "the file is still written for inspection");
  const t = await run(["migrate", file, "--text"]);
  assert.equal(t.code, 1);
  assert.match(t.out, /validation error\(s\)/);
  assert.equal((await run(["migrate", join(dir, "missing.yaml")])).code, 2);
  assert.equal((await run(["migrate"])).code, 64);
});
