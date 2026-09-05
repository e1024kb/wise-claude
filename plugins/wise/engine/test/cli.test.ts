import { test } from "node:test";
import assert from "node:assert/strict";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { main } from "../src/cli.ts";
import type { Question } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const ENGINE = join(HERE, "..");
const BUNDLED = join(ENGINE, "..", "workflows");
// M3.1: the bundled ticket-plan is v2; tests read it in place (no fixture copy).
const FIXTURE = join(BUNDLED, "ticket-plan", "workflow.yaml");

async function run(argv: string[]): Promise<{ code: number; out: string; err: string }> {
  let out = "";
  let err = "";
  const code = await main(argv, { out: (s) => (out += s), err: (s) => (err += s), env: {} });
  return { code, out, err };
}

test("preflight on a v2 file emits the P1 questionary shape", async () => {
  const r = await run(["preflight", FIXTURE, "--profile", "low"]);
  assert.equal(r.code, 0, r.err);
  const j = JSON.parse(r.out) as {
    workflow: string;
    version: number;
    questions: Question[];
    defaults: Record<string, unknown>;
  };
  assert.equal(j.workflow, "ticket-plan");
  assert.equal(j.version, 2);
  assert.ok(j.questions.length > 0);
  for (const q of j.questions) {
    assert.match(q.id, /^(profile|tuning\.[a-z][a-z0-9-]*|step-select|input\.[a-z][a-z0-9_]*)$/);
    assert.ok(["choice", "multi", "text"].includes(q.kind));
    assert.equal(typeof q.label, "string");
  }
  assert.equal(j.defaults.profile, "low");
});

test("preflight --context pre-fills inputs from ticket refs", async () => {
  const ctx = JSON.stringify({ ticket: [{ ref: "LEC-1" }, { ref: "LEC-2" }] });
  const r = await run(["preflight", FIXTURE, "--context", ctx]);
  assert.equal(r.code, 0, r.err);
  const j = JSON.parse(r.out) as { questions: Question[] };
  const input = j.questions.find((q) => q.id.startsWith("input."));
  assert.ok(input);
  assert.equal(input.default, "LEC-1, LEC-2");
});

test("preflight rejects a bad --profile and bad --context with exit 64", async () => {
  assert.equal((await run(["preflight", FIXTURE, "--profile", "turbo"])).code, 64);
  assert.equal((await run(["preflight", FIXTURE, "--context", "{nope"])).code, 64);
});

test("preflight by name resolves through the roots; unknown name exits 2", async () => {
  const r = await run(["preflight", "nope", "--user-root", HERE, "--bundled-root", HERE]);
  assert.equal(r.code, 2);
  assert.match(r.out, /WORKFLOW_NOT_FOUND/);
});

test("compile-check passes every bundled workflow and fails the v1 fixtures with hints", async () => {
  const ok = await run([
    "compile-check",
    join(BUNDLED, "ticket-plan", "workflow.yaml"),
    join(BUNDLED, "example-workflow", "workflow.yaml"),
    join(BUNDLED, "ticket-auto", "workflow.yaml"),
    join(BUNDLED, "impl-plan-auto", "workflow.yaml"),
  ]);
  assert.equal(ok.code, 0, ok.out);
  const bad = await run([
    "compile-check",
    join(HERE, "fixtures", "migrate", "ticket-auto.v1.yaml"),
    join(HERE, "fixtures", "migrate", "impl-plan-auto.v1.yaml"),
  ]);
  assert.equal(bad.code, 1);
  const report = JSON.parse(bad.out) as {
    workflow: string;
    ok: boolean;
    issues: { hint?: string }[];
  }[];
  assert.equal(report.length, 2);
  for (const r of report) {
    assert.equal(r.ok, false, r.workflow);
    assert.ok(
      r.issues.some((i) => i.hint),
      `${r.workflow} has no v1 hint`,
    );
  }
});

test("compile-check --text renders one line per issue", async () => {
  const r = await run([
    "compile-check",
    join(HERE, "fixtures", "migrate", "ticket-auto.v1.yaml"),
    "--text",
  ]);
  assert.equal(r.code, 1);
  assert.match(r.out, /^FAIL ticket-auto/);
  assert.match(r.out, /->/);
});

test("migrate is a dry run listing v1 -> v2 notes; v2 input reports already_v2", async () => {
  const v1 = await run(["migrate", join(HERE, "fixtures", "migrate", "ticket-plan.v1.yaml")]);
  assert.equal(v1.code, 0, v1.err);
  const j = JSON.parse(v1.out) as { dry_run: boolean; already_v2: boolean; notes: unknown[] };
  assert.equal(j.dry_run, true);
  assert.equal(j.already_v2, false);
  assert.ok(j.notes.length > 0);
  const v2 = await run(["migrate", FIXTURE]);
  assert.equal((JSON.parse(v2.out) as { already_v2: boolean }).already_v2, true);
});

test("list-defs lists bundled and user definitions", async () => {
  const r = await run([
    "list-defs",
    "--user-root",
    join(HERE, "fixtures", "defs"),
    "--bundled-root",
    BUNDLED,
  ]);
  assert.equal(r.code, 0, r.err);
  const defs = JSON.parse(r.out) as { name: string; source: string }[];
  assert.ok(defs.some((d) => d.name === "ticket-plan" && d.source === "bundled"));
});

test("unknown command exits 64, help exits 0", async () => {
  assert.equal((await run(["bogus"])).code, 64);
  assert.equal((await run(["help"])).code, 0);
});
