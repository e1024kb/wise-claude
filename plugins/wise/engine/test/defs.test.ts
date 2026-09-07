// defs.ts: locate / load / list / plugins / requires / inputs, the v2 validator per P2 field,
// and the acceptance criterion that every bundled v1 workflow fails with migration hints.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  FROM_CONTEXT_RE,
  RESERVED_NAMES,
  defaultRoots,
  installedPlugins,
  listDefs,
  listInputs,
  loadDef,
  locateDef,
  probeRequires,
  validateDef,
  validateInput,
} from "../src/defs.ts";
import type { ValidationIssue, WorkflowDef } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const BUNDLED = join(HERE, "..", "..", "workflows");

type Doc = Record<string, unknown>;

function doc(extra: Doc = {}, steps?: unknown[]): Doc {
  return {
    version: 2,
    name: "t",
    steps: steps ?? [{ id: "a", type: "agent", prompt: "x" }],
    ...extra,
  };
}
function step(extra: Doc): Doc {
  return doc({}, [{ id: "a", type: "agent", prompt: "x", ...extra }]);
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
function warningAt(list: ValidationIssue[], path: string): ValidationIssue {
  const hit = list.find((i) => i.level === "warning" && i.path === path);
  assert.ok(hit, `no warning at ${path} in ${JSON.stringify(list)}`);
  return hit;
}
function noErrors(list: ValidationIssue[]): void {
  assert.deepEqual(
    list.filter((i) => i.level === "error"),
    [],
  );
}

// ---- roots on disk --------------------------------------------------------------------------------

function roots(): { userRoot: string; bundledRoot: string; tmp: string } {
  const tmp = mkdtempSync(join(tmpdir(), "wise-defs-"));
  const userRoot = join(tmp, "user");
  const bundledRoot = join(tmp, "bundled");
  mkdirSync(userRoot);
  mkdirSync(bundledRoot);
  return { userRoot, bundledRoot, tmp };
}
function writeDef(
  root: string,
  name: string,
  form: "folder" | "flat",
  body = `version: 2\nname: ${name}\ndescription: d\nsteps: []\n`,
): string {
  const path = form === "folder" ? join(root, name, "workflow.yaml") : join(root, `${name}.yaml`);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, body);
  return path;
}

test("locateDef: user root shadows bundled, folder form beats flat, reserved and unknown are null", () => {
  const r = roots();
  const bundledFolder = writeDef(r.bundledRoot, "wf", "folder");
  assert.deepEqual(locateDef("wf", r), {
    name: "wf",
    path: bundledFolder,
    dir: join(r.bundledRoot, "wf"),
    source: "bundled",
  });
  const userFlat = writeDef(r.userRoot, "wf", "flat");
  assert.deepEqual(locateDef("wf", r), { name: "wf", path: userFlat, dir: "", source: "user" });
  const userFolder = writeDef(r.userRoot, "wf", "folder");
  assert.equal(locateDef("wf", r)?.path, userFolder);
  assert.equal(locateDef("missing", r), null);
  for (const name of RESERVED_NAMES) {
    writeDef(r.userRoot, name, "flat");
    assert.equal(locateDef(name, r), null);
  }
});

test("defaultRoots: bundled under the plugin, user under the data dir", () => {
  const d = defaultRoots({ env: { XDG_DATA_HOME: "/x" }, home: "/h" });
  assert.equal(d.userRoot, "/x/wise/workflows/definitions");
  assert.ok(d.bundledRoot.endsWith("/plugins/wise/workflows"));
  assert.equal(
    defaultRoots({ env: { CLAUDE_PLUGIN_DATA: "/c" }, home: "/h" }).userRoot,
    "/c/workflows/definitions",
  );
});

test("loadDef: empty document is {}, malformed YAML throws", () => {
  const r = roots();
  const empty = join(r.tmp, "empty.yaml");
  writeFileSync(empty, "");
  assert.deepEqual(loadDef(empty), {});
  const bad = join(r.tmp, "bad.yaml");
  writeFileSync(bad, "steps: [\n");
  assert.throws(() => loadDef(bad));
});

