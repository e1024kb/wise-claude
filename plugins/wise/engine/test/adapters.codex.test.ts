import { test } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  AUTH_RE,
  buildArgv,
  childEnv,
  codexAdapter,
  composePrompt,
  createStreamParser,
  effortMap,
  probeAuth,
  RATE_LIMIT_RE,
  SANDBOX_MAP,
  startCodex,
  writeSchemaFile,
} from "../src/adapters/codex.ts";
import type { SpawnExit } from "../src/adapters/spawn.ts";
import type { RawEvent, RunReq } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "fixtures", "adapters");
const EXEC = readFileSync(join(FIXTURES, "codex", "exec-schema.ndjson"), "utf8");
const RESUME = readFileSync(join(FIXTURES, "codex", "resume.ndjson"), "utf8");
const SCHEMA = JSON.parse(readFileSync(join(FIXTURES, "schema.json"), "utf8")) as Record<
  string,
  unknown
>;
const THREAD = "01a06ead-00e9-7ea1-a0fe-56e3d5f05ff2";

const BASE_REQ: RunReq = {
  prompt: "ping",
  model: "gpt-5",
  cwd: "/tmp/work",
  mode: "approval-required",
  timeout_ms: 60_000,
  auth: "subscription",
};

const OK_EXIT: SpawnExit = { code: 0, signal: null, timedOut: false, stderr: "" };

function parseAll(
  text: string,
  exit: SpawnExit = OK_EXIT,
  opts: { pool?: RunReq["auth"]; expectJson?: boolean } = {},
) {
  const parser = createStreamParser({
    pool: opts.pool ?? "subscription",
    expectJson: opts.expectJson ?? true,
    now: () => "T",
  });
  const events = parser.feed(text);
  return { events, res: parser.finish(exit), snap: parser.snapshot() };
}

const line = (obj: Record<string, unknown>): string => `${JSON.stringify(obj)}\n`;
const OK_TURN =
  line({ type: "thread.started", thread_id: "t1" }) +
  line({ type: "turn.started" }) +
  line({ type: "item.completed", item: { id: "i0", type: "agent_message", text: "done" } }) +
  line({ type: "turn.completed", usage: { input_tokens: 10, output_tokens: 5 } });

// ---- argv ----------------------------------------------------------------------------------------

test("buildArgv: fresh exec shape per mode, prompt last, stdin never used", () => {
  const argv = buildArgv(BASE_REQ);
  assert.deepEqual(argv, [
    "exec",
    "--json",
    "--skip-git-repo-check",
    "-C",
    "/tmp/work",
    "-s",
    "read-only",
    "-c",
    'approval_policy="never"',
    "-m",
    "gpt-5",
    "ping",
  ]);
  const table: [RunReq["mode"], string][] = [
    ["approval-required", "read-only"],
    ["auto", "workspace-write"],
    ["full-access", "danger-full-access"],
  ];
  for (const [mode, sandbox] of table) {
    const a = buildArgv({ ...BASE_REQ, mode });
    assert.equal(a[a.indexOf("-s") + 1], sandbox, mode);
    assert.equal(SANDBOX_MAP[mode], sandbox);
    assert.equal(a.includes("--dangerously-bypass-approvals-and-sandbox"), false);
  }
});

test("buildArgv: effort, schema path, add_dirs, system prompt, model inherit", () => {
  const argv = buildArgv(
    {
      ...BASE_REQ,
      effort: "xhigh",
      schema: SCHEMA,
      add_dirs: ["/tmp/run-a", "/tmp/run-b"],
      system: "Be terse.",
      model: "inherit",
    },
    { schemaPath: "/tmp/s/schema.json" },
  );
  const after = (flag: string): string | undefined => argv[argv.indexOf(flag) + 1];
  assert.equal(after("--output-schema"), "/tmp/s/schema.json");
  assert.ok(argv.includes('model_reasoning_effort="xhigh"'));
  assert.equal(argv.includes("-m"), false, "inherit omits -m");
  const i = argv.indexOf("--add-dir");
  assert.deepEqual(argv.slice(i, i + 4), ["--add-dir", "/tmp/run-a", "--add-dir", "/tmp/run-b"]);
  assert.equal(argv.at(-1), "Be terse.\n\nping");
  assert.equal(composePrompt({ prompt: "p" }), "p");
  // A schema without a file path is a programming error, not a silent omission.
  assert.throws(() => buildArgv({ ...BASE_REQ, schema: SCHEMA }), /schemaPath/);
  const bare = buildArgv(BASE_REQ);
  for (const flag of ["--output-schema", "--add-dir", "model_reasoning_effort"]) {
    assert.equal(
      bare.some((a) => a.includes(flag)),
      false,
      flag,
    );
  }
});

