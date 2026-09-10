import { test } from "node:test";
import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  AUTH_RE,
  buildArgv,
  childEnv,
  composePrompt,
  createStreamParser,
  effortMap,
  MODE_ARGS,
  probeAuth,
  RATE_LIMIT_RE,
  startCursor,
} from "../src/adapters/cursor.ts";
import type { SpawnExit } from "../src/adapters/spawn.ts";
import type { RunReq } from "../src/types.ts";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "fixtures", "adapters");
const STREAM = readFileSync(join(FIXTURES, "cursor", "schema.stream.ndjson"), "utf8");
const SCHEMA = JSON.parse(readFileSync(join(FIXTURES, "schema.json"), "utf8")) as Record<
  string,
  unknown
>;

const BASE_REQ: RunReq = {
  prompt: "ping",
  model: "composer-2.5",
  cwd: "/tmp/work",
  mode: "approval-required",
  timeout_ms: 60_000,
  auth: "subscription",
};
const OK_EXIT: SpawnExit = { code: 0, signal: null, timedOut: false, stderr: "" };

function parseResult(stream: string, exit: SpawnExit, expectJson = false) {
  const parser = createStreamParser({ pool: "api-key", expectJson });
  parser.feed(stream);
  return parser.finish(exit);
}

function failedResult(message: string): string {
  return `${JSON.stringify({ type: "result", subtype: "error", is_error: true, error: message })}\n`;
}

test("buildArgv: headless stream JSON, explicit workspace, and permission mappings", () => {
  assert.deepEqual(buildArgv(BASE_REQ), [
    "--print",
    "--output-format",
    "stream-json",
    "--stream-partial-output",
    "--trust",
    "--workspace",
    "/tmp/work",
    "--mode",
    "ask",
    "--sandbox",
    "enabled",
    "--model",
    "composer-2.5",
  ]);
  assert.deepEqual(MODE_ARGS.auto, ["--force", "--sandbox", "enabled"]);
  assert.deepEqual(MODE_ARGS["full-access"], [
    "--force",
    "--sandbox",
    "disabled",
    "--approve-mcps",
  ]);
});

test("buildArgv: resume, add-dir, inherit, system/schema prompt, and no effort flag", () => {
  const argv = buildArgv({
    ...BASE_REQ,
    model: "inherit",
    effort: "high",
    resume: "s-1",
    add_dirs: ["/tmp/run"],
  });
  assert.equal(argv.includes("--model"), false);
  assert.deepEqual(argv.slice(argv.indexOf("--resume")), [
    "--resume",
    "s-1",
    "--add-dir",
    "/tmp/run",
  ]);
  assert.equal(buildArgv({ ...BASE_REQ, resume: { id: 1 } }).includes("--resume"), false);
  const prompt = composePrompt({ prompt: "p", system: "s", schema: SCHEMA });
  assert.ok(prompt.startsWith("s\n\np\n\nRespond with only a JSON object"));
  assert.ok(prompt.endsWith(JSON.stringify(SCHEMA)));
  assert.equal(effortMap("high"), undefined);
});

test("childEnv: API key is opt-in and Cursor config survives cleaning", () => {
  const parent = {
    HOME: "/h",
    PATH: "/bin",
    CURSOR_API_KEY: "secret",
    CURSOR_CONFIG_DIR: "/cursor",
    CLAUDECODE: "1",
  };
  assert.deepEqual(childEnv({ auth: "subscription" }, parent), {
    HOME: "/h",
    PATH: "/bin",
    CURSOR_CONFIG_DIR: "/cursor",
  });
  assert.deepEqual(childEnv({ auth: "api-key" }, parent), {
    HOME: "/h",
    PATH: "/bin",
    CURSOR_API_KEY: "secret",
    CURSOR_CONFIG_DIR: "/cursor",
  });
});

