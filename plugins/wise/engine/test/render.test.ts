// Port of plugins/wise/tests/test_render.py against src/render.ts.

import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { render, renderStep } from "../src/render.ts";
import { PLUGIN_ROOT } from "../src/version.ts";
import { EMPTY_USAGE } from "../src/types.ts";
import type { AskStep, BashStep, State, UnitsStep } from "../src/types.ts";

function makeState(overrides: Partial<State> = {}): State {
  return {
    version: 2,
    run_id: "run-123",
    workflow: { name: "wf", version: 2, dir: "/wf" },
    cwd: "/repo",
    project: { name: "proj", path: "/repo", kind: "git" },
    status: "running",
    profile: "medium",
    answers: {},
    context: {},
    inputs: {},
    resolved: {},
    caps: {},
    usage: {
      subscription: EMPTY_USAGE(),
      "api-key": EMPTY_USAGE("api-key"),
      by_harness: {},
      by_step: {},
    },
    steps: {},
    outputs: { greeting: "hi" },
    started_at: "2026-01-01T00:00:00Z",
    last_activity_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

const bash = (run: string): BashStep => ({ id: "a", type: "bash", run });

test("test_render_step_expands_all_known_placeholders", () => {
  const step = bash(
    "{{workflow.dir}}/script.sh {{run.dir}} {{run.id}} {{project.name}} {{greeting}}",
  );
  const out = renderStep(step, makeState(), "/wf", "/run/1") as BashStep;
  assert.equal(out.run, "/wf/script.sh /run/1 run-123 proj hi");
});

test("test_render_step_output_shadows_unresolved_project_placeholder", () => {
  // No `extra` key on project, so the project pass leaves `{{project.extra}}`
  // untouched; outputs substitute last and an output literally named
  // `project.extra` shadows it. Pins the substitution order.
  const state = makeState({ outputs: { "project.extra": "shadowed-value" } });
  const out = renderStep(bash("{{project.extra}}"), state, "", "") as BashStep;
  assert.equal(out.run, "shadowed-value");
});

test("test_render_step_unresolved_placeholder_left_verbatim", () => {
  const out = renderStep(bash("{{no_such_key}}"), makeState(), "", "") as BashStep;
  assert.equal(out.run, "{{no_such_key}}");
});

test("test_render_step_recurses_into_lists_and_dicts", () => {
  const ask: AskStep = { id: "a", type: "ask", message: "m", options: ["{{run.id}}", "x"] };
  const askOut = renderStep(ask, makeState(), "", "") as AskStep;
  assert.deepEqual(askOut.options, ["run-123", "x"]);

  const units: UnitsStep = {
    id: "u",
    type: "units",
    pipeline: "ticket",
    items: "{{ticket_list}}",
    groups: { nested: "{{greeting}}" },
  };
  const unitsOut = renderStep(units, makeState(), "", "") as UnitsStep;
  assert.deepEqual(unitsOut.groups, { nested: "hi" });
  // Input is a copy, not mutated in place.
  assert.equal(units.groups["nested"], "{{greeting}}");
});

test("test_cmd_render_expands_project_run_and_outputs", () => {
  const out = render(
    "{{run.dir}}/{{run.id}}/{{project.name}}/{{greeting}}",
    makeState(),
    "",
    "/tmp/run",
  );
  assert.equal(out, "/tmp/run/run-123/proj/hi");
});

test("test_cmd_render_leaves_workflow_dir_literal", () => {
  // v1's CLI renderer did not know workflow.dir. v2 has one renderer that
  // always takes it, so the same call now resolves; `run.dir` is the one
  // placeholder left verbatim when its value is not supplied.
  assert.equal(render("{{workflow.dir}}/x", makeState(), "/wf"), "/wf/x");
  assert.equal(render("{{run.dir}}/x", makeState(), "/wf"), "{{run.dir}}/x");
});

test("test_cmd_render_output_shadows_unresolved_project_placeholder", () => {
  const state = makeState({ outputs: { "project.extra": "shadowed" } });
  assert.equal(render("{{project.extra}}", state, "", ""), "shadowed");
});

test("render resolves inputs and lets recorded outputs win over them", () => {
  const state = makeState({
    inputs: { ticket_ref: "LEC-1", greeting: "from-input" },
    outputs: { greeting: "from-output" },
  });
  assert.equal(render("{{ticket_ref}} {{greeting}}", state, ""), "LEC-1 from-output");
});

test("${CLAUDE_PLUGIN_ROOT} renders to the plugin root", () => {
  const out = render("Read ${CLAUDE_PLUGIN_ROOT}/agents/architect.md", makeState(), "/wf");
  assert.equal(out, `Read ${PLUGIN_ROOT}/agents/architect.md`);
  assert.ok(existsSync(join(PLUGIN_ROOT, "agents", "architect.md")));
});

test("render tolerates a null project", () => {
  const state = makeState({ project: null });
  assert.equal(render("{{project.name}}", state, ""), "{{project.name}}");
});

test("{{usage}} renders the run's usage views as JSON; an output named usage shadows it", () => {
  const state = makeState();
  state.usage.subscription.input = 300;
  state.usage.by_harness.claude = { ...EMPTY_USAGE(), input: 300 };
  state.usage.by_step.classify = { ...EMPTY_USAGE(), input: 300 };
  const out = render("Usage:\n{{usage}}", state, "/wf", "/run");
  const parsed = JSON.parse(out.slice("Usage:\n".length)) as {
    total: { input: number };
    by_pool: { subscription: { input: number } };
    by_harness: { claude: { input: number } };
    by_step: { classify: { input: number } };
  };
  assert.equal(parsed.total.input, 300);
  assert.equal(parsed.by_pool.subscription.input, 300);
  assert.equal(parsed.by_harness.claude.input, 300);
  assert.equal(parsed.by_step.classify.input, 300);
  const shadowed = render("{{usage}}", makeState({ outputs: { usage: "mine" } }), "/wf");
  assert.equal(shadowed, "mine");
});