test("listDefs: user first, folder form before flat within a root, shadowed flag, unreadable reported", () => {
  const r = roots();
  writeDef(r.bundledRoot, "shared", "folder");
  writeDef(
    r.bundledRoot,
    "only-bundled",
    "flat",
    "version: 2\nname: renamed\ndescription: '  spaced  '\nsteps: []\n",
  );
  writeDef(r.userRoot, "shared", "flat");
  writeDef(r.userRoot, "both", "folder");
  writeDef(r.userRoot, "both", "flat");
  writeDef(r.userRoot, "broken", "flat", "steps: [\n");
  const items = listDefs(r);
  assert.deepEqual(
    items.map((i) => [i.name, i.source, i.shadowed]),
    [
      ["both", "user", false],
      ["broken", "user", false],
      ["shared", "user", false],
      ["shared", "bundled", true],
      ["renamed", "bundled", false],
    ],
  );
  assert.equal(
    items.find((i) => i.name === "both")?.path,
    join(r.userRoot, "both", "workflow.yaml"),
  );
  assert.match(items.find((i) => i.name === "broken")?.description ?? "", /^<unreadable: /);
  assert.equal(items.find((i) => i.name === "renamed")?.description, "spaced");
  assert.deepEqual(
    listDefs({ userRoot: join(r.tmp, "nope"), bundledRoot: join(r.tmp, "nope2") }),
    [],
  );
});

// ---- installed plugins / probe-requires ----------------------------------------------------------

test("installedPlugins: registry keys win, bare names, missing root is empty", () => {
  const r = roots();
  const pluginsRoot = join(r.tmp, "plugins");
  assert.deepEqual([...installedPlugins({ pluginsRoot })], []);
  mkdirSync(pluginsRoot);
  writeFileSync(
    join(pluginsRoot, "installed_plugins.json"),
    JSON.stringify({ plugins: { "wise@wise-claude": {}, bare: {} } }),
  );
  assert.deepEqual([...installedPlugins({ pluginsRoot })].toSorted(), ["bare", "wise"]);
});

test("installedPlugins: falls back to a walk that reads plugin.json names in the cache layout", () => {
  const r = roots();
  const pluginsRoot = join(r.tmp, "plugins");
  writeFileSync(join(r.tmp, "x"), "");
  mkdirSync(join(pluginsRoot, "cache", "market", "named", "1.2.3", ".claude-plugin"), {
    recursive: true,
  });
  writeFileSync(
    join(pluginsRoot, "cache", "market", "named", "1.2.3", ".claude-plugin", "plugin.json"),
    JSON.stringify({ name: "real-name" }),
  );
  mkdirSync(join(pluginsRoot, "cache", "market", "nameless", "0.1", ".claude-plugin"), {
    recursive: true,
  });
  writeFileSync(
    join(pluginsRoot, "cache", "market", "nameless", "0.1", ".claude-plugin", "plugin.json"),
    "{not json",
  );
  mkdirSync(join(pluginsRoot, "flat-plugin", ".claude-plugin"), { recursive: true });
  writeFileSync(
    join(pluginsRoot, "flat-plugin", ".claude-plugin", "plugin.json"),
    JSON.stringify({}),
  );
  writeFileSync(join(pluginsRoot, "installed_plugins.json"), "{broken");
  assert.deepEqual([...installedPlugins({ pluginsRoot })].toSorted(), [
    "flat-plugin",
    "nameless",
    "real-name",
  ]);
});

test("probeRequires: missing plugins and tools are listed", () => {
  const def = valid(doc({ requires: { plugins: ["wise", "other"], tools: ["gh", "nope-tool"] } }));
  const res = probeRequires(def, { installed: new Set(["wise"]), hasTool: (n) => n === "gh" });
  assert.deepEqual(res, { ok: false, missing: ["plugin:other", "tool:nope-tool"] });
  assert.deepEqual(probeRequires(valid(doc()), { installed: new Set() }), {
    ok: true,
    missing: [],
  });
  const r = roots();
  const pluginsRoot = join(r.tmp, "plugins");
  mkdirSync(pluginsRoot);
  writeFileSync(
    join(pluginsRoot, "installed_plugins.json"),
    JSON.stringify({ plugins: { "wise@m": {} } }),
  );
  assert.equal(
    probeRequires(valid(doc({ requires: { plugins: ["wise"] } })), { pluginsRoot }).ok,
    true,
  );
});

// ---- inputs -----------------------------------------------------------------------------------------------

test("listInputs fills the default prompt and keeps declared fields", () => {
  const def = valid(
    doc({
      inputs: [
        { name: "a" },
        {
          name: "b",
          prompt: "B?",
          optional: true,
          "from-context": "guidance",
          extract: "(x+)",
          validate: "x+",
        },
      ],
    }),
  );
  assert.deepEqual(listInputs(def), [
    { name: "a", prompt: "Value for a?" },
    {
      name: "b",
      prompt: "B?",
      optional: true,
      "from-context": "guidance",
      extract: "(x+)",
      validate: "x+",
    },
  ]);
  assert.deepEqual(listInputs(valid(doc())), []);
});

