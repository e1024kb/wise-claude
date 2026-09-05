import { test } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  APPROVAL_MAP,
  AUTH_RE,
  authFilePath,
  buildArgv,
  childEnv,
  composePrompt,
  createStreamParser,
  effortMap,
  extractJson,
  geminiAdapter,
  probeAuth,
  PROMPT_ARGV_MAX,
  promptViaStdin,
  RATE_LIMIT_RE,
  startGemini,
  TURN_LIMIT_RE,
} from "../src/adapters/gemini.ts";
import type { SpawnExit } from "../src/adapters/spawn.ts";
import type { RawEvent, RunReq } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "fixtures", "adapters");
const SCHEMA_STREAM = readFileSync(join(FIXTURES, "gemini", "schema.stream.ndjson"), "utf8");
const TOOL_STREAM = readFileSync(join(FIXTURES, "gemini", "tool.stream.ndjson"), "utf8");
const SCHEMA = JSON.parse(readFileSync(join(FIXTURES, "schema.json"), "utf8")) as Record<
  string,
  unknown
>;
const SESSION = "3f6c1a2e-9b4d-4c7e-8a1f-2d5e6b7c8d90";

const BASE_REQ: RunReq = {
  prompt: "ping",
  model: "gemini-2.5-pro",
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
const INIT = line({ type: "init", session_id: "s-1", model: "gemini-2.5-pro" });
const assistant = (content: string): string =>
  line({ type: "message", role: "assistant", content, delta: true });
const RESULT_OK = line({
  type: "result",
  status: "success",
  stats: { input_tokens: 10, output_tokens: 5, cached: 2, total_tokens: 15 },
});
const fatal = (type: string, message: string, code?: number | string): string =>
  line({
    type: "error",
    error: { type, message, ...(code === undefined ? {} : { code }) },
    stats: { input_tokens: 3, output_tokens: 0, cached: 0 },
  });

// ---- argv ----------------------------------------------------------------------------------------

test("buildArgv: base shape, -p prompt, stream-json, skip-trust, default approval, model", () => {
  assert.deepEqual(buildArgv(BASE_REQ), [
    "-p",
    "ping",
    "--output-format",
    "stream-json",
    "--skip-trust",
    "--approval-mode",
    "default",
    "-m",
    "gemini-2.5-pro",
  ]);
});

test("buildArgv: approval mode per run mode; yolo only under full-access", () => {
  const table: [RunReq["mode"], string][] = [
    ["approval-required", "default"],
    ["auto", "auto_edit"],
    ["full-access", "yolo"],
  ];
  for (const [mode, approval] of table) {
    const argv = buildArgv({ ...BASE_REQ, mode });
    assert.equal(APPROVAL_MAP[mode], approval);
    assert.equal(argv[argv.indexOf("--approval-mode") + 1], approval, mode);
    assert.equal(argv.includes("--yolo"), false);
    assert.equal(argv.includes("-y"), false);
  }
});

test("buildArgv: add_dirs, resume, inherit, system + schema folded into the prompt, no effort flag", () => {
  const argv = buildArgv({
    ...BASE_REQ,
    model: "inherit",
    effort: "high",
    schema: SCHEMA,
    system: "Be terse.",
    add_dirs: ["/tmp/run-a", "/tmp/run-b"],
    resume: SESSION,
    max_turns: 5,
  });
  assert.equal(argv.includes("-m"), false, "inherit omits -m");
  const i = argv.indexOf("--include-directories");
  assert.deepEqual(argv.slice(i, i + 4), [
    "--include-directories",
    "/tmp/run-a",
    "--include-directories",
    "/tmp/run-b",
  ]);
  assert.equal(argv[argv.indexOf("--resume") + 1], SESSION);
  const prompt = argv[1] ?? "";
  assert.ok(prompt.startsWith("Be terse.\n\nping\n\nRespond with only a JSON object"));
  assert.ok(prompt.endsWith(JSON.stringify(SCHEMA)));
  for (const flag of ["--effort", "--reasoning-effort", "--max-turns", "--json-schema"]) {
    assert.equal(argv.includes(flag), false, flag);
  }
  assert.equal(composePrompt({ prompt: "p" }), "p");
  assert.equal(composePrompt({ prompt: "p", system: "s" }), "s\n\np");
  assert.equal(buildArgv({ ...BASE_REQ, resume: { s: 1 } }).includes("--resume"), false);
  assert.equal(buildArgv({ ...BASE_REQ, resume: "" }).includes("--resume"), false);
});

test("buildArgv: a prompt over PROMPT_ARGV_MAX bytes leaves argv and goes on stdin; at the cap it stays on -p", () => {
  const atCap = "x".repeat(PROMPT_ARGV_MAX);
  const overCap = `${atCap}y`;
  assert.equal(promptViaStdin(atCap), false);
  assert.equal(promptViaStdin(overCap), true);
  assert.equal(promptViaStdin("é".repeat(PROMPT_ARGV_MAX / 2 + 1)), true);
  assert.deepEqual(buildArgv({ ...BASE_REQ, prompt: atCap }).slice(0, 2), ["-p", atCap]);
  const big = buildArgv({ ...BASE_REQ, prompt: overCap });
  assert.equal(big[0], "--output-format");
  assert.equal(big.includes("-p"), false);
  assert.equal(big.includes(overCap), false);
  // The schema instruction counts toward the cap: the composed prompt decides.
  const nearCap = "x".repeat(PROMPT_ARGV_MAX - 10);
  assert.equal(buildArgv({ ...BASE_REQ, prompt: nearCap, schema: SCHEMA }).includes("-p"), false);
});

test("effortMap: gemini has no effort control", () => {
  for (const e of ["low", "medium", "high", "xhigh", "max"] as const) {
    assert.equal(effortMap(e), undefined);
  }
});

// ---- env -----------------------------------------------------------------------------------------------

test("childEnv: key vars only under api-key, GEMINI_CLI_HOME and GOOGLE_CLOUD_PROJECT kept, Claude vars stripped", () => {
  const parent = {
    HOME: "/h",
    PATH: "/bin",
    GEMINI_API_KEY: "gk",
    GOOGLE_API_KEY: "ok",
    GEMINI_CLI_HOME: "/g",
    GOOGLE_CLOUD_PROJECT: "proj",
    CLAUDECODE: "1",
    CLAUDE_CODE_ENTRYPOINT: "cli",
  };
  assert.deepEqual(childEnv({ auth: "subscription" }, parent), {
    HOME: "/h",
    PATH: "/bin",
    GEMINI_CLI_HOME: "/g",
    GOOGLE_CLOUD_PROJECT: "proj",
  });
  assert.deepEqual(childEnv({ auth: "api-key", env: { WISE_STEP_TOKEN: "t" } }, parent), {
    HOME: "/h",
    PATH: "/bin",
    GEMINI_CLI_HOME: "/g",
    GOOGLE_CLOUD_PROJECT: "proj",
    GOOGLE_API_KEY: "ok",
    GEMINI_API_KEY: "gk",
    WISE_STEP_TOKEN: "t",
  });
});

// ---- schema-by-instruction extraction ---------------------------------------------------------------------

const errOf = (text: string): string => {
  const r = extractJson(text);
  return r.ok ? "" : r.error;
};

test("extractJson: whole text, fenced block, bare object in prose, braces inside strings, none", () => {
  assert.deepEqual(extractJson('{"answer":"pong","n":42}'), {
    ok: true,
    json: { answer: "pong", n: 42 },
  });
  assert.deepEqual(extractJson('  {"a":1}\n'), { ok: true, json: { a: 1 } });
  assert.deepEqual(extractJson('Sure:\n```json\n{"a": 1}\n```\nDone.'), {
    ok: true,
    json: { a: 1 },
    warning: "JSON extracted from a fenced code block",
  });
  assert.deepEqual(extractJson('```\n{"a":2}\n```'), {
    ok: true,
    json: { a: 2 },
    warning: "JSON extracted from a fenced code block",
  });
  assert.deepEqual(extractJson('The answer is {"a": {"b": [1, 2]}} as requested.'), {
    ok: true,
    json: { a: { b: [1, 2] } },
    warning: "JSON extracted from surrounding prose",
  });
  assert.deepEqual(extractJson('Note {not json} then {"s":"a } b \\" c","n":1}.'), {
    ok: true,
    json: { s: 'a } b " c', n: 1 },
    warning: "JSON extracted from surrounding prose",
  });
  assert.equal(extractJson("no object here").ok, false);
  assert.match(errOf("no object here"), /not JSON: no object here/);
  assert.match(errOf(""), /\(empty\)/);
  assert.match(errOf('{"unterminated": '), /not JSON/);
});

// ---- parser: fixtures ------------------------------------------------------------------------------------

test("parser: schema fixture yields ok, json from joined deltas, usage without cost, session cursor", () => {
  const { events, res, snap } = parseAll(SCHEMA_STREAM);
  assert.equal(events.length, 6, "one RawEvent per stdout line");
  assert.ok(events.every((e) => e.harness === "gemini" && e.ts === "T"));
  assert.equal(res.exit, "ok");
  assert.equal(res.error, undefined);
  assert.equal(res.warnings, undefined);
  assert.equal(res.text, '{"answer":"pong","n":42}');
  assert.deepEqual(res.json, { answer: "pong", n: 42 });
  assert.equal(res.cursor, SESSION);
  assert.deepEqual(res.usage, {
    input: 9280,
    output: 12,
    cache_read: 4096,
    cache_write: 0,
    pool: "subscription",
  });
  assert.equal(snap.session_id, SESSION);
  assert.equal(snap.model, "gemini-2.5-pro");
  assert.equal(snap.result, true);
  assert.equal(snap.segments, 1);
  assert.equal(snap.tool_uses, 0);
});

test("parser: tool fixture keeps the final segment, extracts fenced JSON with a warning, keeps notices", () => {
  const { res, snap } = parseAll(TOOL_STREAM, OK_EXIT, { pool: "api-key" });
  assert.equal(res.exit, "ok", res.error);
  assert.equal(res.text, 'Here is the result:\n```json\n{"answer":"pong","n":43}\n```\n');
  assert.deepEqual(res.json, { answer: "pong", n: 43 });
  assert.deepEqual(res.warnings, [
    "Switching from gemini-2.5-pro to gemini-2.5-flash for the rest of this session due to capacity.",
    "JSON extracted from a fenced code block",
  ]);
  assert.equal(res.cursor, "7b2e4d10-5c6f-4a8b-9d0e-1f2a3b4c5d6e");
  assert.equal(res.usage.pool, "api-key");
  assert.equal(res.usage.input, 12790);
  assert.equal(res.usage.cost_usd, undefined);
  assert.equal(snap.tool_uses, 1);
  assert.equal(snap.segments, 2);
  assert.equal(snap.model, "gemini-2.5-flash");
});

test("parser: chunked delivery and no trailing newline give the same result", () => {
  const parser = createStreamParser({ pool: "subscription", expectJson: true });
  const text = SCHEMA_STREAM.trimEnd();
  for (let i = 0; i < text.length; i += 37) parser.feed(text.slice(i, i + 37));
  const res = parser.finish(OK_EXIT);
  assert.equal(res.exit, "ok");
  assert.deepEqual(res.json, { answer: "pong", n: 42 });
});

test("parser: without a schema the text is returned as-is and never parsed", () => {
  const { res } = parseAll(INIT + assistant("plain prose, no JSON") + RESULT_OK, OK_EXIT, {
    expectJson: false,
  });
  assert.equal(res.exit, "ok");
  assert.equal(res.text, "plain prose, no JSON");
  assert.equal(res.json, undefined);
  assert.deepEqual(res.usage, {
    input: 10,
    output: 5,
    cache_read: 2,
    cache_write: 0,
    pool: "subscription",
  });
});

test("parser: a schema answer that is not JSON turns a success into an error", () => {
  const { res } = parseAll(INIT + assistant("I cannot do that.") + RESULT_OK);
  assert.equal(res.exit, "error");
  assert.match(res.error ?? "", /final assistant text is not JSON: I cannot do that\./);
  assert.equal(res.cursor, "s-1");
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
    { name: "success", stream: INIT + assistant('{"a":1}') + RESULT_OK, expect: "ok" },
    {
      name: "fatal auth event",
      stream: INIT + fatal("FatalAuthenticationError", "Please run gemini to log in", 41),
      exit: { code: 41 },
      expect: "auth",
      error: /FatalAuthenticationError: Please run gemini to log in \(code 41\)/,
    },
    {
      name: "exit 41 with plain stderr",
      stream: "",
      exit: { code: 41, stderr: "fatal" },
      expect: "auth",
    },
    {
      name: "IneligibleTierError on stderr before any event, exit 1 (this machine)",
      stream: "",
      exit: {
        code: 1,
        stderr:
          "Error authenticating: IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals.",
      },
      expect: "auth",
      error: /IneligibleTierError/,
    },
    {
      name: "429 fatal event",
      stream: INIT + fatal("ApiError", "Resource has been exhausted (e.g. check quota).", 429),
      exit: { code: 429 },
      expect: "rate_limited",
      error: /quota/,
    },
    {
      name: "RESOURCE_EXHAUSTED notice with severity error and no result",
      stream:
        INIT +
        line({ type: "error", severity: "error", message: "RESOURCE_EXHAUSTED: quota exceeded" }),
      exit: { code: 1 },
      expect: "rate_limited",
    },
    {
      name: "turn limit",
      stream:
        INIT + fatal("FatalTurnLimitedError", "Reached max session turns for this session.", 53),
      exit: { code: 53 },
      expect: "max_turns",
      error: /max session turns/,
    },
    {
      name: "tool needing confirmation under default approval",
      stream:
        INIT +
        fatal(
          "FatalToolExecutionError",
          'Tool execution for "WriteFile" requires user confirmation, which is not supported in non-interactive mode.',
          54,
        ),
      exit: { code: 54 },
      expect: "error",
      error: /requires user confirmation/,
    },
    {
      name: "untrusted workspace exit 55",
      stream: "",
      exit: { code: 55, stderr: "Gemini CLI is not running in a trusted directory." },
      expect: "error",
      error: /trusted directory/,
    },
    {
      name: "no result, nothing said",
      stream: "",
      exit: { code: 1 },
      expect: "error",
      error: /no result event \(exit code 1/,
    },
    {
      name: "junk stdout without a result carries the stdout",
      stream: "warming up...\n",
      exit: { code: 1 },
      expect: "error",
      error: /no result event \(stdout: warming up/,
    },
    {
      name: "result present despite non-zero exit stays ok",
      stream: INIT + assistant('{"a":1}') + RESULT_OK,
      exit: { code: 1, stderr: "Warning: 256-color support not detected." },
      expect: "ok",
    },
    {
      name: "timeout wins",
      stream: INIT + assistant('{"a":1}') + RESULT_OK,
      exit: { timedOut: true, signal: "SIGTERM", code: null },
      expect: "timeout",
    },
    {
      name: "spawn failure",
      stream: "",
      exit: { code: null, error: "spawn gemini ENOENT" },
      expect: "error",
      error: /ENOENT/,
    },
    {
      name: "ok text mentioning 429 and login stays ok",
      stream: INIT + assistant('{"note":"the API returns 429 after login"}') + RESULT_OK,
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
  assert.ok(RATE_LIMIT_RE.test("RESOURCE_EXHAUSTED"));
  assert.ok(RATE_LIMIT_RE.test("Rate-Limit"));
  assert.ok(AUTH_RE.test("UNAUTHENTICATED"));
  assert.ok(AUTH_RE.test("API key not valid"));
  assert.ok(TURN_LIMIT_RE.test("Reached max session turns"));
  assert.equal(AUTH_RE.test("all good"), false);
});

// ---- startGemini against a fake binary -------------------------------------------------------------------

function fakeBin(body: string): { bin: string; dir: string } {
  const dir = mkdtempSync(join(tmpdir(), "wise-fake-gemini-"));
  const bin = join(dir, "gemini");
  writeFileSync(bin, `#!/bin/sh\n${body}\n`);
  chmodSync(bin, 0o755);
  return { bin, dir };
}

const SLEEP_BODY = `exec "${process.execPath}" -e "setInterval(() => {}, 1000)"`;

/** A fake gemini that echoes argv and stdin inside the assistant JSON answer, stream-json style. */
const ECHO_SCRIPT = `
  const fs = require("node:fs");
  const args = process.argv.slice(2);
  const stdin = fs.readFileSync(0, "utf8");
  const out = (o) => process.stdout.write(JSON.stringify(o) + "\\n");
  out({ type: "init", timestamp: "t", session_id: "fake-sess", model: "gemini-fake" });
  out({ type: "message", timestamp: "t", role: "assistant", content: "Here you go: ", delta: true });
  out({ type: "message", timestamp: "t", role: "assistant", delta: true,
        content: JSON.stringify({ argv: args, stdinBytes: Buffer.byteLength(stdin), head: stdin.slice(0, 8) }) });
  out({ type: "result", timestamp: "t", status: "success",
        stats: { input_tokens: 7, output_tokens: 3, cached: 2, total_tokens: 12, tool_calls: 0, models: {} } });
`;

function echoBin(): { bin: string; dir: string } {
  const scratch = mkdtempSync(join(tmpdir(), "wise-fake-gemini-"));
  const file = join(scratch, "echo.cjs");
  writeFileSync(file, ECHO_SCRIPT);
  return fakeBin(`exec "${process.execPath}" "${file}" "$@"`);
}

const FAKE_ENV = { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" };

test("startGemini: fake binary sees argv and a closed stdin; JSON pulled out of prose with a warning", async () => {
  const { bin, dir } = echoBin();
  const events: RawEvent[] = [];
  const run = startGemini(
    { ...BASE_REQ, cwd: dir, schema: SCHEMA, mode: "full-access" },
    (e) => events.push(e),
    { bin, parentEnv: FAKE_ENV },
  );
  assert.ok(run.pid > 0);
  assert.equal("nudge" in run, false);
  const res = await run.done;
  assert.equal(res.exit, "ok", res.error);
  const echoed = res.json as { argv: string[]; stdinBytes: number; head: string };
  assert.equal(echoed.argv[0], "-p");
  assert.ok(echoed.argv[1]?.startsWith("ping\n\nRespond with only a JSON object"));
  assert.equal(echoed.argv[echoed.argv.indexOf("--approval-mode") + 1], "yolo");
  assert.equal(echoed.stdinBytes, 0);
  assert.equal(res.cursor, "fake-sess");
  assert.deepEqual(res.usage, {
    input: 7,
    output: 3,
    cache_read: 2,
    cache_write: 0,
    pool: "subscription",
  });
  assert.deepEqual(res.warnings, ["JSON extracted from surrounding prose"]);
  assert.equal(events.length, 4);
  assert.equal(run.snapshot().result, true);
  assert.equal(run.snapshot().model, "gemini-fake");
});

test("startGemini: a large prompt travels on stdin with no -p", async () => {
  const { bin, dir } = echoBin();
  const prompt = `BIGPROMPT${"z".repeat(PROMPT_ARGV_MAX)}`;
  const run = startGemini({ ...BASE_REQ, cwd: dir, prompt, schema: SCHEMA }, () => {}, {
    bin,
    parentEnv: FAKE_ENV,
  });
  const res = await run.done;
  assert.equal(res.exit, "ok", res.error);
  const echoed = res.json as { argv: string[]; stdinBytes: number; head: string };
  assert.equal(echoed.argv.includes("-p"), false);
  assert.equal(echoed.argv[0], "--output-format");
  assert.equal(echoed.stdinBytes, Buffer.byteLength(composePrompt({ prompt, schema: SCHEMA })));
  assert.equal(echoed.head, "BIGPROMP");
});

test("startGemini: timeout kills the child; kill() settles as error; non-string cursor warns", async () => {
  const { bin, dir } = fakeBin(SLEEP_BODY);
  const env = { PATH: process.env.PATH ?? "" };
  const t = startGemini({ ...BASE_REQ, cwd: dir, timeout_ms: 300 }, () => {}, {
    bin,
    parentEnv: env,
  });
  assert.equal((await t.done).exit, "timeout");
  const k = startGemini({ ...BASE_REQ, cwd: dir, resume: 42 }, () => {}, { bin, parentEnv: env });
  setTimeout(() => k.kill(), 100);
  const res = await k.done;
  assert.equal(res.exit, "error");
  assert.match(res.error ?? "", /signal SIGTERM/);
  assert.deepEqual(res.warnings, ["ignored non-string resume cursor"]);
});

// ---- probeAuth ------------------------------------------------------------------------------------------------

test("probeAuth: api-key reads either key var; subscription checks oauth_creds.json under GEMINI_CLI_HOME or ~/.gemini", async () => {
  assert.deepEqual(await probeAuth("api-key", { parentEnv: { GEMINI_API_KEY: "gk" } }), {
    ok: true,
    login_cmd: "export GEMINI_API_KEY=...",
  });
  assert.equal((await probeAuth("api-key", { parentEnv: { GOOGLE_API_KEY: "ok" } })).ok, true);
  assert.equal((await probeAuth("api-key", { parentEnv: {} })).ok, false);

  const home = mkdtempSync(join(tmpdir(), "wise-fake-gemini-home-"));
  assert.equal(authFilePath({}, home), join(home, ".gemini", "oauth_creds.json"));
  assert.equal(authFilePath({ GEMINI_CLI_HOME: "/g" }, home), "/g/.gemini/oauth_creds.json");
  // Missing file.
  assert.deepEqual(await probeAuth("subscription", { parentEnv: {}, home }), {
    ok: false,
    login_cmd: "gemini (interactive, then /auth)",
  });
  // Present under the default dir, with the real file's key names (values invented).
  mkdirSync(join(home, ".gemini"));
  const creds = join(home, ".gemini", "oauth_creds.json");
  writeFileSync(
    creds,
    JSON.stringify({
      access_token: "ya29.x",
      refresh_token: "1//x",
      scope: "openid",
      token_type: "Bearer",
      id_token: "eyJ",
      expiry_date: 1788567806720,
    }),
  );
  assert.equal((await probeAuth("subscription", { parentEnv: {}, home })).ok, true);
  writeFileSync(creds, JSON.stringify({ refresh_token: "1//x" }));
  assert.equal((await probeAuth("subscription", { parentEnv: {}, home })).ok, true);
  // No token, empty object and junk are not logged in.
  writeFileSync(creds, JSON.stringify({ scope: "openid" }));
  assert.equal((await probeAuth("subscription", { parentEnv: {}, home })).ok, false);
  writeFileSync(creds, "{}");
  assert.equal((await probeAuth("subscription", { parentEnv: {}, home })).ok, false);
  writeFileSync(creds, "junk");
  assert.equal((await probeAuth("subscription", { parentEnv: {}, home })).ok, false);
  // GEMINI_CLI_HOME wins over HOME.
  const alt = join(home, "alt");
  mkdirSync(join(alt, ".gemini"), { recursive: true });
  writeFileSync(join(alt, ".gemini", "oauth_creds.json"), JSON.stringify({ access_token: "t" }));
  assert.equal(
    (await probeAuth("subscription", { parentEnv: { GEMINI_CLI_HOME: alt }, home })).ok,
    true,
  );
  assert.equal(geminiAdapter.id, "gemini");
});

// ---- live smoke (WISE_LIVE=1) ------------------------------------------------------------------------------

test(
  "live: gemini answers the fixture schema, or reports auth on a machine with a broken login",
  { skip: process.env.WISE_LIVE !== "1" },
  async () => {
    const events: RawEvent[] = [];
    const cwd = mkdtempSync(join(tmpdir(), "wise-live-gemini-"));
    const res = await geminiAdapter.run(
      {
        ...BASE_REQ,
        model: "inherit",
        prompt: "Reply with answer set to exactly 'pong' and n set to 42. Do not use tools.",
        schema: SCHEMA,
        cwd,
        timeout_ms: 180_000,
      },
      (e) => events.push(e),
    );
    console.log("live gemini", JSON.stringify(res), "events", events.length);
    // Best effort (D15): the user's login is broken here, so `auth` is the expected outcome.
    assert.ok(["ok", "auth"].includes(res.exit), `${res.exit}: ${res.error ?? ""}`);
    if (res.exit === "ok") {
      assert.equal((res.json as { answer?: string }).answer, "pong");
      assert.ok(typeof res.cursor === "string" && res.cursor.length > 0);
    }
  },
);
