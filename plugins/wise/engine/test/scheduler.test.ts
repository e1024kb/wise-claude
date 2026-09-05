import { test } from "node:test";
import assert from "node:assert/strict";
import type { State, Step, StepStatus, WorkflowDef } from "../src/types.ts";
import { TRIGGER_RULES } from "../src/types.ts";
import {
  evaluateWhen,
  nextWave,
  stepById,
  triggerRuleSatisfied,
  type TriggerVerdict,
} from "../src/scheduler.ts";

// ---- helpers ------------------------------------------------------------------

function deps(...statuses: StepStatus[]): { status: StepStatus }[] {
  return statuses.map((status) => ({ status }));
}

function verdict(runnable: boolean, skip: boolean): TriggerVerdict {
  return { runnable, skip };
}

function makeDef(steps: Step[]): WorkflowDef {
  return { version: 2, name: "t", steps };
}

function makeState(
  steps: Record<string, StepStatus>,
  outputs: Record<string, unknown> = {},
  extra: Partial<Pick<State, "inputs" | "answers">> = {},
): State {
  const zero = { input: 0, output: 0, cache_read: 0, cache_write: 0 };
  return {
    version: 2,
    run_id: "run-1",
    workflow: { name: "t", version: 2, dir: "" },
    cwd: "/tmp/t",
    project: null,
    status: "running",
    profile: "medium",
    answers: extra.answers ?? {},
    context: {},
    inputs: extra.inputs ?? {},
    resolved: {},
    caps: {},
    usage: {
      subscription: { ...zero, pool: "subscription" },
      "api-key": { ...zero, pool: "api-key" },
      by_harness: {},
    },
    steps: Object.fromEntries(
      Object.entries(steps).map(([id, status]) => [id, { status, attempts: 0 }]),
    ),
    outputs,
    started_at: "2026-01-01T00:00:00Z",
    last_activity_at: "2026-01-01T00:00:00Z",
  };
}

function bash(id: string, when?: string | string[]): Step {
  const step: Step = { id, type: "bash", run: "true" };
  // A v1 list `when:` is accepted at runtime even though the v2 type says string.
  if (when !== undefined) (step as { when?: unknown }).when = when;
  return step;
}

function readyIds(def: WorkflowDef, state: State): string[] {
  return nextWave(def, state).ready.map((s) => s.id);
}

// ---- triggerRuleSatisfied truth table ---------------------------------------------

const TRUTH_TABLE: [string, StepStatus[], TriggerVerdict][] = [
  // all-success: runnable only when every dep completed.
  ["all-success", ["completed", "completed"], verdict(true, false)],
  ["all-success", ["completed", "failed"], verdict(false, true)],
  ["all-success", ["completed", "skipped"], verdict(false, true)],
  ["all-success", ["completed", "cancelled"], verdict(false, true)],
  ["all-success", ["completed", "running"], verdict(false, false)],
  // one-success: runnable as soon as any dep completed.
  ["one-success", ["completed", "pending"], verdict(true, false)],
  ["one-success", ["failed", "skipped"], verdict(false, true)],
  ["one-success", ["failed", "running"], verdict(false, false)],
  // all-done: runnable once every dep reached ANY terminal state.
  ["all-done", ["completed", "failed"], verdict(true, false)],
  ["all-done", ["completed", "running"], verdict(false, false)],
  // none-failed-min-one-success: needs all terminal, zero failed, >=1 success.
  ["none-failed-min-one-success", ["completed", "skipped"], verdict(true, false)],
  ["none-failed-min-one-success", ["completed", "failed"], verdict(false, true)],
  ["none-failed-min-one-success", ["skipped", "cancelled"], verdict(false, false)],
  ["none-failed-min-one-success", ["completed", "running"], verdict(false, false)],
  // none-failed: all terminal + zero failed; runnable even when ALL deps are skipped.
  ["none-failed", ["completed", "completed"], verdict(true, false)],
  ["none-failed", ["completed", "skipped"], verdict(true, false)],
  ["none-failed", ["skipped", "skipped"], verdict(true, false)],
  ["none-failed", ["skipped", "cancelled"], verdict(true, false)],
  ["none-failed", ["completed", "failed"], verdict(false, true)],
  ["none-failed", ["failed", "running"], verdict(false, true)],
  ["none-failed", ["completed", "running"], verdict(false, false)],
  ["none-failed", ["completed", "pending"], verdict(false, false)],
];

for (const [rule, statuses, expected] of TRUTH_TABLE) {
  test(`test_trigger_rule_truth_table[${rule}-${statuses.join(",")}]`, () => {
    assert.deepEqual(triggerRuleSatisfied(rule, deps(...statuses)), expected);
  });
}