test("validateInput: extract group 1 else whole match, full-match validate, regex errors", () => {
  assert.deepEqual(validateInput("see LEC-772 now", "([A-Z]+-\\d+)"), {
    ok: true,
    value: "LEC-772",
  });
  assert.deepEqual(validateInput("see LEC-772 now", "[A-Z]+-\\d+"), { ok: true, value: "LEC-772" });
  assert.deepEqual(validateInput("nothing", "\\d+"), {
    ok: false,
    reason: "no-match",
    message: "INVALID:no-match",
  });
  assert.deepEqual(validateInput("ask", undefined, "defaults|ask"), { ok: true, value: "ask" });
  assert.deepEqual(validateInput("asking", undefined, "defaults|ask"), {
    ok: false,
    reason: "validate",
    message: "INVALID:validate",
  });
  assert.deepEqual(validateInput("raw", "", ""), { ok: true, value: "raw" });
  assert.equal(validateInput("x", "(").ok, false);
  assert.equal((validateInput("x", "(") as { reason: string }).reason, "bad-extract-regex");
  assert.equal(
    (validateInput("x", undefined, "(") as { reason: string }).reason,
    "bad-validate-regex",
  );
});

// ---- validateDef: top level --------------------------------------------------------------------------------

test("validateDef: preflight.permissions is allowlist | full", () => {
  assert.equal(valid(doc({ preflight: { permissions: "full" } })).preflight?.permissions, "full");
  assert.equal(
    valid(doc({ preflight: { permissions: "allowlist" } })).preflight?.permissions,
    "allowlist",
  );
  assert.equal(
    valid(doc({ preflight: { worktree: "current" } })).preflight?.permissions,
    undefined,
  );
  errorAt(
    issues(doc({ preflight: { permissions: "yolo" } })),
    "preflight.permissions",
    /allowlist \| full/,
  );
});

test("validateDef: non-mapping and minimal valid document", () => {
  assert.equal(validateDef("nope", "t.yaml").def, undefined);
  assert.equal(validateDef(null, "t.yaml").issues[0]?.level, "error");
  const def = valid(doc());
  assert.deepEqual(def, {
    version: 2,
    name: "t",
    steps: [{ id: "a", type: "agent", prompt: "x" }],
  });
});

test("validateDef: version missing, 1, or other carries the v2 hint", () => {
  for (const [d, re] of [
    [{ name: "t", steps: [{ id: "a", type: "agent", prompt: "x" }] }, /missing/],
    [doc({ version: 1 }), /v1 workflow/],
    [doc({ version: 3 }), /unsupported/],
    [doc({ version: "2" }), /unsupported/],
  ] as [Doc, RegExp][]) {
    assert.match(errorAt(issues(d), "version", re).hint ?? "", /version: 2/);
  }
});

test("validateDef: name and description", () => {
  errorAt(issues(doc({ name: "" })), "name");
  errorAt(issues(doc({ name: 3 })), "name");
  errorAt(issues(doc({ description: ["x"] })), "description");
  assert.equal(valid(doc({ description: "d" })).description, "d");
});

test("validateDef: unknown top-level keys warn, `author` is accepted silently, `agents` is v1", () => {
  const list = issues(doc({ author: "me", bogus: 1 }));
  noErrors(list);
  warningAt(list, "bogus");
  assert.ok(!list.some((i) => i.path === "author"));
  assert.match(errorAt(issues(doc({ agents: "auto" })), "agents").hint ?? "", /drop it/);
});

test("validateDef: project-selection values and v1 hints", () => {
  for (const v of ["current", "ask", "none"] as const)
    assert.equal(valid(doc({ "project-selection": v }))["project-selection"], v);
  assert.match(
    errorAt(issues(doc({ "project-selection": "prompt" })), "project-selection").hint ?? "",
    /ask/,
  );
  assert.match(
    errorAt(issues(doc({ "project-selection": "any" })), "project-selection").hint ?? "",
    /none/,
  );
  errorAt(issues(doc({ "project-selection": "bogus" })), "project-selection");
});

test("validateDef: requires mapping form, v1 list hint names the plugins", () => {
  assert.deepEqual(valid(doc({ requires: { plugins: ["p"], tools: ["gh"] } })).requires, {
    plugins: ["p"],
    tools: ["gh"],
  });
  const hint =
    errorAt(
      issues(
        doc({ requires: [{ plugin: "some-plugin" }, { skill: "skill-creator:skill-creator" }] }),
      ),
      "requires",
    ).hint ?? "";
  assert.match(hint, /plugins: \[some-plugin, skill-creator\]/);
  errorAt(issues(doc({ requires: { plugins: "p" } })), "requires.plugins");
  errorAt(issues(doc({ requires: "p" })), "requires");
});