test("buildArgv: resume form uses config overrides instead of exec-only flags", () => {
  const argv = buildArgv(
    { ...BASE_REQ, resume: THREAD, mode: "auto", effort: "low", schema: SCHEMA, add_dirs: ["/r"] },
    { schemaPath: "/tmp/s/schema.json" },
  );
  assert.deepEqual(argv.slice(0, 3), ["exec", "resume", THREAD]);
  assert.equal(argv.includes("-C"), false);
  assert.equal(argv.includes("-s"), false);
  assert.equal(argv.includes("--add-dir"), false);
  assert.ok(argv.includes('sandbox_mode="workspace-write"'));
  assert.ok(argv.includes('sandbox_workspace_write.writable_roots=["/r"]'));
  assert.ok(argv.includes('model_reasoning_effort="low"'));
  assert.equal(argv[argv.indexOf("--output-schema") + 1], "/tmp/s/schema.json");
  assert.equal(argv.at(-1), "ping");
  // Non-string or empty cursors mean a fresh exec.
  assert.equal(buildArgv({ ...BASE_REQ, resume: { t: 1 } })[1], "--json");
  assert.equal(buildArgv({ ...BASE_REQ, resume: "" })[1], "--json");
});

test("effortMap follows the shared per-harness table", () => {
  for (const e of ["low", "medium", "high", "xhigh", "max"] as const) assert.equal(effortMap(e), e);
});

test("writeSchemaFile: writes the schema, cleanup removes the temp dir", () => {
  const f = writeSchemaFile(SCHEMA);
  assert.deepEqual(JSON.parse(readFileSync(f.path, "utf8")), SCHEMA);
  f.cleanup();
  assert.equal(existsSync(f.path), false);
  assert.equal(existsSync(dirname(f.path)), false);
  f.cleanup(); // idempotent
});

// ---- env -----------------------------------------------------------------------------------------

test("childEnv: OPENAI_API_KEY only under api-key, CODEX_HOME kept, Claude session vars stripped", () => {
  const parent = {
    HOME: "/h",
    PATH: "/bin",
    OPENAI_API_KEY: "sk",
    CODEX_HOME: "/cx",
    CLAUDECODE: "1",
    CLAUDE_CODE_SESSION_ID: "abc",
  };
  assert.deepEqual(childEnv({ auth: "subscription" }, parent), {
    HOME: "/h",
    PATH: "/bin",
    CODEX_HOME: "/cx",
  });
  assert.deepEqual(childEnv({ auth: "api-key", env: { WISE_STEP_TOKEN: "t" } }, parent), {
    HOME: "/h",
    PATH: "/bin",
    CODEX_HOME: "/cx",
    OPENAI_API_KEY: "sk",
    WISE_STEP_TOKEN: "t",
  });
});

// ---- parser: fixtures ---------------------------------------------------------------------------------

test("parser: exec fixture yields ok, json, usage, thread cursor", () => {
  const { events, res, snap } = parseAll(EXEC);
  assert.equal(events.length, 4);
  assert.ok(events.every((e) => e.harness === "codex" && e.ts === "T" && e.parsed !== undefined));
  assert.equal(res.exit, "ok");
  assert.equal(res.error, undefined);
  assert.deepEqual(res.json, { answer: "pong", n: 42 });
  assert.equal(res.text, '{"answer":"pong","n":42}');
  assert.equal(res.cursor, THREAD);
  assert.deepEqual(res.usage, {
    input: 13697,
    output: 19,
    cache_read: 1408,
    cache_write: 0,
    pool: "subscription",
  });
  assert.equal(snap.thread_id, THREAD);
  assert.equal(snap.turns, 1);
  assert.equal(snap.completed, 1);
  assert.deepEqual(snap.commands, []);
});

test("parser: resume fixture keeps the thread id and reads the cached input", () => {
  const { res } = parseAll(RESUME, OK_EXIT, { pool: "api-key" });
  assert.equal(res.exit, "ok");
  assert.deepEqual(res.json, { answer: "pong", n: 43 });
  assert.equal(res.cursor, THREAD);
  assert.equal(res.usage.cache_read, 13696);
  assert.equal(res.usage.input, 13729);
  assert.equal(res.usage.pool, "api-key");
  assert.equal(res.usage.cost_usd, undefined, "codex reports no cost");
});