test("test_trigger_rules_registry_names_every_rule", () => {
  assert.deepEqual(
    new Set(TRIGGER_RULES),
    new Set([
      "all-success",
      "one-success",
      "all-done",
      "none-failed",
      "none-failed-min-one-success",
    ]),
  );
});

test("test_trigger_rule_empty_deps_always_runnable", () => {
  assert.deepEqual(triggerRuleSatisfied("all-success", []), verdict(true, false));
  assert.deepEqual(triggerRuleSatisfied("one-success", []), verdict(true, false));
});

test("test_trigger_rule_unknown_delegates_to_all_success", () => {
  const ok = deps("completed", "completed");
  const bad = deps("completed", "failed");
  assert.deepEqual(triggerRuleSatisfied("bogus-rule", ok), triggerRuleSatisfied("all-success", ok));
  assert.deepEqual(triggerRuleSatisfied("bogus-rule", ok), verdict(true, false));
  assert.deepEqual(
    triggerRuleSatisfied("bogus-rule", bad),
    triggerRuleSatisfied("all-success", bad),
  );
  assert.deepEqual(triggerRuleSatisfied("bogus-rule", bad), verdict(false, true));
});

test("stepById returns the first match or undefined", () => {
  const steps = [bash("a"), bash("b")];
  assert.equal(stepById(steps, "b")?.id, "b");
  assert.equal(stepById(steps, "zzz"), undefined);
});

// ---- nextWave ----------------------------------------------------------------

test("test_next_wave_unreachable_pending_is_terminal_failed", () => {
  // "orphan" is in the state but no longer in the definition: stuck pending forever.
  const def = makeDef([bash("a")]);
  const state = makeState({ a: "completed", orphan: "pending" });
  const wave = nextWave(def, state);
  assert.deepEqual(wave.ready, []);
  assert.deepEqual(wave.skipped, []);
  assert.equal(wave.done, true);
  assert.equal(wave.failed, true);
});

test("test_next_wave_all_terminal_is_completed", () => {
  const wave = nextWave(makeDef([bash("a")]), makeState({ a: "completed" }));
  assert.deepEqual(wave.ready, []);
  assert.equal(wave.done, true);
  assert.equal(wave.failed, false);
});

const WHEN_CASES: [string, string, boolean][] = [
  ["mode == 'fast'", "fast", true],
  ["mode == 'fast'", "slow", false],
  ["mode != 'fast'", "slow", true],
  ["mode != 'fast'", "fast", false],
  // an unparseable `when:` expression is treated TRUE (and surfaced as a warning).
  ["this is not a valid expr @@@", "anything", true],
  // v2 deviation: `&&` is now a real operator, so this evaluates to false instead of
  // falling through to the unparseable-truthy path as in workflows.py.
  ["mode == 'fast' && other == 'x'", "slow", false],
];

for (const [expr, value, shouldRun] of WHEN_CASES) {
  test(`test_next_wave_when_semantics[${expr}-${value}]`, () => {
    const wave = nextWave(makeDef([bash("a", expr)]), makeState({ a: "pending" }, { mode: value }));
    const ran = wave.ready.map((s) => s.id);
    if (shouldRun) {
      assert.deepEqual(ran, ["a"]);
      assert.deepEqual(wave.skipped, []);
    } else {
      assert.deepEqual(ran, []);
      assert.deepEqual(
        wave.skipped.map((s) => s.id),
        ["a"],
      );
    }
  });
}

test("nextWave surfaces an unparseable when: as a warning", () => {
  const wave = nextWave(
    makeDef([bash("a", "this is not a valid expr @@@")]),
    makeState({ a: "pending" }),
  );
  assert.equal(wave.warnings.length, 1);
  assert.match(wave.warnings[0] as string, /^when-unparseable:a:/);
});

const WHEN_LIST_CASES: [string[], Record<string, unknown>, boolean][] = [
  // AND semantics: all conditions must hold.
  [["readiness == 'gaps'", "gap_mode == 'ask'"], { readiness: "gaps", gap_mode: "ask" }, true],
  [
    ["readiness == 'gaps'", "gap_mode == 'ask'"],
    { readiness: "gaps", gap_mode: "defaults" },
    false,
  ],
  [["readiness == 'gaps'", "gap_mode == 'ask'"], { readiness: "ready", gap_mode: "ask" }, false],
  // first condition guards the second's unset-var != '' trap.
  [["review_mode == 'ask'", "user_comments != ''"], { review_mode: "auto" }, false],
  [
    ["review_mode == 'ask'", "user_comments != ''"],
    { review_mode: "ask", user_comments: "tweak X" },
    true,
  ],
  // an unparseable member of the list is treated TRUE (ignored).
  [["mode == 'fast'", "not a valid expr @@@"], { mode: "fast" }, true],
];