// ---- validateDef: tuning / profiles field details -------------------------------------------------------------

test("validateDef: tuning group fields (harness, effort, fallback, locked, options)", () => {
  const def = valid(
    doc({
      tuning: {
        groups: [
          {
            id: "plan",
            label: "Plan",
            description: "d",
            default: { harness: "codex", model: "gpt", effort: "max" },
            fallback: ["claude", "gemini"],
            locked: false,
            options: [{ id: "cheap", label: "Cheap", description: "d", value: { model: "mini" } }],
          },
        ],
      },
    }),
  );
  assert.deepEqual(def.tuning?.groups[0], {
    id: "plan",
    label: "Plan",
    description: "d",
    default: { harness: "codex", model: "gpt", effort: "max" },
    fallback: ["claude", "gemini"],
    locked: false,
    options: [{ id: "cheap", label: "Cheap", description: "d", value: { model: "mini" } }],
  });
  const g = (d: Doc) => doc({ tuning: { groups: [{ id: "plan", default: {}, ...d }] } });
  errorAt(issues(g({ default: { harness: "openai" } })), "tuning.groups[0].default.harness");
  errorAt(issues(g({ default: { effort: "ultra" } })), "tuning.groups[0].default.effort");
  errorAt(issues(g({ default: { model: 3 } })), "tuning.groups[0].default.model");
  errorAt(issues(g({ fallback: ["nope"] })), "tuning.groups[0].fallback[0]");
  errorAt(issues(g({ fallback: "codex" })), "tuning.groups[0].fallback");
  errorAt(issues(g({ locked: "yes" })), "tuning.groups[0].locked");
  errorAt(issues(g({ options: [{ id: "default", value: {} }] })), "tuning.groups[0].options[0].id");
  errorAt(
    issues(
      g({
        options: [
          { id: "x", value: {} },
          { id: "x", value: {} },
        ],
      }),
    ),
    "tuning.groups[0].options[1].id",
  );
  errorAt(
    issues(g({ options: [{ id: "x", value: "sonnet" }] })),
    "tuning.groups[0].options[0].value",
  );
  errorAt(issues(g({ options: "x" })), "tuning.groups[0].options");
  errorAt(issues(doc({ tuning: { groups: "x" } })), "tuning.groups");
  errorAt(issues(doc({ tuning: { groups: ["x"] } })), "tuning.groups[0]");
});

test("validateDef: profiles caps and description; empty block keeps profiles undefined", () => {
  const def = valid(
    doc({ profiles: { low: { caps: { tokens: 2_000_000 }, description: "cheap" }, max: null } }),
  );
  assert.deepEqual(def.profiles, {
    low: { tuning: {}, caps: { tokens: 2_000_000 }, description: "cheap" },
    max: { tuning: {}, caps: {} },
  });
  assert.equal(valid(doc()).profiles, undefined);
  errorAt(issues(doc({ profiles: { low: { tuning: "x" } } })), "profiles.low.tuning");
  errorAt(issues(doc({ profiles: { low: { caps: [1] } } })), "profiles.low.caps");
  errorAt(issues(doc({ profiles: { low: { description: 1 } } })), "profiles.low.description");
});

// ---- validateDef: inputs / step-select ----------------------------------------------------------------------------

test("validateDef: input names, duplicates, from-context paths, regexes, defaults", () => {
  errorAt(issues(doc({ inputs: [{ name: "Bad" }] })), "inputs[0].name");
  errorAt(issues(doc({ inputs: [{ name: "a" }, { name: "a" }] })), "inputs[1].name", /duplicate/);
  errorAt(
    issues(doc({ inputs: [{ name: "a", "from-context": "ticket.ref" }] })),
    "inputs[0].from-context",
  );
  errorAt(issues(doc({ inputs: [{ name: "a", validate: "(" }] })), "inputs[0].validate");
  errorAt(issues(doc({ inputs: [{ name: "a", extract: "" }] })), "inputs[0].extract");
  errorAt(issues(doc({ inputs: [{ name: "a", default: { x: 1 } }] })), "inputs[0].default");
  errorAt(issues(doc({ inputs: "a" })), "inputs");
  errorAt(issues(doc({ inputs: ["a"] })), "inputs[0]");
  assert.equal(valid(doc({ inputs: [{ name: "a", default: 3 }] })).inputs?.[0]?.default, "3");
  for (const p of ["guidance", "ticket[].ref", "ticket[].url", "links[]", "decisions.db-choice"])
    assert.ok(FROM_CONTEXT_RE.test(p), p);
  for (const p of ["ticket", "ticket[].nope", "decisions", "links"])
    assert.ok(!FROM_CONTEXT_RE.test(p), p);
});

