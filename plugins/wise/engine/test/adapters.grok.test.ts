import { test } from "node:test";
import assert from "node:assert/strict";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  AUTH_RE,
  authFilePath,
  buildArgv,
  childEnv,
  createStreamParser,
  effortMap,
  grokAdapter,
  PERMISSION_MAP,
  probeAuth,
  PROMPT_ARGV_MAX,
  promptViaFile,
  RATE_LIMIT_RE,
  startGrok,
  writePromptFile,
} from "../src/adapters/grok.ts";
import type { SpawnExit } from "../src/adapters/spawn.ts";
import type { RawEvent, RunReq } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "fixtures", "adapters");
const RESULT = readFileSync(join(FIXTURES, "grok", "schema.result.json"), "utf8");
const RESUME = readFileSync(join(FIXTURES, "grok", "resume.result.json"), "utf8");
const SCHEMA = JSON.parse(readFileSync(join(FIXTURES, "schema.json"), "utf8")) as Record<
  string,
  unknown
>;
const SESSION = "01a06ead-86d2-7412-b4e2-ca1ef24f3925";

const BASE_REQ: RunReq = {
  prompt: "ping",
  model: "grok-4.6-build",
  cwd: "/tmp/work",
  mode: "approval-required",
  timeout_ms: 60_000,
  auth: "subscription",
};

const OK_EXIT: SpawnExit = { code: 0, signal: null, timedOut: false, stderr: "" };

function parseAll(text: string, exit: SpawnExit = OK_EXIT, pool: RunReq["auth"] = "subscription") {
  const parser = createStreamParser({ pool, now: () => "T" });
  const events = parser.feed(text);
  return { events, res: parser.finish(exit), snap: parser.snapshot() };
}

const doc = (fields: Record<string, unknown>, pretty = true): string =>
  JSON.stringify(
    { text: "hi", stopReason: "end_turn", sessionId: "s-1", ...fields },
    null,
    pretty ? 2 : 0,
  ) + "\n";

// ---- argv ----------------------------------------------------------------------------------------

test("buildArgv: base shape, -p prompt, json output, no auto-update, cwd, dontAsk", () => {
  assert.deepEqual(buildArgv(BASE_REQ), [
    "-p",
    "ping",
    "--output-format",
    "json",
    "--no-auto-update",
    "--cwd",
    "/tmp/work",
    "--permission-mode",
    "dontAsk",
    "-m",
    "grok-4.6-build",
  ]);
});

test("buildArgv: permission flags per mode; --always-approve only under full-access", () => {
  const table: [RunReq["mode"], string[]][] = [
    ["approval-required", ["--permission-mode", "dontAsk"]],
    ["auto", ["--permission-mode", "acceptEdits"]],
    ["full-access", ["--always-approve"]],
  ];
  for (const [mode, flags] of table) {
    const argv = buildArgv({ ...BASE_REQ, mode });
    assert.deepEqual([...PERMISSION_MAP[mode]], flags);
    for (const f of flags) assert.ok(argv.includes(f), `${mode}: ${f}`);
    assert.equal(argv.includes("--always-approve"), mode === "full-access", mode);
  }
});

test("buildArgv: effort, schema inline, max_turns, resume, rules, allow, inherit", () => {
  const argv = buildArgv({
    ...BASE_REQ,
    model: "inherit",
    effort: "high",
    schema: SCHEMA,
    max_turns: 5,
    resume: SESSION,
    system: "Be terse.",
    allowed_tools: ["Bash(git:*)", "WebSearch"],
  });
  const after = (flag: string): string | undefined => argv[argv.indexOf(flag) + 1];
  assert.equal(argv.includes("-m"), false, "inherit omits -m");
  assert.equal(after("--reasoning-effort"), "high");
  assert.deepEqual(JSON.parse(after("--json-schema") ?? ""), SCHEMA);
  assert.equal(after("--max-turns"), "5");
  assert.equal(after("--resume"), SESSION);
  assert.equal(after("--rules"), "Be terse.");
  const i = argv.indexOf("--allow");
  assert.deepEqual(argv.slice(i, i + 4), ["--allow", "Bash(git:*)", "--allow", "WebSearch"]);
  assert.equal(argv.includes("--system-prompt-override"), false);
  const bare = buildArgv(BASE_REQ);
  for (const flag of [
    "--reasoning-effort",
    "--json-schema",
    "--max-turns",
    "--resume",
    "--rules",
  ]) {
    assert.equal(bare.includes(flag), false, flag);
  }
  assert.equal(buildArgv({ ...BASE_REQ, resume: { s: 1 } }).includes("--resume"), false);
  assert.equal(buildArgv({ ...BASE_REQ, resume: "" }).includes("--resume"), false);
});