for (const [whenList, outputs, shouldRun] of WHEN_LIST_CASES) {
  test(`test_next_wave_when_list_is_anded[${whenList.join(" & ")}-${JSON.stringify(outputs)}]`, () => {
    const wave = nextWave(makeDef([bash("a", whenList)]), makeState({ a: "pending" }, outputs));
    const ran = wave.ready.map((s) => s.id);
    if (shouldRun) {
      assert.deepEqual(ran, ["a"]);
    } else {
      assert.deepEqual(ran, []);
      assert.deepEqual(
        wave.skipped.map((s) => s.id),
        ["a"],
      );
    }
  });
}

test("nextWave computes waves from depends_on and propagates skips", () => {
  const def = makeDef([
    bash("a"),
    { ...bash("b"), depends_on: ["a"] },
    { ...bash("c"), depends_on: ["a"] },
    { ...bash("d"), depends_on: ["b", "c"] },
    { ...bash("e"), depends_on: ["b", "c"], "trigger-rule": "all-done" },
  ]);

  // Wave 1: only the root.
  assert.deepEqual(
    readyIds(
      def,
      makeState({ a: "pending", b: "pending", c: "pending", d: "pending", e: "pending" }),
    ),
    ["a"],
  );
  // Wave 2: both children of a.
  assert.deepEqual(
    readyIds(
      def,
      makeState({ a: "completed", b: "pending", c: "pending", d: "pending", e: "pending" }),
    ),
    ["b", "c"],
  );
  // b failed: d skips (all-success), e waits for c (all-done).
  const wave = nextWave(
    def,
    makeState({ a: "completed", b: "failed", c: "running", d: "pending", e: "pending" }),
  );
  assert.deepEqual(wave.ready, []);
  assert.deepEqual(
    wave.skipped.map((s) => s.id),
    ["d"],
  );
  assert.match(
    wave.skipped[0]?.reason ?? "",
    /trigger-rule all-success not satisfied: b=failed, c=running/,
  );
  assert.equal(wave.done, false);
  // c done: e runs on all-done.
  assert.deepEqual(
    readyIds(
      def,
      makeState({ a: "completed", b: "failed", c: "completed", d: "skipped", e: "pending" }),
    ),
    ["e"],
  );
  // Everything terminal with a failure: done + failed.
  const final = nextWave(
    def,
    makeState({ a: "completed", b: "failed", c: "completed", d: "skipped", e: "completed" }),
  );
  assert.equal(final.done, true);
  assert.equal(final.failed, true);
});

test("nextWave is not done while a step is running", () => {
  const wave = nextWave(makeDef([bash("a")]), makeState({ a: "running" }));
  assert.equal(wave.done, false);
  assert.equal(wave.failed, false);
});

test("nextWave ignores dependencies missing from the state", () => {
  const def = makeDef([{ ...bash("b"), depends_on: ["ghost"] }]);
  assert.deepEqual(readyIds(def, makeState({ b: "pending" })), ["b"]);
});

test("nextWave skip reason names the false when: condition", () => {
  const wave = nextWave(
    makeDef([bash("a", "mode == 'fast'")]),
    makeState({ a: "pending" }, { mode: "slow" }),
  );
  assert.deepEqual(wave.skipped, [{ id: "a", reason: "when: mode == 'fast' is false" }]);
});

test("nextWave when: sees inputs and answers as well as outputs", () => {
  const def = makeDef([
    bash("a", "inputs.mode == 'fast' && answers.profile == 'low'"),
    bash("b", "mode == 'fast' && profile == 'max'"),
  ]);
  const state = makeState(
    { a: "pending", b: "pending" },
    {},
    { inputs: { mode: "fast" }, answers: { profile: "low" } },
  );
  assert.deepEqual(readyIds(def, state), ["a"]);
});

// ---- evaluateWhen -----------------------------------------------------------------

const SCOPE = {
  outputs: { team: "solo", count: 3, flag: true, empty: "", zero: 0 },
  inputs: { mode: "fast", team: "inputs-team" },
  answers: { profile: "low" },
};

test("evaluateWhen: equality and inequality on dotted and bare identifiers", () => {
  assert.equal(evaluateWhen("outputs.team == 'solo'", SCOPE), true);
  assert.equal(evaluateWhen('inputs.mode == "fast"', SCOPE), true);
  assert.equal(evaluateWhen("answers.profile != 'max'", SCOPE), true);
  // bare names resolve outputs first, then inputs, then answers.
  assert.equal(evaluateWhen("team == 'solo'", SCOPE), true);
  assert.equal(evaluateWhen("mode == 'fast'", SCOPE), true);
  assert.equal(evaluateWhen("profile == 'low'", SCOPE), true);
});