test("validateDef: step-select shape", () => {
  const def = valid(doc({ "step-select": { prompt: "P?", optional: ["a"] } }));
  assert.deepEqual(def["step-select"], { prompt: "P?", optional: ["a"] });
  assert.deepEqual(valid(doc({ "step-select": {} }))["step-select"], {});
  errorAt(issues(doc({ "step-select": { prompt: 1 } })), "step-select.prompt");
  errorAt(issues(doc({ "step-select": { optional: "a" } })), "step-select.optional");
  errorAt(issues(doc({ "step-select": { optional: [1] } })), "step-select.optional[0]");
});

// ---- validateDef: steps ---------------------------------------------------------------------------------------------

test("validateDef: steps list, ids, duplicates, missing id/type", () => {
  errorAt(issues(doc({ steps: [] })), "steps");
  errorAt(issues(doc({ steps: "x" })), "steps");
  errorAt(issues(doc({}, [{ type: "agent", prompt: "x" }])), "steps[0]", /missing id or type/);
  errorAt(issues(doc({}, [{ id: "a", prompt: "x" }])), "steps[0]", /missing id or type/);
  errorAt(
    issues(doc({}, [{ id: "Bad Id", type: "agent", prompt: "x" }])),
    "steps[0].id",
    /must match/,
  );
  errorAt(
    issues(
      doc({}, [
        { id: "a", type: "agent", prompt: "x" },
        { id: "a", type: "bash", run: "true" },
      ]),
    ),
    "steps[1].id",
    /duplicate/,
  );
  assert.equal(
    valid(doc({}, [{ id: "under_score-ok9", type: "bash", run: "true" }])).steps[0]?.id,
    "under_score-ok9",
  );
});

test("validateDef: unknown step type carries the v2 hint", () => {
  assert.match(
    errorAt(issues(doc({}, [{ id: "a", type: "magic" }])), "steps[0].type").hint ?? "",
    /agent \| bash \| approval \| ask \| units/,
  );
});

test("validateDef: v1 step types map to agent / skill sugar / units", () => {
  assert.match(
    errorAt(issues(doc({}, [{ id: "a", type: "prompt", prompt: "x" }])), "steps[0].type").hint ??
      "",
    /type: agent/,
  );
  const skill =
    errorAt(
      issues(doc({}, [{ id: "a", type: "skill", skill: "wise:wise-commit", payload: {} }])),
      "steps[0].type",
    ).hint ?? "";
  assert.match(skill, /skill: wise:wise-commit/);
  for (const t of ["interactive", "supervised-prompt"]) {
    assert.match(
      errorAt(issues(doc({}, [{ id: "a", type: t, prompt: "x", until: "^ok$" }])), "steps[0].type")
        .hint ?? "",
      /type: units/,
    );
  }
});

test("validateDef: v1 step keys each get a hint (until, max_iterations, agent, payload, command, success, cwd, question, header, skip_label)", () => {
  const list = issues(
    doc({}, [
      {
        id: "a",
        type: "prompt",
        prompt: "x",
        until: "^(patch|minor)$",
        max_iterations: 3,
        outputs: ["release_kind"],
        agent: "architect",
        payload: {},
        command: "true",
        success: { exit_code: 0 },
        cwd: "/x",
        question: "q",
        header: "H",
        skip_label: "skip",
      },
    ]),
  );
  for (const key of [
    "max_iterations",
    "agent",
    "payload",
    "command",
    "success",
    "cwd",
    "question",
    "header",
    "skip_label",
    "until",
  ]) {
    const hit = errorAt(list, `steps[0].${key}`);
    assert.ok(hit.hint, `hint missing for ${key}`);
  }
  assert.match(
    errorAt(list, "steps[0].until").hint ?? "",
    /schema: \{ type: object, properties: \{ release_kind: \{ type: string \} \}, required: \[release_kind\] \}/,
  );
  assert.match(errorAt(list, "steps[0].command").hint ?? "", /run:/);
  assert.match(errorAt(list, "steps[0].question").hint ?? "", /message:/);
});