test("parser: partial chunks reassemble; trailing line flushed by finish()", () => {
  const parser = createStreamParser({ pool: "subscription", expectJson: true });
  const events: RawEvent[] = [];
  const text = EXEC.trimEnd();
  for (let i = 0; i < text.length; i += 37) events.push(...parser.feed(text.slice(i, i + 37)));
  assert.equal(events.length, 3, "last line has no newline until finish");
  const res = parser.finish(OK_EXIT);
  assert.equal(res.exit, "ok");
  assert.deepEqual(res.json, { answer: "pong", n: 42 });
  assert.equal(res.usage.output, 19);
});

test("parser: items are tallied; plain text without a schema is not parsed", () => {
  const stream =
    line({ type: "thread.started", thread_id: "t2" }) +
    line({ type: "turn.started" }) +
    line({
      type: "item.completed",
      item: { id: "i1", type: "command_execution", command: "ls", exit_code: 0 },
    }) +
    line({
      type: "item.completed",
      item: { id: "i2", type: "file_change", changes: [{ path: "a" }, { path: "b" }] },
    }) +
    line({ type: "item.completed", item: { id: "i3", type: "reasoning", text: "hmm" } }) +
    line({ type: "item.completed", item: { id: "i4", type: "agent_message", text: "all done" } }) +
    line({ type: "turn.completed", usage: { input_tokens: 1, output_tokens: 2 } }) +
    "not json\n";
  const { res, snap, events } = parseAll(stream, OK_EXIT, { expectJson: false });
  assert.equal(res.exit, "ok");
  assert.equal(res.text, "all done");
  assert.equal(res.json, undefined);
  assert.deepEqual(snap.commands, ["ls"]);
  assert.equal(snap.file_changes, 2);
  assert.equal(events.at(-1)?.parsed, undefined);
});

test("parser: schema expected but final message is not JSON becomes error", () => {
  const { res } = parseAll(OK_TURN, OK_EXIT, { expectJson: true });
  assert.equal(res.exit, "error");
  assert.match(res.error ?? "", /not JSON: done/);
  assert.equal(res.cursor, "t1");
});

// ---- parser: exit classification ---------------------------------------------------------------------

test("parser: exit classification table", () => {
  const rows: {
    name: string;
    stream: string;
    exit?: Partial<SpawnExit>;
    expect: string;
    error?: RegExp;
  }[] = [
    { name: "success", stream: OK_TURN, expect: "ok" },
    {
      name: "turn.failed rate limit",
      stream:
        line({ type: "thread.started", thread_id: "t" }) +
        line({ type: "turn.started" }) +
        line({
          type: "turn.failed",
          error: { message: "You've hit your usage limit. Try again at 3pm." },
        }),
      exit: { code: 1 },
      expect: "rate_limited",
      error: /usage limit/,
    },
    {
      name: "error event 429",
      stream: line({ type: "error", message: "429 Too Many Requests" }),
      exit: { code: 1 },
      expect: "rate_limited",
    },
    {
      name: "auth in error event",
      stream: line({ type: "error", message: "Not logged in. Run `codex login`." }),
      exit: { code: 1 },
      expect: "auth",
      error: /codex login/,
    },
    {
      name: "auth in stderr, no events",
      stream: "",
      exit: { code: 1, stderr: "Error: 401 Unauthorized" },
      expect: "auth",
    },
    {
      name: "rate limit in stderr, no events",
      stream: "",
      exit: { code: 1, stderr: "stream error: rate limit reached" },
      expect: "rate_limited",
    },
    {
      name: "generic turn.failed",
      stream: line({ type: "turn.failed", error: { message: "boom" } }),
      exit: { code: 1 },
      expect: "error",
      error: /^boom$/,
    },
    {
      name: "completed turn but non-zero exit",
      stream: OK_TURN,
      exit: { code: 2, stderr: "panic" },
      expect: "error",
      error: /panic/,
    },
    {
      name: "no turn.completed, clean exit",
      stream: line({ type: "thread.started", thread_id: "t" }),
      expect: "error",
      error: /no turn.completed event/,
    },
    {
      name: "stdin notice alone is not an error signal",
      stream: OK_TURN,
      exit: { stderr: "Reading additional input from stdin..." },
      expect: "ok",
    },
    {
      name: "timeout wins",
      stream: OK_TURN,
      exit: { timedOut: true, signal: "SIGTERM", code: null },
      expect: "timeout",
    },
    {
      name: "spawn failure",
      stream: "",
      exit: { code: null, error: "spawn codex ENOENT" },
      expect: "error",
      error: /ENOENT/,
    },
    {
      name: "ok message mentioning 429 stays ok",
      stream:
        line({ type: "turn.started" }) +
        line({
          type: "item.completed",
          item: { type: "agent_message", text: "the API returns 429 on rate limit" },
        }) +
        line({ type: "turn.completed", usage: {} }),
      expect: "ok",
    },
  ];
  for (const row of rows) {
    const { res } = parseAll(row.stream, { ...OK_EXIT, ...row.exit }, { expectJson: false });
    assert.equal(res.exit, row.expect, row.name);
    if (row.error) assert.match(res.error ?? "", row.error, row.name);
    if (row.expect === "ok") assert.equal(res.error, undefined, row.name);
  }
});