test("buildArgv: a prompt over PROMPT_ARGV_MAX bytes goes through --prompt-file, at the cap it stays on -p", () => {
  const atCap = "x".repeat(PROMPT_ARGV_MAX);
  const overCap = `${atCap}y`;
  assert.equal(promptViaFile(atCap), false);
  assert.equal(promptViaFile(overCap), true);
  assert.equal(promptViaFile("é".repeat(PROMPT_ARGV_MAX / 2 + 1)), true);
  assert.deepEqual(buildArgv({ ...BASE_REQ, prompt: atCap }).slice(0, 2), ["-p", atCap]);
  const big = buildArgv({ ...BASE_REQ, prompt: overCap }, { promptPath: "/tmp/p.md" });
  assert.deepEqual(big.slice(0, 2), ["--prompt-file", "/tmp/p.md"]);
  assert.equal(big.includes("-p"), false);
  assert.equal(big.includes(overCap), false);
  assert.throws(() => buildArgv({ ...BASE_REQ, prompt: overCap }), /promptPath/);
  const written = writePromptFile(overCap);
  assert.equal(readFileSync(written.path, "utf8"), overCap);
  written.cleanup();
  assert.equal(existsSync(written.path), false);
});

test("effortMap follows the shared per-harness table", () => {
  for (const e of ["low", "medium", "high", "xhigh", "max"] as const) assert.equal(effortMap(e), e);
});

// ---- env -----------------------------------------------------------------------------------------------

test("childEnv: XAI_API_KEY only under api-key, GROK_HOME kept, Claude vars stripped", () => {
  const parent = {
    HOME: "/h",
    PATH: "/bin",
    XAI_API_KEY: "xai",
    GROK_HOME: "/g",
    CLAUDECODE: "1",
    CLAUDE_CODE_ENTRYPOINT: "cli",
  };
  assert.deepEqual(childEnv({ auth: "subscription" }, parent), {
    HOME: "/h",
    PATH: "/bin",
    GROK_HOME: "/g",
  });
  assert.deepEqual(childEnv({ auth: "api-key", env: { WISE_STEP_TOKEN: "t" } }, parent), {
    HOME: "/h",
    PATH: "/bin",
    GROK_HOME: "/g",
    XAI_API_KEY: "xai",
    WISE_STEP_TOKEN: "t",
  });
});

// ---- parser: fixtures ------------------------------------------------------------------------------------

test("parser: pretty-printed result fixture yields ok, json, usage with cost, session cursor", () => {
  const { events, res, snap } = parseAll(RESULT);
  assert.ok(events.length > 1, "one RawEvent per stdout line");
  assert.ok(events.every((e) => e.harness === "grok" && e.ts === "T"));
  assert.equal(res.exit, "ok");
  assert.equal(res.error, undefined);
  assert.deepEqual(res.json, { answer: "pong", n: 42 });
  assert.equal(res.text, '{"answer":"pong","n":42}');
  assert.equal(res.cursor, SESSION);
  assert.deepEqual(res.usage, {
    input: 21102,
    output: 254,
    cache_read: 5760,
    cache_write: 0,
    cost_usd: 0.00792336,
    pool: "subscription",
  });
  assert.equal(snap.session_id, SESSION);
  assert.equal(snap.model, "grok-4.6-build");
  assert.equal(snap.result, true);
});

test("parser: resume fixture keeps the session id; pool follows the request", () => {
  const { res } = parseAll(RESUME, OK_EXIT, "api-key");
  assert.equal(res.exit, "ok");
  assert.deepEqual(res.json, { answer: "pong", n: 43 });
  assert.equal(res.cursor, SESSION);
  assert.equal(res.usage.pool, "api-key");
  assert.equal(res.usage.cost_usd, 0.00794478);
});