test("evaluateWhen: numbers and booleans", () => {
  assert.equal(evaluateWhen("count == 3", SCOPE), true);
  assert.equal(evaluateWhen("count == '3'", SCOPE), true);
  assert.equal(evaluateWhen("count != 4", SCOPE), true);
  assert.equal(evaluateWhen("flag == true", SCOPE), true);
  assert.equal(evaluateWhen("flag != false", SCOPE), true);
  assert.equal(evaluateWhen("true", SCOPE), true);
  assert.equal(evaluateWhen("false", SCOPE), false);
  assert.equal(evaluateWhen("1.5 == 1.5", SCOPE), true);
});

test("evaluateWhen: && binds tighter than ||", () => {
  // Parsed as (false && false) || true.
  assert.equal(evaluateWhen("team == 'x' && mode == 'y' || profile == 'low'", SCOPE), true);
  // Parsed as true || (false && false).
  assert.equal(evaluateWhen("profile == 'low' || team == 'x' && mode == 'y'", SCOPE), true);
  // Parsed as false || (true && false).
  assert.equal(evaluateWhen("team == 'x' || profile == 'low' && mode == 'y'", SCOPE), false);
});

test("evaluateWhen: parentheses override precedence", () => {
  assert.equal(evaluateWhen("(team == 'x' || profile == 'low') && mode == 'y'", SCOPE), false);
  assert.equal(evaluateWhen("(team == 'x' || profile == 'low') && mode == 'fast'", SCOPE), true);
  assert.equal(evaluateWhen("((profile == 'low'))", SCOPE), true);
});

test("evaluateWhen: unary ! and bare truthiness", () => {
  assert.equal(evaluateWhen("team", SCOPE), true);
  assert.equal(evaluateWhen("empty", SCOPE), false);
  assert.equal(evaluateWhen("zero", SCOPE), false);
  assert.equal(evaluateWhen("count", SCOPE), true);
  assert.equal(evaluateWhen("flag", SCOPE), true);
  assert.equal(evaluateWhen("!empty", SCOPE), true);
  assert.equal(evaluateWhen("!team", SCOPE), false);
  assert.equal(evaluateWhen("!!team", SCOPE), true);
  assert.equal(evaluateWhen("!(team == 'solo')", SCOPE), false);
  assert.equal(evaluateWhen("!team == 'solo'", SCOPE), false);
});

test("evaluateWhen: unset identifiers", () => {
  assert.equal(evaluateWhen("missing", SCOPE), false);
  assert.equal(evaluateWhen("!missing", SCOPE), true);
  assert.equal(evaluateWhen("missing == 'x'", SCOPE), false);
  assert.equal(evaluateWhen("missing != 'x'", SCOPE), true);
  assert.equal(evaluateWhen("missing == ''", SCOPE), false);
  assert.equal(evaluateWhen("missing != ''", SCOPE), true);
  assert.equal(evaluateWhen("outputs.nested.deep == 'x'", SCOPE), false);
  assert.equal(evaluateWhen("nothing.here", {}), false);
});

test("evaluateWhen: quoted strings with spaces and the other quote kind", () => {
  const scope = { outputs: { msg: "hello world", q: "it's" } };
  assert.equal(evaluateWhen("msg == 'hello world'", scope), true);
  assert.equal(evaluateWhen('msg == "hello world"', scope), true);
  assert.equal(evaluateWhen("msg != 'hello  world'", scope), true);
  assert.equal(evaluateWhen('q == "it\'s"', scope), true);
  assert.equal(evaluateWhen("'' == ''", scope), true);
  assert.equal(evaluateWhen("'a b' != 'a  b'", scope), true);
});

test("evaluateWhen: malformed input throws with a position", () => {
  const cases: [string, RegExp][] = [
    ["mode == ", /unexpected end of expression at position 8/],
    ["mode == 'fast", /unterminated string at position 8/],
    ["(mode == 'fast'", /expected '\)' at position 15/],
    ["mode == 'fast')", /unexpected "\)" at position 14/],
    ["mode = 'fast'", /unexpected character "=" at position 5/],
    ["mode == 'fast' other", /unexpected "other" at position 15/],
    ["this is not a valid expr @@@", /unexpected character "@" at position 25/],
    ["a & b", /unexpected character "&" at position 2/],
    ["1.2.3 == 1", /bad number 1\.2\.3 at position 0/],
    ["a. == 'x'", /bad identifier a\. at position 0/],
    ["", /unexpected end of expression at position 0/],
  ];
  for (const [expr, pattern] of cases) {
    assert.throws(() => evaluateWhen(expr, SCOPE), pattern, expr);
  }
});
