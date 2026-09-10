import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  AUTH_RE,
  buildArgv,
  childEnv,
  claudeAdapter,
  createStreamParser,
  effortMap,
  MODE_MAP,
  probeAuth,
  RATE_LIMIT_RE,
  startClaude,
  initializeMessage,
  INIT_REQUEST_ID,
  permissionRequestOf,
  permissionResponse,
  userMessage,
} from "../src/adapters/claude.ts";
import type { SpawnExit } from "../src/adapters/spawn.ts";
import { adapterFor, AdapterError, hasAdapter } from "../src/adapters/index.ts";
import type { RawEvent, RunReq } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "fixtures", "adapters", "claude");
const STREAM = readFileSync(join(FIXTURES, "haiku-schema.stream.ndjson"), "utf8");
const PLUGIN_BLOCK = readFileSync(join(FIXTURES, "plugin-block.result.json"), "utf8");
const MCP_INJECT = readFileSync(join(FIXTURES, "mcp-inject.result.json"), "utf8");

const SCHEMA = {
  type: "object",
  properties: { answer: { type: "string" }, n: { type: "integer" } },
  required: ["answer", "n"],
};

const BASE_REQ: RunReq = {
  prompt: "ping",
  model: "haiku",
  cwd: "/tmp",
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

const resultLine = (fields: Record<string, unknown>): string =>
  `${JSON.stringify({ type: "result", session_id: "sess-1", ...fields })}\n`;

// ---- argv ----------------------------------------------------------------------------------------

test("buildArgv: base shape, mode mapping, stdin prompt, never --bare", () => {
  const argv = buildArgv(BASE_REQ);
  assert.deepEqual(argv, [
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--model",
    "haiku",
    "--permission-mode",
    "default",
    "--permission-prompt-tool",
    "stdio",
    "--mcp-config",
    '{"mcpServers":{}}',
  ]);
  assert.equal(argv.includes("--bare"), false);
  assert.equal(
    argv.includes("--strict-mcp-config"),
    false,
    "children inherit the CLI's MCP servers",
  );
  assert.equal(argv.includes("ping"), false, "prompt travels over stdin");
});

test("buildArgv: permission mode per run mode", () => {
  const table: [RunReq["mode"], string][] = [
    ["approval-required", "default"],
    ["auto", "acceptEdits"],
    ["full-access", "bypassPermissions"],
  ];
  for (const [mode, expected] of table) {
    const argv = buildArgv({ ...BASE_REQ, mode });
    assert.equal(argv[argv.indexOf("--permission-mode") + 1], expected, mode);
    assert.equal(MODE_MAP[mode], expected);
  }
});

test("buildArgv: schema, effort, max_turns, resume, system, mcp_config", () => {
  const argv = buildArgv({
    ...BASE_REQ,
    effort: "xhigh",
    schema: SCHEMA,
    max_turns: 7,
    resume: "13ceb80d-3fbe-4726-a052-f66bf181c9f6",
    system: "Be terse.",
    mcp_config: { mcpServers: { wise: { command: "wise-engine", args: ["unit-mcp"] } } },
  });
  const after = (flag: string): string | undefined => argv[argv.indexOf(flag) + 1];
  assert.equal(after("--effort"), "xhigh");
  assert.deepEqual(JSON.parse(after("--json-schema") ?? ""), SCHEMA);
  assert.equal(after("--max-turns"), "7");
  assert.equal(after("--resume"), "13ceb80d-3fbe-4726-a052-f66bf181c9f6");
  assert.equal(after("--append-system-prompt"), "Be terse.");
  assert.equal(argv.includes("--strict-mcp-config"), false);
  assert.deepEqual(JSON.parse(after("--mcp-config") ?? ""), {
    mcpServers: { wise: { command: "wise-engine", args: ["unit-mcp"] } },
  });
  // Flags whose values are absent do not appear.
  const bare = buildArgv(BASE_REQ);
  for (const flag of [
    "--effort",
    "--json-schema",
    "--max-turns",
    "--resume",
    "--append-system-prompt",
  ]) {
    assert.equal(bare.includes(flag), false, flag);
  }
});

test("buildArgv: non-string resume cursors are ignored", () => {
  assert.equal(buildArgv({ ...BASE_REQ, resume: { thread: 1 } }).includes("--resume"), false);
  assert.equal(buildArgv({ ...BASE_REQ, resume: "" }).includes("--resume"), false);
});

test("userMessage: stream-json user message, newline-terminated", () => {
  const line = userMessage("hi\nthere");
  assert.ok(line.endsWith("\n"));
  assert.deepEqual(JSON.parse(line), {
    type: "user",
    message: { role: "user", content: [{ type: "text", text: "hi\nthere" }] },
  });
});

test("effortMap is the identity", () => {
  for (const e of ["low", "medium", "high", "xhigh", "max"] as const) assert.equal(effortMap(e), e);
});

// ---- env -----------------------------------------------------------------------------------------------

test("childEnv: api key only under api-key, config dir kept, session vars stripped", () => {
  const parent = {
    HOME: "/h",
    PATH: "/bin",
    ANTHROPIC_API_KEY: "sk",
    CLAUDE_CONFIG_DIR: "/cfg",
    CLAUDECODE: "1",
    CLAUDE_CODE_ENTRYPOINT: "cli",
    CLAUDE_CODE_SESSION_ID: "abc",
  };
  const sub = childEnv({ auth: "subscription" }, parent);
  assert.deepEqual(sub, { HOME: "/h", PATH: "/bin", CLAUDE_CONFIG_DIR: "/cfg" });
  const key = childEnv({ auth: "api-key", env: { WISE_STEP_TOKEN: "tok" } }, parent);
  assert.deepEqual(key, {
    HOME: "/h",
    PATH: "/bin",
    CLAUDE_CONFIG_DIR: "/cfg",
    ANTHROPIC_API_KEY: "sk",
    WISE_STEP_TOKEN: "tok",
  });
});

// ---- parser: fixtures ---------------------------------------------------------------------------------

test("parser: haiku fixture yields ok, structured output, usage, cursor", () => {
  const { events, res, snap } = parseAll(STREAM);
  const lineCount = STREAM.split("\n").filter((l) => l.length > 0).length;
  assert.equal(events.length, lineCount);
  assert.ok(events.every((e) => e.harness === "claude" && e.ts === "T" && e.parsed !== undefined));
  assert.equal(res.exit, "ok");
  assert.equal(res.error, undefined);
  assert.deepEqual(res.json, { answer: "pong", n: 42 });
  assert.equal(res.text, '{"answer":"pong","n":42}');
  assert.equal(res.cursor, "13ceb80d-3fbe-4726-a052-f66bf181c9f6");
  // The fixture literal is 0.059784899999999995 (the spike wrote it as 0.0597849).
  const { cost_usd, ...tokens } = res.usage;
  assert.deepEqual(tokens, {
    input: 20,
    output: 420,
    cache_read: 27169,
    cache_write: 27474,
    pool: "subscription",
  });
  assert.ok(Math.abs((cost_usd ?? 0) - 0.0597849) < 1e-12);
  assert.equal(snap.session_id, "13ceb80d-3fbe-4726-a052-f66bf181c9f6");
  assert.equal(snap.tools, 215);
  assert.equal(snap.model, "claude-haiku-4-5-20251001");
  assert.equal(snap.turns, 4);
  assert.deepEqual(snap.tool_uses, ["StructuredOutput"]);
  assert.equal(snap.results, 1);
  assert.deepEqual(snap.denials, []);
});

test("parser: usage pool follows the request auth", () => {
  const { res } = parseAll(STREAM, OK_EXIT, "api-key");
  assert.equal(res.usage.pool, "api-key");
});

test("parser: --output-format json results parse as a one-line stream", () => {
  const block = parseAll(PLUGIN_BLOCK).res;
  assert.equal(block.exit, "ok");
  assert.deepEqual(block.json, { answer: "blocked 240s", n: 240 });
  assert.equal(block.cursor, "87b96749-a791-4dc5-8a1a-349a60ca5272");
  assert.deepEqual(block.usage, {
    input: 38,
    output: 562,
    cache_read: 100374,
    cache_write: 10046,
    cost_usd: 0.032977400000000004,
    pool: "subscription",
  });
  const inject = parseAll(MCP_INJECT).res;
  assert.equal(inject.exit, "ok");
  assert.deepEqual(inject.json, { answer: "reported", n: 1 });
  assert.equal(inject.usage.cache_write, 7188);
  assert.equal(inject.usage.cost_usd, 0.0235168);
});

// ---- parser: chunking and junk ------------------------------------------------------------------------

test("parser: partial lines across chunks reassemble to the same result", () => {
  const parser = createStreamParser({ pool: "subscription", now: () => "T" });
  const events: RawEvent[] = [];
  // Feed in awkward slices, including one that splits a multi-byte-safe string mid-token.
  for (let i = 0; i < STREAM.length; i += 777)
    events.push(...parser.feed(STREAM.slice(i, i + 777)));
  const res = parser.finish(OK_EXIT);
  const lineCount = STREAM.split("\n").filter((l) => l.length > 0).length;
  assert.equal(events.length, lineCount);
  assert.equal(res.exit, "ok");
  assert.deepEqual(res.json, { answer: "pong", n: 42 });
  assert.equal(res.usage.output, 420);
});

test("parser: trailing line without newline is flushed by finish()", () => {
  const parser = createStreamParser({ pool: "subscription" });
  assert.deepEqual(
    parser.feed(resultLine({ is_error: false, subtype: "success", result: "x" }).trimEnd()),
    [],
  );
  const res = parser.finish(OK_EXIT);
  assert.equal(res.exit, "ok");
  assert.equal(res.text, "x");
});

test("parser: non-JSON lines pass through with parsed undefined", () => {
  const parser = createStreamParser({ pool: "subscription" });
  const events = parser.feed("warning: something\n{not json\n\n");
  assert.equal(events.length, 3);
  assert.equal(events[0]?.line, "warning: something");
  assert.equal(events[0]?.parsed, undefined);
  assert.equal(events[1]?.parsed, undefined);
  assert.equal(events[2]?.line, "");
  const res = parser.finish({ ...OK_EXIT, code: 1 });
  assert.equal(res.exit, "error");
  assert.match(res.error ?? "", /no result event \(exit code 1/);
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
    {
      name: "success",
      stream: resultLine({ is_error: false, subtype: "success", result: "hi" }),
      expect: "ok",
    },
    {
      name: "max turns",
      stream: resultLine({ is_error: true, subtype: "error_max_turns" }),
      expect: "max_turns",
    },
    {
      name: "rate limit in result text",
      stream: resultLine({
        is_error: true,
        subtype: "error_during_execution",
        result: "API Error: 429 rate limit",
      }),
      expect: "rate_limited",
      error: /429/,
    },
    {
      name: "overloaded in result text",
      stream: resultLine({
        is_error: true,
        subtype: "error_during_execution",
        result: "Overloaded",
      }),
      expect: "rate_limited",
    },
    {
      name: "auth in result text",
      stream: resultLine({
        is_error: true,
        subtype: "error_during_execution",
        result: "Failed to authenticate. Run claude auth login.",
      }),
      expect: "auth",
      error: /authenticate/,
    },
    {
      name: "auth in stderr, no result",
      stream: "",
      exit: { code: 1, stderr: "Not logged in · Please run /login" },
      expect: "auth",
      error: /logged in/,
    },
    {
      name: "rate limit in stderr, no result",
      stream: "",
      exit: { code: 1, stderr: "Error: rate_limit_error" },
      expect: "rate_limited",
    },
    {
      name: "generic result error",
      stream: resultLine({ is_error: true, subtype: "error_during_execution", result: "boom" }),
      expect: "error",
      error: /^boom$/,
    },
    {
      name: "missing result, non-zero exit, stderr",
      stream: '{"type":"system","subtype":"init","session_id":"s"}\n',
      exit: { code: 2, stderr: "segfault-ish" },
      expect: "error",
      error: /segfault-ish/,
    },
    {
      name: "missing result, clean exit",
      stream: "",
      expect: "error",
      error: /no result event/,
    },
    {
      name: "timeout wins over a result",
      stream: resultLine({ is_error: false, subtype: "success", result: "late" }),
      exit: { timedOut: true, signal: "SIGTERM", code: null },
      expect: "timeout",
    },
    {
      name: "spawn failure",
      stream: "",
      exit: { code: null, error: "spawn claude ENOENT" },
      expect: "error",
      error: /ENOENT/,
    },
    {
      name: "ok result text mentioning rate limit stays ok",
      stream: resultLine({
        is_error: false,
        subtype: "success",
        result: "the rate limit is 429 per minute",
      }),
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

test("parser: error text is clipped to 500 chars", () => {
  const { res } = parseAll("", { ...OK_EXIT, code: 1, stderr: "e".repeat(2000) });
  assert.equal(res.error?.length, 500);
});

test("parser: regexes", () => {
  assert.ok(RATE_LIMIT_RE.test("Rate-Limit"));
  assert.ok(RATE_LIMIT_RE.test("ratelimit"));
  assert.ok(AUTH_RE.test("OAuth token expired"));
  assert.equal(AUTH_RE.test("all good"), false);
});

// ---- parser: text fallbacks and denials -------------------------------------------------------------

test("parser: text falls back to the last assistant text", () => {
  const stream =
    '{"type":"assistant","message":{"content":[{"type":"text","text":"first"}]}}\n' +
    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{}}]}}\n' +
    '{"type":"assistant","message":{"content":[{"type":"text","text":"second "},{"type":"text","text":"half"}]}}\n';
  const { res, snap } = parseAll(stream, { ...OK_EXIT, code: 1 });
  assert.equal(res.text, "second half");
  assert.equal(snap.turns, 3);
  assert.deepEqual(snap.tool_uses, ["Bash"]);
  assert.equal(res.exit, "error");
});

test("parser: permission denials fill an empty text and stay ok", () => {
  const stream = resultLine({
    is_error: false,
    subtype: "success",
    result: "",
    permission_denials: [{ tool_name: "Bash", tool_input: { command: "rm -rf /" } }],
  });
  const { res, snap } = parseAll(stream);
  assert.equal(res.exit, "ok");
  assert.equal(res.text, 'permission denied: Bash({"command":"rm -rf /"})');
  assert.deepEqual(res.warnings, ['1 permission denial(s): Bash({"command":"rm -rf /"})']);
  assert.deepEqual(snap.denials, ['Bash({"command":"rm -rf /"})']);
  // Non-empty text is left alone.
  const withText = parseAll(
    resultLine({
      is_error: false,
      subtype: "success",
      result: "done",
      permission_denials: [{ tool_name: "Edit" }],
    }),
  ).res;
  assert.equal(withText.text, "done");
});

test("parser: several result events (nudges) keep the last one", () => {
  const stream =
    resultLine({
      is_error: false,
      subtype: "success",
      result: "one",
      structured_output: { n: 1 },
      total_cost_usd: 0.01,
    }) +
    resultLine({
      is_error: false,
      subtype: "success",
      result: "two",
      structured_output: { n: 2 },
      total_cost_usd: 0.02,
    });
  const seen: unknown[] = [];
  const parser = createStreamParser({ pool: "subscription", onResult: (r) => seen.push(r.result) });
  parser.feed(stream);
  const res = parser.finish(OK_EXIT);
  assert.deepEqual(seen, ["one", "two"]);
  assert.equal(res.text, "two");
  assert.deepEqual(res.json, { n: 2 });
  assert.equal(res.usage.cost_usd, 0.02);
  assert.equal(parser.snapshot().results, 2);
});

test("parser: cursor falls back to the init session id when no result arrives", () => {
  const { res } = parseAll(
    '{"type":"system","subtype":"init","session_id":"init-sess","tools":["A","B"]}\n',
    {
      ...OK_EXIT,
      code: 1,
    },
  );
  assert.equal(res.cursor, "init-sess");
  assert.equal(res.exit, "error");
});

// ---- startClaude against a fake binary --------------------------------------------------------------

import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";

/** Write an executable stand-in for the claude binary that runs `body` (a shell line). */
function fakeBin(body: string): { bin: string; dir: string } {
  const dir = mkdtempSync(join(tmpdir(), "wise-fake-claude-"));
  const bin = join(dir, "claude");
  writeFileSync(bin, `#!/bin/sh\n${body}\n`);
  chmodSync(bin, 0o755);
  return { bin, dir };
}

const SLEEP_BODY = `exec "${process.execPath}" -e "setInterval(() => {}, 1000)"`;

const ECHO_SCRIPT = `
  process.stdout.write(JSON.stringify({ type: "system", subtype: "init", session_id: "fake-sess", tools: [] }) + "\\n");
  let buf = ""; let n = 0;
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (c) => {
    buf += c;
    let i;
    while ((i = buf.indexOf("\\n")) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      const msg = JSON.parse(line);
      if (msg.type !== "user") continue;
      n += 1;
      const text = msg.message.content[0].text;
      process.stdout.write(JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text: "got " + text }] } }) + "\\n");
      process.stdout.write(JSON.stringify({ type: "result", subtype: "success", is_error: false, session_id: "fake-sess", result: "got " + text, num_turns: n, total_cost_usd: n * 0.01, usage: { input_tokens: n, output_tokens: 2 * n, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } }) + "\\n");
    }
  });
  process.stdin.on("end", () => process.exit(0));
`;

/** Like ECHO_SCRIPT, but folds every line buffered so far into ONE turn (one result, queued 0). */
const COALESCE_SCRIPT = `
  process.stdout.write(JSON.stringify({ type: "system", subtype: "init", session_id: "fake-sess", tools: [] }) + "\\n");
  let buf = ""; let n = 0;
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (c) => {
    buf += c;
    const lines = buf.split("\\n"); buf = lines.pop();
    const texts = lines.filter(Boolean).map((l) => JSON.parse(l)).filter((m) => m.type === "user").map((m) => m.message.content[0].text);
    if (texts.length === 0) return;
    n += 1;
    process.stdout.write(JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text: "got " + texts.join("+") }] } }) + "\\n");
    process.stdout.write(JSON.stringify({ type: "result", subtype: "success", is_error: false, stop_reason: "tool_use", queued_turn_count: 0, session_id: "fake-sess", result: "got " + texts.join("+"), num_turns: n, total_cost_usd: 0.01, usage: { input_tokens: texts.length, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } }) + "\\n");
  });
  process.stdin.on("end", () => process.exit(0));
`;

test("startClaude: a nudge the CLI folds into the running turn (one result, queued_turn_count 0) still ends stdin", async () => {
  const scratch = mkdtempSync(join(tmpdir(), "wise-fake-claude-"));
  const script = join(scratch, "coalesce.cjs");
  writeFileSync(script, COALESCE_SCRIPT);
  const { bin, dir } = fakeBin(`exec "${process.execPath}" "${script}"`);
  const run = startClaude({ ...BASE_REQ, prompt: "ping", cwd: dir, timeout_ms: 10_000 }, () => {}, {
    bin,
    parentEnv: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" },
  });
  run.nudge("again"); // written before the fake reads: both lines land in one turn
  const res = await run.done;
  assert.equal(res.exit, "ok", res.error);
  assert.equal(res.text, "got ping+again");
  assert.equal(run.snapshot().results, 1);
  assert.throws(() => run.nudge("late"), /stdin is closed/);
});

test("startClaude: fake binary round-trips prompt and nudge over stdin", async () => {
  // The stand-in drops the claude argv and runs the echo script instead.
  const scratch = mkdtempSync(join(tmpdir(), "wise-fake-claude-"));
  const script = join(scratch, "echo.cjs");
  writeFileSync(script, ECHO_SCRIPT);
  const { bin, dir } = fakeBin(`exec "${process.execPath}" "${script}"`);

  const events: RawEvent[] = [];
  const run = startClaude({ ...BASE_REQ, prompt: "ping", cwd: dir }, (e) => events.push(e), {
    bin,
    parentEnv: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" },
  });
  assert.ok(run.pid > 0);
  // Nudge before the first result lands; the fake answers both in order.
  run.nudge("again");
  const res = await run.done;
  assert.equal(res.exit, "ok");
  assert.equal(res.text, "got again");
  assert.equal(res.cursor, "fake-sess");
  assert.deepEqual(res.usage, {
    input: 2,
    output: 4,
    cache_read: 0,
    cache_write: 0,
    cost_usd: 0.02,
    pool: "subscription",
  });
  assert.equal(run.snapshot().results, 2);
  assert.equal(run.snapshot().turns, 2);
  assert.equal(
    events.filter((e) => (e.parsed as { type?: string } | undefined)?.type === "result").length,
    2,
  );
  assert.throws(() => run.nudge("late"), /stdin is closed/);
});

test("startClaude: timeout kills the child and classifies as timeout", async () => {
  const { bin, dir } = fakeBin(SLEEP_BODY);
  const run = startClaude({ ...BASE_REQ, cwd: dir, timeout_ms: 300 }, () => {}, {
    bin,
    parentEnv: { PATH: process.env.PATH ?? "" },
  });
  const res = await run.done;
  assert.equal(res.exit, "timeout");
});

test("startClaude: kill() settles done with error", async () => {
  const { bin, dir } = fakeBin(SLEEP_BODY);
  const run = startClaude({ ...BASE_REQ, cwd: dir }, () => {}, {
    bin,
    parentEnv: { PATH: process.env.PATH ?? "" },
  });
  setTimeout(() => run.kill(), 100);
  const res = await run.done;
  assert.equal(res.exit, "error");
  assert.match(res.error ?? "", /signal SIGTERM/);
});

// ---- probeAuth and registry ------------------------------------------------------------------------------

test("probeAuth: api-key reads the key from the parent env", async () => {
  assert.deepEqual(await probeAuth("api-key", { parentEnv: { ANTHROPIC_API_KEY: "sk" } }), {
    ok: true,
    login_cmd: "export ANTHROPIC_API_KEY=...",
  });
  assert.equal((await probeAuth("api-key", { parentEnv: {} })).ok, false);
});

test("probeAuth: subscription parses `claude auth status` JSON", async () => {
  const dir = mkdtempSync(join(tmpdir(), "wise-fake-claude-"));
  const yes = join(dir, "claude-yes");
  const no = join(dir, "claude-no");
  const junk = join(dir, "claude-junk");
  writeFileSync(yes, `#!/bin/sh\necho '{"loggedIn":true,"authMethod":"claude.ai"}'\n`);
  writeFileSync(no, `#!/bin/sh\necho '{"loggedIn":false}'\nexit 1\n`);
  writeFileSync(junk, `#!/bin/sh\necho 'not json'\n`);
  for (const f of [yes, no, junk]) chmodSync(f, 0o755);
  const env = { PATH: process.env.PATH ?? "" };
  assert.deepEqual(await probeAuth("subscription", { bin: yes, parentEnv: env }), {
    ok: true,
    login_cmd: "claude auth login",
  });
  assert.equal((await probeAuth("subscription", { bin: no, parentEnv: env })).ok, false);
  assert.equal((await probeAuth("subscription", { bin: junk, parentEnv: env })).ok, false);
  assert.equal(
    (await probeAuth("subscription", { bin: "/nonexistent/claude", parentEnv: env })).ok,
    false,
  );
});

test("registry: every harness adapter is registered", () => {
  assert.equal(adapterFor("claude"), claudeAdapter);
  assert.equal(claudeAdapter.id, "claude");
  for (const h of ["claude", "codex", "cursor", "gemini", "grok"] as const) {
    assert.equal(hasAdapter(h), true, h);
    assert.equal(adapterFor(h).id, h);
  }
  assert.throws(
    () => adapterFor("nope" as never),
    (err: unknown) =>
      err instanceof AdapterError &&
      err.code === "HARNESS_UNAVAILABLE" &&
      err.message.includes("nope"),
  );
});

// ---- live smoke (WISE_LIVE=1) ------------------------------------------------------------------------------

test(
  "live: haiku with the fixture schema answers pong",
  { skip: process.env.WISE_LIVE !== "1" },
  async () => {
    const events: RawEvent[] = [];
    const res = await claudeAdapter.run(
      {
        ...BASE_REQ,
        prompt: "Reply with answer set to exactly 'pong' and n set to 42.",
        effort: "low",
        schema: SCHEMA,
        cwd: tmpdir(),
        timeout_ms: 180_000,
      },
      (e) => events.push(e),
    );
    console.log(
      "live usage",
      JSON.stringify(res.usage),
      "cursor",
      res.cursor,
      "events",
      events.length,
    );
    assert.equal(res.exit, "ok", res.error);
    assert.equal((res.json as { answer?: string }).answer, "pong");
    assert.ok(res.usage.output > 0);
    assert.ok(typeof res.cursor === "string" && res.cursor.length > 0);
  },
);

test("buildArgv: model inherit or empty omits --model", () => {
  for (const model of ["inherit", ""]) {
    const argv = buildArgv({ ...BASE_REQ, model });
    assert.equal(argv.includes("--model"), false, `model=${JSON.stringify(model)}`);
  }
  assert.ok(buildArgv({ ...BASE_REQ, model: "sonnet" }).includes("--model"));
});

test("buildArgv: add_dirs become --add-dir pairs after the permission mode", () => {
  const argv = buildArgv({ ...BASE_REQ, add_dirs: ["/tmp/run-a", "/tmp/run-b"] });
  const i = argv.indexOf("--add-dir");
  assert.ok(i > argv.indexOf("--permission-mode"));
  assert.deepEqual(argv.slice(i, i + 4), ["--add-dir", "/tmp/run-a", "--add-dir", "/tmp/run-b"]);
  assert.equal(buildArgv(BASE_REQ).includes("--add-dir"), false);
});

test("buildArgv: engine MCP server and step rules are pre-granted via --allowedTools", () => {
  const req = {
    ...BASE_REQ,
    mcp_config: { mcpServers: { "wise-engine": { command: "bash" } } },
    allowed_tools: ["Bash(git:*)", "Bash(mkdir:*)"],
  };
  const argv = buildArgv(req);
  const i = argv.indexOf("--allowedTools");
  assert.ok(i > 0);
  assert.equal(argv[i + 1], "mcp__wise-engine,Bash(git:*),Bash(mkdir:*)");
  assert.equal(buildArgv(BASE_REQ).includes("--allowedTools"), false);
});

test("buildArgv: mcp_policy engine-only keeps --strict-mcp-config", () => {
  const argv = buildArgv({ ...BASE_REQ, mcp_policy: "engine-only" });
  assert.ok(argv.includes("--strict-mcp-config"));
  assert.equal(argv.indexOf("--strict-mcp-config") < argv.indexOf("--mcp-config"), true);
  assert.equal(
    buildArgv({ ...BASE_REQ, mcp_policy: "inherit" }).includes("--strict-mcp-config"),
    false,
  );
});

test("permission protocol: initialize handshake, can_use_tool request and response shapes", () => {
  const init = JSON.parse(initializeMessage());
  assert.deepEqual(init, {
    type: "control_request",
    request_id: INIT_REQUEST_ID,
    request: { subtype: "initialize", hooks: {} },
  });
  const req = permissionRequestOf({
    type: "control_request",
    request_id: "r1",
    request: { subtype: "can_use_tool", tool_name: "WebFetch", input: { url: "https://x" } },
  });
  assert.deepEqual(req, { request_id: "r1", tool_name: "WebFetch", input: { url: "https://x" } });
  assert.equal(
    permissionRequestOf({
      type: "control_request",
      request_id: "r2",
      request: { subtype: "initialize" },
    }),
    undefined,
  );
  assert.equal(permissionRequestOf({ type: "assistant" }), undefined);
  assert.deepEqual(
    JSON.parse(permissionResponse("r1", { behavior: "allow", updatedInput: { a: 1 } })),
    {
      type: "control_response",
      response: {
        subtype: "success",
        request_id: "r1",
        response: { behavior: "allow", updatedInput: { a: 1 } },
      },
    },
  );
});

test("parser: a can_use_tool control request is decided and reported through onPermission", () => {
  const seen: string[] = [];
  const parser = createStreamParser({
    pool: "subscription",
    now: () => "T",
    onPermission: (r, d) => seen.push(`${d.behavior} ${r.tool_name} ${r.request_id}`),
  });
  const lines = [
    {
      type: "control_request",
      request_id: "a",
      request: { subtype: "can_use_tool", tool_name: "WebFetch", input: {} },
    },
    {
      type: "control_request",
      request_id: "b",
      request: { subtype: "can_use_tool", tool_name: "mcp__jira__getJiraIssue", input: {} },
    },
    {
      type: "control_request",
      request_id: "c",
      request: { subtype: "can_use_tool", tool_name: "mcp__slack__slack_send_message", input: {} },
    },
    {
      type: "control_request",
      request_id: "d",
      request: { subtype: "can_use_tool", tool_name: "Bash", input: { command: "rm -rf x" } },
    },
    { type: "control_response", response: { subtype: "success", request_id: INIT_REQUEST_ID } },
  ];
  parser.feed(lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
  assert.deepEqual(seen, [
    "allow WebFetch a",
    "allow mcp__jira__getJiraIssue b",
    "deny mcp__slack__slack_send_message c",
    "deny Bash d",
  ]);
  assert.deepEqual(parser.snapshot().permissions, [
    "allow:WebFetch",
    "allow:mcp__jira__getJiraIssue",
    "deny:mcp__slack__slack_send_message",
    "deny:Bash",
  ]);
  assert.equal(parser.snapshot().turns, 0, "control traffic is not a turn");
});

test("parser: auto-mode file mutations stay within the configured workspace roots", () => {
  const seen: string[] = [];
  const parser = createStreamParser({
    pool: "subscription",
    mode: "auto",
    workspaceRoots: ["/work/project", "/work/shared"],
    onPermission: (request, decision) =>
      seen.push(`${decision.behavior} ${request.tool_name} ${request.request_id}`),
  });
  const lines = [
    {
      type: "control_request",
      request_id: "inside",
      request: {
        subtype: "can_use_tool",
        tool_name: "Edit",
        input: { file_path: "/work/project/src/a.ts" },
      },
    },
    {
      type: "control_request",
      request_id: "added",
      request: {
        subtype: "can_use_tool",
        tool_name: "Write",
        input: { file_path: "/work/shared/a.ts" },
      },
    },
    {
      type: "control_request",
      request_id: "outside",
      request: {
        subtype: "can_use_tool",
        tool_name: "Edit",
        input: { file_path: "/tmp/a.ts" },
      },
    },
  ];
  parser.feed(lines.map((line) => JSON.stringify(line)).join("\n") + "\n");
  assert.deepEqual(seen, ["allow Edit inside", "allow Write added", "deny Edit outside"]);
});

/** A fake CLI that asks permission for WebFetch and reports what the engine answered. */
const PERMISSION_SCRIPT = `
  process.stdout.write(JSON.stringify({ type: "system", subtype: "init", session_id: "fake-sess", tools: [] }) + "\\n");
  let buf = ""; let sawInit = false; let asked = false;
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (c) => {
    buf += c;
    let i;
    while ((i = buf.indexOf("\\n")) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      if (msg.type === "control_request" && msg.request.subtype === "initialize") {
        sawInit = true;
        process.stdout.write(JSON.stringify({ type: "control_response", response: { subtype: "success", request_id: msg.request_id, response: { commands: [] } } }) + "\\n");
        continue;
      }
      if (msg.type === "user" && !asked) {
        asked = true;
        process.stdout.write(JSON.stringify({ type: "control_request", request_id: "perm-1", request: { subtype: "can_use_tool", tool_name: "WebFetch", input: { url: "https://example.com" } } }) + "\\n");
        process.stdout.write(JSON.stringify({ type: "control_request", request_id: "perm-2", request: { subtype: "can_use_tool", tool_name: "Bash", input: { command: "rm x" } } }) + "\\n");
        continue;
      }
      if (msg.type === "control_response") {
        const r = msg.response;
        const text = r.request_id + "=" + r.response.behavior + (r.response.updatedInput ? ":" + r.response.updatedInput.url : "") + (r.response.message ? ":denied" : "");
        process.stdout.write(JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text }] } }) + "\\n");
        if (r.request_id === "perm-2") {
          process.stdout.write(JSON.stringify({ type: "result", subtype: "success", is_error: false, session_id: "fake-sess", result: "init=" + sawInit, num_turns: 1, total_cost_usd: 0.01, usage: { input_tokens: 1, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 } }) + "\\n");
        }
      }
    }
  });
  process.stdin.on("end", () => process.exit(0));
`;

test("startClaude: answers the child's can_use_tool requests over stdin after the initialize handshake", async () => {
  const scratch = mkdtempSync(join(tmpdir(), "wise-fake-claude-"));
  const script = join(scratch, "perm.cjs");
  writeFileSync(script, PERMISSION_SCRIPT);
  const { bin, dir } = fakeBin(`exec "${process.execPath}" "${script}"`);
  const texts: string[] = [];
  const run = startClaude(
    { ...BASE_REQ, prompt: "go", cwd: dir, timeout_ms: 10_000 },
    (e) => {
      const p = e.parsed as
        | { type?: string; message?: { content?: { text?: string }[] } }
        | undefined;
      if (p?.type === "assistant") texts.push(p.message?.content?.[0]?.text ?? "");
    },
    { bin, parentEnv: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "" } },
  );
  const res = await run.done;
  assert.equal(res.exit, "ok", res.error);
  assert.equal(res.text, "init=true", "the initialize request reached the child first");
  assert.deepEqual(texts, ["perm-1=allow:https://example.com", "perm-2=deny:denied"]);
  assert.deepEqual(run.snapshot().permissions, ["allow:WebFetch", "deny:Bash"]);
});