test("parser: Cursor stream yields structured output, session cursor, model, and snapshots", () => {
  const parser = createStreamParser({
    pool: "subscription",
    expectJson: true,
    now: () => "T",
  });
  const events = parser.feed(STREAM);
  const res = parser.finish(OK_EXIT);
  assert.equal(events.length, 5);
  assert.ok(events.every((event) => event.harness === "cursor" && event.ts === "T"));
  assert.equal(res.exit, "ok");
  assert.deepEqual(res.json, { answer: "pong", n: 42 });
  assert.equal(res.cursor, "cursor-session-1");
  assert.equal(res.model, "composer-2.5");
  assert.deepEqual(res.usage, {
    input: 0,
    output: 0,
    cache_read: 0,
    cache_write: 0,
    pool: "subscription",
  });
  assert.deepEqual(parser.snapshot(), {
    session_id: "cursor-session-1",
    model: "composer-2.5",
    assistant_events: 2,
    tool_calls: 1,
    results: 1,
    errors: [],
  });
});

test("parser: classifies auth, rate limits, timeouts, and malformed schema output", () => {
  assert.equal(parseResult(failedResult("not authenticated"), OK_EXIT).exit, "auth");
  assert.equal(parseResult(failedResult("rate limit exceeded"), OK_EXIT).exit, "rate_limited");
  assert.equal(
    parseResult("", { code: null, signal: "SIGTERM", timedOut: true, stderr: "slow" }).exit,
    "timeout",
  );
  assert.equal(
    parseResult(
      `${JSON.stringify({ type: "result", subtype: "success", result: "not json" })}\n`,
      OK_EXIT,
      true,
    ).exit,
    "error",
  );
  assert.match("429 too many requests", RATE_LIMIT_RE);
  assert.match("agent login required", AUTH_RE);
});

test("startCursor: prompt travels on stdin and a fake binary round-trips a result", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cursor-adapter-"));
  const bin = join(dir, "cursor-agent");
  await import("node:fs/promises").then(({ writeFile }) =>
    writeFile(
      bin,
      `#!${process.execPath}
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => input += chunk);
process.stdin.on("end", () => {
  const event = {type:"result", subtype:"success", is_error:false, session_id:"fake-s", result:JSON.stringify({prompt:input, argv:process.argv.slice(2)})};
  process.stdout.write(JSON.stringify(event) + "\\n");
});
`,
    ),
  );
  chmodSync(bin, 0o755);
  const run = startCursor({ ...BASE_REQ, cwd: dir, schema: SCHEMA }, () => {}, {
    bin,
    parentEnv: { PATH: process.env.PATH ?? "" },
  });
  const res = await run.done;
  assert.equal(res.exit, "ok");
  const value = res.json as { prompt: string; argv: string[] };
  assert.equal(value.prompt, composePrompt({ ...BASE_REQ, schema: SCHEMA }));
  assert.ok(value.argv.includes("stream-json"));
});

test("probeAuth: API key and subscription status JSON", async () => {
  assert.equal((await probeAuth("api-key", { parentEnv: {} })).ok, false);
  assert.equal((await probeAuth("api-key", { parentEnv: { CURSOR_API_KEY: "key" } })).ok, true);
  const dir = mkdtempSync(join(tmpdir(), "cursor-auth-"));
  const yes = join(dir, "yes");
  const no = join(dir, "no");
  const { writeFile } = await import("node:fs/promises");
  await writeFile(yes, "#!/bin/sh\nprintf '%s\\n' '{\"isAuthenticated\":true}'\n");
  await writeFile(no, "#!/bin/sh\nprintf '%s\\n' '{\"isAuthenticated\":false}'\n");
  chmodSync(yes, 0o755);
  chmodSync(no, 0o755);
  assert.equal((await probeAuth("subscription", { bin: yes, parentEnv: {} })).ok, true);
  assert.equal((await probeAuth("subscription", { bin: no, parentEnv: {} })).ok, false);
});