test("parser: error text clipped to 500 chars; regexes", () => {
  const { res } = parseAll("", { ...OK_EXIT, code: 1, stderr: "e".repeat(2000) }, {});
  assert.equal(res.error?.length, 500);
  assert.ok(RATE_LIMIT_RE.test("Rate-Limit"));
  assert.ok(RATE_LIMIT_RE.test("usage limit reached"));
  assert.ok(AUTH_RE.test("Please run codex login"));
  assert.equal(AUTH_RE.test("all good"), false);
});

// ---- startCodex against a fake binary --------------------------------------------------------------

function fakeBin(body: string): { bin: string; dir: string } {
  const dir = mkdtempSync(join(tmpdir(), "wise-fake-codex-"));
  const bin = join(dir, "codex");
  writeFileSync(bin, `#!/bin/sh\n${body}\n`);
  chmodSync(bin, 0o755);
  return { bin, dir };
}

const SLEEP_BODY = `exec "${process.execPath}" -e "setInterval(() => {}, 1000)"`;

test("startCodex: fake binary sees argv, schema file, closed stdin; temp schema cleaned up", async () => {
  // Echo argv, the schema file contents, and whether stdin hit EOF, as codex-shaped events.
  const script = `
    const fs = require("node:fs");
    const args = process.argv.slice(2);
    const i = args.indexOf("--output-schema");
    const schema = i >= 0 ? fs.readFileSync(args[i + 1], "utf8") : "";
    const stdin = fs.readFileSync(0, "utf8");
    const out = (o) => process.stdout.write(JSON.stringify(o) + "\\n");
    out({ type: "thread.started", thread_id: "fake-thread" });
    out({ type: "turn.started" });
    out({ type: "item.completed", item: { type: "agent_message", text: JSON.stringify({ argv: args, schema, stdin }) } });
    out({ type: "turn.completed", usage: { input_tokens: 3, cached_input_tokens: 1, output_tokens: 4 } });
  `;
  const scratch = mkdtempSync(join(tmpdir(), "wise-fake-codex-"));
  const file = join(scratch, "echo.cjs");
  writeFileSync(file, script);
  const { bin, dir } = fakeBin(`exec "${process.execPath}" "${file}" "$@"`);
  const events: RawEvent[] = [];
  const run = startCodex(
    { ...BASE_REQ, cwd: dir, schema: SCHEMA, effort: "low" },
    (e) => events.push(e),
    { bin, parentEnv: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" } },
  );
  assert.ok(run.pid > 0);
  assert.equal("nudge" in run, false);
  const res = await run.done;
  assert.equal(res.exit, "ok", res.error);
  const echoed = res.json as { argv: string[]; schema: string; stdin: string };
  assert.equal(echoed.argv[0], "exec");
  assert.ok(echoed.argv.includes('model_reasoning_effort="low"'));
  assert.deepEqual(JSON.parse(echoed.schema), SCHEMA);
  assert.equal(echoed.stdin, "", "stdin closed without bytes");
  const schemaPath = echoed.argv[echoed.argv.indexOf("--output-schema") + 1] ?? "";
  assert.equal(existsSync(schemaPath), false, "schema temp file removed after exit");
  assert.equal(res.cursor, "fake-thread");
  assert.deepEqual(res.usage, {
    input: 3,
    output: 4,
    cache_read: 1,
    cache_write: 0,
    pool: "subscription",
  });
  assert.equal(events.length, 4);
  assert.equal(run.snapshot().completed, 1);
});

test("startCodex: timeout kills the child; kill() settles as error; non-string cursor warns", async () => {
  const { bin, dir } = fakeBin(SLEEP_BODY);
  const env = { PATH: process.env.PATH ?? "" };
  const t = startCodex({ ...BASE_REQ, cwd: dir, timeout_ms: 300 }, () => {}, {
    bin,
    parentEnv: env,
  });
  assert.equal((await t.done).exit, "timeout");
  const k = startCodex({ ...BASE_REQ, cwd: dir, resume: { bad: true } }, () => {}, {
    bin,
    parentEnv: env,
  });
  setTimeout(() => k.kill(), 100);
  const res = await k.done;
  assert.equal(res.exit, "error");
  assert.match(res.error ?? "", /signal SIGTERM/);
  assert.deepEqual(res.warnings, ["ignored non-string resume cursor"]);
});

// ---- probeAuth ----------------------------------------------------------------------------------------

test("probeAuth: api-key reads OPENAI_API_KEY; subscription parses `codex login status`", async () => {
  assert.deepEqual(await probeAuth("api-key", { parentEnv: { OPENAI_API_KEY: "sk" } }), {
    ok: true,
    login_cmd: "export OPENAI_API_KEY=...",
  });
  assert.equal((await probeAuth("api-key", { parentEnv: {} })).ok, false);
  const dir = mkdtempSync(join(tmpdir(), "wise-fake-codex-"));
  const yes = join(dir, "codex-yes");
  const no = join(dir, "codex-no");
  const noZero = join(dir, "codex-no-zero");
  writeFileSync(yes, `#!/bin/sh\necho 'Logged in using ChatGPT'\n`);
  writeFileSync(no, `#!/bin/sh\necho 'Not logged in' >&2\nexit 1\n`);
  writeFileSync(noZero, `#!/bin/sh\necho 'Not logged in'\n`);
  for (const f of [yes, no, noZero]) chmodSync(f, 0o755);
  const env = { PATH: process.env.PATH ?? "" };
  assert.deepEqual(await probeAuth("subscription", { bin: yes, parentEnv: env }), {
    ok: true,
    login_cmd: "codex login",
  });
  assert.equal((await probeAuth("subscription", { bin: no, parentEnv: env })).ok, false);
  assert.equal((await probeAuth("subscription", { bin: noZero, parentEnv: env })).ok, false);
  assert.equal(
    (await probeAuth("subscription", { bin: "/nonexistent/codex", parentEnv: env })).ok,
    false,
  );
  assert.equal(codexAdapter.id, "codex");
});

// ---- live smoke (WISE_LIVE=1) ------------------------------------------------------------------------------

test(
  "live: codex answers the fixture schema and resumes the thread",
  { skip: process.env.WISE_LIVE !== "1" },
  async () => {
    const events: RawEvent[] = [];
    const cwd = mkdtempSync(join(tmpdir(), "wise-live-codex-"));
    const first = await codexAdapter.run(
      {
        ...BASE_REQ,
        model: "inherit",
        prompt: "Reply with answer set to exactly 'pong' and n set to 42. Do not use tools.",
        effort: "low",
        schema: SCHEMA,
        cwd,
        timeout_ms: 180_000,
      },
      (e) => events.push(e),
    );
    console.log(
      "live codex",
      JSON.stringify({ ...first, text: undefined }),
      "events",
      events.length,
    );
    assert.equal(first.exit, "ok", first.error);
    assert.equal((first.json as { answer?: string }).answer, "pong");
    assert.ok(first.usage.input > 0);
    assert.ok(typeof first.cursor === "string" && first.cursor.length > 0);
    const second = await codexAdapter.run(
      {
        ...BASE_REQ,
        model: "inherit",
        prompt: "Same answer, n = previous n + 1. Do not use tools.",
        effort: "low",
        schema: SCHEMA,
        cwd,
        resume: first.cursor,
        timeout_ms: 180_000,
      },
      () => {},
    );
    console.log("live codex resume", JSON.stringify({ ...second, text: undefined }));
    assert.equal(second.exit, "ok", second.error);
    assert.equal((second.json as { n?: number }).n, 43);
    assert.equal(second.cursor, first.cursor);
  },
);