test("parser: chunked delivery and no trailing newline give the same result", () => {
  const parser = createStreamParser({ pool: "subscription" });
  const text = RESULT.trimEnd();
  for (let i = 0; i < text.length; i += 53) parser.feed(text.slice(i, i + 53));
  const res = parser.finish(OK_EXIT);
  assert.equal(res.exit, "ok");
  assert.deepEqual(res.json, { answer: "pong", n: 42 });
});

test("parser: NDJSON fallback takes the last result-shaped line", () => {
  const stream =
    '{"type":"progress","step":1}\n' +
    doc({ text: "one", sessionId: "s-a", structuredOutput: { n: 1 } }, false) +
    doc({ text: "two", sessionId: "s-b", structuredOutput: { n: 2 } }, false);
  const { res } = parseAll(stream);
  assert.equal(res.exit, "ok");
  assert.equal(res.text, "two");
  assert.deepEqual(res.json, { n: 2 });
  assert.equal(res.cursor, "s-b");
});

test("parser: junk stdout without a result is an error carrying the stdout", () => {
  const { res } = parseAll("warming up...\n", { ...OK_EXIT, code: 1 });
  assert.equal(res.exit, "error");
  assert.match(res.error ?? "", /no JSON result \(stdout: warming up/);
  assert.equal(res.cursor, undefined);
  const clean = parseAll("").res;
  assert.match(clean.error ?? "", /no JSON result \(exit code 0/);
});

// ---- parser: exit classification ------------------------------------------------------------------------

test("parser: exit classification table", () => {
  const rows: {
    name: string;
    stream: string;
    exit?: Partial<SpawnExit>;
    expect: string;
    error?: RegExp;
  }[] = [
    { name: "success", stream: doc({}), expect: "ok" },
    { name: "EndTurn casing", stream: doc({ stopReason: "EndTurn" }), expect: "ok" },
    {
      name: "max turns",
      stream: doc({ stopReason: "max_turns" }),
      expect: "max_turns",
      error: /max turns/,
    },
    {
      name: "is_error with rate limit text",
      stream: doc({ is_error: true, text: "429 rate limit exceeded" }),
      expect: "rate_limited",
      error: /429/,
    },
    {
      name: "error object with auth text",
      stream: doc({ error: { message: "401 Unauthorized: run grok login" } }),
      expect: "auth",
      error: /grok login/,
    },
    {
      name: "generic error string",
      stream: doc({ error: "boom" }),
      expect: "error",
      error: /^boom$/,
    },
    {
      name: "auth in stderr, no result",
      stream: "",
      exit: { code: 1, stderr: "GROK_AUTH_EXPIRED token expired, please sign in" },
      expect: "auth",
    },
    {
      name: "rate limit in stderr, no result",
      stream: "",
      exit: { code: 1, stderr: "error: Too Many Requests" },
      expect: "rate_limited",
    },
    {
      name: "result present despite non-zero exit stays ok",
      stream: doc({}),
      exit: { code: 1, stderr: "update check failed" },
      expect: "ok",
    },
    {
      name: "timeout wins",
      stream: doc({}),
      exit: { timedOut: true, signal: "SIGTERM", code: null },
      expect: "timeout",
    },
    {
      name: "spawn failure",
      stream: "",
      exit: { code: null, error: "spawn grok ENOENT" },
      expect: "error",
      error: /ENOENT/,
    },
    {
      name: "ok text mentioning 429 stays ok",
      stream: doc({ text: "the API returns 429 on rate limit" }),
      expect: "ok",
    },
  ];
  for (const row of rows) {
    const { res } = parseAll(row.stream, { ...OK_EXIT, ...row.exit });
    assert.equal(res.exit, row.expect, row.name);
    if (row.error) assert.match(res.error ?? "", row.error, row.name);
    if (row.expect === "ok") assert.equal(res.error, undefined, row.name);
  }
});

test("parser: error text clipped to 500 chars; regexes", () => {
  const { res } = parseAll("", { ...OK_EXIT, code: 1, stderr: "e".repeat(2000) });
  assert.equal(res.error?.length, 500);
  assert.ok(RATE_LIMIT_RE.test("Rate-Limit"));
  assert.ok(AUTH_RE.test("please reauthenticate"));
  assert.equal(AUTH_RE.test("all good"), false);
});

// ---- startGrok against a fake binary -------------------------------------------------------------------

function fakeBin(body: string): { bin: string; dir: string } {
  const dir = mkdtempSync(join(tmpdir(), "wise-fake-grok-"));
  const bin = join(dir, "grok");
  writeFileSync(bin, `#!/bin/sh\n${body}\n`);
  chmodSync(bin, 0o755);
  return { bin, dir };
}

const SLEEP_BODY = `exec "${process.execPath}" -e "setInterval(() => {}, 1000)"`;

test("startGrok: fake binary sees argv and a closed stdin; pretty JSON result parsed", async () => {
  const script = `
    const fs = require("node:fs");
    const args = process.argv.slice(2);
    const stdin = fs.readFileSync(0, "utf8");
    process.stdout.write(JSON.stringify({
      text: "ok", stopReason: "end_turn", sessionId: "fake-sess",
      usage: { input_tokens: 7, output_tokens: 3, cache_read_input_tokens: 2, cache_creation_input_tokens: 1 },
      total_cost_usd: 0.5,
      structuredOutput: { argv: args, stdin },
    }, null, 2) + "\\n");
  `;
  const scratch = mkdtempSync(join(tmpdir(), "wise-fake-grok-"));
  const file = join(scratch, "echo.cjs");
  writeFileSync(file, script);
  const { bin, dir } = fakeBin(`exec "${process.execPath}" "${file}" "$@"`);
  const events: RawEvent[] = [];
  const run = startGrok(
    { ...BASE_REQ, cwd: dir, schema: SCHEMA, mode: "full-access" },
    (e) => events.push(e),
    { bin, parentEnv: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" } },
  );
  assert.ok(run.pid > 0);
  assert.equal("nudge" in run, false);
  const res = await run.done;
  assert.equal(res.exit, "ok", res.error);
  const echoed = res.json as { argv: string[]; stdin: string };
  assert.deepEqual(echoed.argv.slice(0, 2), ["-p", "ping"]);
  assert.ok(echoed.argv.includes("--always-approve"));
  assert.equal(echoed.stdin, "");
  assert.equal(res.cursor, "fake-sess");
  assert.deepEqual(res.usage, {
    input: 7,
    output: 3,
    cache_read: 2,
    cache_write: 1,
    cost_usd: 0.5,
    pool: "subscription",
  });
  assert.ok(events.length > 1);
  assert.equal(run.snapshot().result, true);
});

test("startGrok: a large prompt is read from --prompt-file and the temp file is removed on exit", async () => {
  const script = `
    const fs = require("node:fs");
    const args = process.argv.slice(2);
    const i = args.indexOf("--prompt-file");
    const body = i >= 0 ? fs.readFileSync(args[i + 1], "utf8") : "";
    process.stdout.write(JSON.stringify({
      text: "ok", stopReason: "end_turn", sessionId: "big-sess",
      usage: { input_tokens: 1, output_tokens: 1 },
      structuredOutput: { argv: args, bytes: Buffer.byteLength(body), head: body.slice(0, 8) },
    }, null, 2) + "\\n");
  `;
  const scratch = mkdtempSync(join(tmpdir(), "wise-fake-grok-"));
  const file = join(scratch, "big.cjs");
  writeFileSync(file, script);
  const { bin, dir } = fakeBin(`exec "${process.execPath}" "${file}" "$@"`);
  const prompt = `BIGPROMPT${"z".repeat(PROMPT_ARGV_MAX)}`;
  const run = startGrok({ ...BASE_REQ, cwd: dir, prompt, schema: SCHEMA }, () => {}, {
    bin,
    parentEnv: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" },
  });
  const res = await run.done;
  assert.equal(res.exit, "ok", res.error);
  const echoed = res.json as { argv: string[]; bytes: number; head: string };
  assert.equal(echoed.argv[0], "--prompt-file");
  assert.equal(echoed.argv.includes("-p"), false);
  assert.equal(echoed.bytes, Buffer.byteLength(prompt));
  assert.equal(echoed.head, "BIGPROMP");
  assert.equal(existsSync(echoed.argv[1] ?? ""), false, "prompt temp file removed after exit");
});

test("startGrok: timeout kills the child; kill() settles as error; non-string cursor warns", async () => {
  const { bin, dir } = fakeBin(SLEEP_BODY);
  const env = { PATH: process.env.PATH ?? "" };
  const t = startGrok({ ...BASE_REQ, cwd: dir, timeout_ms: 300 }, () => {}, {
    bin,
    parentEnv: env,
  });
  assert.equal((await t.done).exit, "timeout");
  const k = startGrok({ ...BASE_REQ, cwd: dir, resume: 42 }, () => {}, { bin, parentEnv: env });
  setTimeout(() => k.kill(), 100);
  const res = await k.done;
  assert.equal(res.exit, "error");
  assert.match(res.error ?? "", /signal SIGTERM/);
  assert.deepEqual(res.warnings, ["ignored non-string resume cursor"]);
});

// ---- probeAuth ------------------------------------------------------------------------------------------------

test("probeAuth: api-key reads XAI_API_KEY; subscription checks auth.json under GROK_HOME or ~/.grok", async () => {
  assert.deepEqual(await probeAuth("api-key", { parentEnv: { XAI_API_KEY: "xai" } }), {
    ok: true,
    login_cmd: "export XAI_API_KEY=...",
  });
  assert.equal((await probeAuth("api-key", { parentEnv: {} })).ok, false);

  const home = mkdtempSync(join(tmpdir(), "wise-fake-grok-home-"));
  assert.equal(authFilePath({}, home), join(home, ".grok", "auth.json"));
  assert.equal(authFilePath({ GROK_HOME: "/g" }, home), "/g/auth.json");
  // Missing file.
  assert.deepEqual(await probeAuth("subscription", { parentEnv: {}, home }), {
    ok: false,
    login_cmd: "grok login",
  });
  // Present under the default dir.
  mkdirSync(join(home, ".grok"));
  writeFileSync(join(home, ".grok", "auth.json"), JSON.stringify({ access_token: "t" }));
  assert.equal((await probeAuth("subscription", { parentEnv: {}, home })).ok, true);
  // Empty object and junk are not logged in.
  writeFileSync(join(home, ".grok", "auth.json"), "{}");
  assert.equal((await probeAuth("subscription", { parentEnv: {}, home })).ok, false);
  writeFileSync(join(home, ".grok", "auth.json"), "junk");
  assert.equal((await probeAuth("subscription", { parentEnv: {}, home })).ok, false);
  // GROK_HOME wins over HOME.
  const alt = join(home, "alt");
  mkdirSync(alt);
  writeFileSync(join(alt, "auth.json"), JSON.stringify({ access_token: "t" }));
  assert.equal((await probeAuth("subscription", { parentEnv: { GROK_HOME: alt }, home })).ok, true);
  assert.equal(grokAdapter.id, "grok");
});

// ---- live smoke (WISE_LIVE=1) ------------------------------------------------------------------------------

test(
  "live: grok answers the fixture schema and resumes the session",
  { skip: process.env.WISE_LIVE !== "1" },
  async () => {
    const events: RawEvent[] = [];
    const cwd = mkdtempSync(join(tmpdir(), "wise-live-grok-"));
    const first = await grokAdapter.run(
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
      "live grok",
      JSON.stringify({ ...first, text: undefined }),
      "events",
      events.length,
    );
    assert.equal(first.exit, "ok", first.error);
    assert.equal((first.json as { answer?: string }).answer, "pong");
    assert.ok(first.usage.input > 0);
    assert.ok(typeof first.cursor === "string" && first.cursor.length > 0);
    const second = await grokAdapter.run(
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
    console.log("live grok resume", JSON.stringify({ ...second, text: undefined }));
    assert.equal(second.exit, "ok", second.error);
    assert.equal((second.json as { n?: number }).n, 43);
    assert.equal(second.cursor, first.cursor);
  },
);