test("validateDef: depends_on targets, self-dependency, trigger-rule, when", () => {
  const two = (b: Doc) =>
    doc({}, [
      { id: "a", type: "agent", prompt: "x" },
      { id: "b", type: "bash", run: "true", ...b },
    ]);
  assert.deepEqual(
    valid(two({ depends_on: ["a"], "trigger-rule": "none-failed", when: "x == 'y'" })).steps[1],
    {
      id: "b",
      type: "bash",
      run: "true",
      depends_on: ["a"],
      "trigger-rule": "none-failed",
      when: "x == 'y'",
    },
  );
  errorAt(issues(two({ depends_on: ["nope"] })), "steps[1].depends_on", /unknown step/);
  errorAt(issues(two({ depends_on: ["b"] })), "steps[1].depends_on", /depends on itself/);
  errorAt(issues(two({ depends_on: "a" })), "steps[1].depends_on");
  errorAt(issues(two({ "trigger-rule": "some-success" })), "steps[1].trigger-rule");
  errorAt(issues(two({ when: 1 })), "steps[1].when");
  assert.match(
    errorAt(issues(two({ when: ["a == 'x'", "b == 'y'"] })), "steps[1].when").hint ?? "",
    /when: "a == 'x' && b == 'y'"/,
  );
});

test("validateDef: per-step overrides", () => {
  const s = valid(
    step({
      harness: "codex",
      model: "gpt-5",
      effort: "low",
      auth: "api-key",
      fallback: ["claude"],
      mode: "full-access",
      resume: "unit",
      max_turns: 3,
      timeout: 600,
      description: "d",
      optional: true,
    }),
  ).steps[0];
  assert.deepEqual(s, {
    id: "a",
    type: "agent",
    prompt: "x",
    harness: "codex",
    model: "gpt-5",
    effort: "low",
    auth: "api-key",
    fallback: ["claude"],
    mode: "full-access",
    resume: "unit",
    max_turns: 3,
    timeout: 600,
    description: "d",
    optional: true,
  });
  const bad: [Doc, string][] = [
    [{ harness: "openai" }, "harness"],
    [{ model: "" }, "model"],
    [{ effort: "ultra" }, "effort"],
    [{ auth: "oauth" }, "auth"],
    [{ fallback: ["nope"] }, "fallback[0]"],
    [{ mode: "yolo" }, "mode"],
    [{ resume: "always" }, "resume"],
    [{ max_turns: 0 }, "max_turns"],
    [{ timeout: -1 }, "timeout"],
    [{ description: 1 }, "description"],
    [{ optional: "yes" }, "optional"],
  ];
  for (const [extra, key] of bad) errorAt(issues(step(extra)), `steps[0].${key}`);
});

test("validateDef: agent step prompt, skill sugar, schema, outputs, until deprecation", () => {
  errorAt(issues(doc({}, [{ id: "a", type: "agent" }])), "steps[0].prompt");
  errorAt(issues(doc({}, [{ id: "a", type: "agent", prompt: "  " }])), "steps[0].prompt");
  const sugar = valid(doc({}, [{ id: "a", type: "agent", skill: "/wise-commit" }])).steps[0];
  assert.deepEqual(sugar, {
    id: "a",
    type: "agent",
    prompt: "Run /wise-commit",
    skill: "wise-commit",
    harness: "claude",
  });
  assert.equal(
    valid(doc({}, [{ id: "a", type: "agent", skill: "wise-commit", harness: "claude" }])).steps[0]
      ?.harness,
    "claude",
  );
  errorAt(
    issues(doc({}, [{ id: "a", type: "agent", skill: "wise-commit", harness: "codex" }])),
    "steps[0].harness",
  );
  errorAt(
    issues(doc({}, [{ id: "a", type: "agent", skill: "wise-commit", prompt: "also" }])),
    "steps[0].prompt",
    /exclusive/,
  );
  errorAt(issues(doc({}, [{ id: "a", type: "agent", skill: "" }])), "steps[0].skill");

  const schema = { type: "object", properties: { team: { type: "string" } }, required: ["team"] };
  const withSchema = valid(step({ schema, outputs: ["team"] })).steps[0];
  assert.deepEqual(withSchema, { id: "a", type: "agent", prompt: "x", schema, outputs: ["team"] });
  errorAt(issues(step({ schema: "object" })), "steps[0].schema");
  errorAt(issues(step({ outputs: "team" })), "steps[0].outputs");
  assert.match(
    errorAt(issues(step({ outputs: ["team"] })), "steps[0].outputs", /need a `schema:`/).hint ?? "",
    /properties: \{ team/,
  );
  errorAt(
    issues(step({ schema, outputs: ["other"] })),
    "steps[0].outputs",
    /not a schema property/,
  );

  const deprecated = validateDef(step({ until: "^(a|b)$", outputs: ["kind"] }), "t.yaml");
  assert.ok(deprecated.def, JSON.stringify(deprecated.issues));
  assert.deepEqual(deprecated.def.steps[0], {
    id: "a",
    type: "agent",
    prompt: "x",
    until: "^(a|b)$",
    outputs: ["kind"],
  });
  const warn = warningAt(deprecated.issues, "steps[0].until");
  assert.match(warn.message, /deprecated/);
  assert.match(
    warn.hint ?? "",
    /schema: \{ type: object, properties: \{ kind: \{ type: string \} \}/,
  );
  errorAt(issues(step({ until: 1 })), "steps[0].until");
});

test("validateDef: bash / approval / ask steps", () => {
  assert.deepEqual(
    valid(doc({}, [{ id: "a", type: "bash", run: "make", outputs: ["o"] }])).steps[0],
    { id: "a", type: "bash", run: "make", outputs: ["o"] },
  );
  errorAt(issues(doc({}, [{ id: "a", type: "bash" }])), "steps[0].run");
  errorAt(issues(doc({}, [{ id: "a", type: "bash", run: "x", outputs: "o" }])), "steps[0].outputs");
  assert.deepEqual(valid(doc({}, [{ id: "a", type: "approval", message: "ok?" }])).steps[0], {
    id: "a",
    type: "approval",
    message: "ok?",
  });
  errorAt(issues(doc({}, [{ id: "a", type: "approval" }])), "steps[0].message");
  const ask = valid(
    doc({}, [
      {
        id: "a",
        type: "ask",
        message: "m",
        options: ["skip", "go"],
        allow_text: true,
        output: "answer",
      },
    ]),
  ).steps[0];
  assert.deepEqual(ask, {
    id: "a",
    type: "ask",
    message: "m",
    options: ["skip", "go"],
    allow_text: true,
    output: "answer",
  });
  errorAt(issues(doc({}, [{ id: "a", type: "ask" }])), "steps[0].message");
  errorAt(
    issues(doc({}, [{ id: "a", type: "ask", message: "m", options: [] }])),
    "steps[0].options",
  );
  errorAt(
    issues(doc({}, [{ id: "a", type: "ask", message: "m", allow_text: "yes" }])),
    "steps[0].allow_text",
  );
  errorAt(
    issues(doc({}, [{ id: "a", type: "ask", message: "m", output: "Bad-Name" }])),
    "steps[0].output",
  );
  warningAt(
    issues(doc({}, [{ id: "a", type: "approval", message: "m", extra: 1 }])),
    "steps[0].extra",
  );
});

test("validateDef: units step", () => {
  const tuning = {
    groups: [
      { id: "plan", default: { model: "opus" } },
      { id: "watch", default: { model: "sonnet" } },
    ],
  };
  const profiles = { low: { caps: { max_fix_attempts: 3 } } };
  const units = (extra: Doc = {}) =>
    doc({ tuning, profiles }, [
      {
        id: "process",
        type: "units",
        pipeline: "ticket",
        items: "{{ticket_ids}}",
        groups: { plan: "plan", watch: "watch" },
        ...extra,
      },
    ]);
  assert.deepEqual(
    valid(units({ caps: ["max_fix_attempts"], parallel: 2, group: "plan" })).steps[0],
    {
      id: "process",
      type: "units",
      group: "plan",
      pipeline: "ticket",
      items: "{{ticket_ids}}",
      groups: { plan: "plan", watch: "watch" },
      caps: ["max_fix_attempts"],
      parallel: 2,
    },
  );
  errorAt(issues(units({ pipeline: "epic" })), "steps[0].pipeline");
  errorAt(issues(units({ items: "" })), "steps[0].items");
  errorAt(issues(units({ groups: {} })), "steps[0].groups");
  errorAt(issues(units({ groups: { plan: "nope" } })), "steps[0].groups.plan");
  warningAt(issues(units({ groups: { plan: "plan", bogus: "plan" } })), "steps[0].groups.bogus");
  errorAt(issues(units({ caps: ["Bad"] })), "steps[0].caps");
  warningAt(issues(units({ caps: ["undeclared_cap"] })), "steps[0].caps");
  errorAt(issues(units({ caps: "x" })), "steps[0].caps");
  errorAt(issues(units({ parallel: 0 })), "steps[0].parallel");
});

// ---- fixtures and bundled workflows -------------------------------------------------------------------------------

test("every bundled workflow validates as v2 with no issues", () => {
  // M3.1 migrated ticket-plan and example-workflow; M4.3 / M4.4 ticket-auto and impl-plan-auto.
  const expectedSteps: Record<string, number> = {
    "ticket-plan": 16,
    "example-workflow": 8,
    "ticket-auto": 5,
    "impl-plan-auto": 4,
    "code-review": 9,
  };
  const all = listDefs({ userRoot: join(tmpdir(), "wise-no-user-root"), bundledRoot: BUNDLED });
  assert.deepEqual(all.map((i) => i.name).toSorted(), Object.keys(expectedSteps).toSorted());
  for (const item of all) {
    const res = validateDef(loadDef(item.path), item.path);
    assert.deepEqual(res.issues, [], item.name);
    assert.equal(res.def?.name, item.name);
    assert.equal(res.def?.steps.length, expectedSteps[item.name], item.name);
  }
});

test("the v1 fixtures fail validation with at least one migration hint", () => {
  // Snapshots of ticket-auto and impl-plan-auto as they were before M4.3 / M4.4.
  const FIXTURES = join(HERE, "fixtures", "migrate");
  const items = ["impl-plan-auto", "ticket-auto"].map((name) => ({
    name,
    path: join(FIXTURES, `${name}.v1.yaml`),
  }));
  for (const item of items) {
    const res = validateDef(loadDef(item.path), item.path);
    assert.equal(res.def, undefined, `${item.name} should not validate as v2`);
    const hints = res.issues.filter((i) => i.level === "error" && i.hint);
    assert.ok(hints.length >= 1, `${item.name} has no v1 hint`);
    assert.ok(
      hints.some((i) => i.path === "version" && /version: 2/.test(i.hint ?? "")),
      `${item.name} lacks the version hint`,
    );
    assert.ok(
      hints.some((i) => /^steps\[\d+\]\.type$/.test(i.path)),
      `${item.name} lacks a step type hint`,
    );
  }
  const byName = new Map(items.map((i) => [i.name, validateDef(loadDef(i.path), i.path).issues]));
  const hintsOf = (name: string) =>
    (byName.get(name) ?? []).map((i) => `${i.path}: ${i.hint ?? ""}`).join("\n");
  assert.match(
    hintsOf("ticket-auto"),
    /tuning\.groups\[0\]\.default: write it as a mapping: \{ harness: claude, model: opus, effort: high \}/,
  );
  assert.match(hintsOf("ticket-auto"), /steps\[5\]\.type: use `type: units`/);
  assert.match(hintsOf("impl-plan-auto"), /preflight\.rename_session: drop it/);
});

test("allowed_tools: list of non-empty strings, rejected otherwise", () => {
  const base = {
    name: "at",
    version: 2,
    steps: [{ id: "a", type: "agent", prompt: "x", allowed_tools: ["Bash(git:*)"] }],
  };
  assert.equal(validateDef(base, "t.yaml").issues.filter((i) => i.level === "error").length, 0);
  const bad = { ...base, steps: [{ ...base.steps[0], allowed_tools: ["", 3] }] };
  assert.ok(validateDef(bad, "t.yaml").issues.some((i) => i.path.endsWith("allowed_tools")));
});

test("mcp: inherit | engine-only on steps, rejected otherwise", () => {
  const base = {
    name: "mcp",
    version: 2,
    steps: [{ id: "a", type: "agent", prompt: "x", mcp: "engine-only" }],
  };
  const ok = validateDef(base, "t.yaml");
  assert.equal(ok.issues.filter((i) => i.level === "error").length, 0);
  assert.equal(ok.def?.steps[0]?.mcp, "engine-only");
  const bad = { ...base, steps: [{ ...base.steps[0], mcp: "all" }] };
  assert.ok(validateDef(bad, "t.yaml").issues.some((i) => i.path.endsWith("mcp")));
});

test("allow-api: boolean on steps and tuning groups (M6.2), rejected otherwise", () => {
  const withGroup = (d: Doc, stepExtra: Doc = {}) =>
    doc({ tuning: { groups: [{ id: "paid", default: { harness: "codex" }, ...d }] } }, [
      { id: "a", type: "agent", prompt: "x", group: "paid", auth: "api-key", ...stepExtra },
    ]);
  const def = valid(withGroup({ "allow-api": true }, { "allow-api": false }));
  assert.equal(def.tuning?.groups[0]?.["allow-api"], true);
  assert.equal(def.steps[0]?.["allow-api"], false);
  assert.equal(valid(step({})).steps[0]?.["allow-api"], undefined);
  errorAt(issues(withGroup({ "allow-api": "yes" })), "tuning.groups[0].allow-api");
  errorAt(issues(step({ "allow-api": 1 })), "steps[0].allow-api");
  noErrors(issues(withGroup({}, { "allow-api": true })));
});
