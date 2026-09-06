import { test } from "node:test";
import assert from "node:assert/strict";
import { tmpdir } from "node:os";
import {
  cleanEnv,
  createLineSplitter,
  isBlockedVar,
  PASSTHROUGH_VARS,
  spawnClean,
} from "../src/adapters/spawn.ts";

const NODE = process.execPath;
const CWD = tmpdir();

function collect(stream: NodeJS.ReadableStream): Promise<string> {
  return new Promise((resolve) => {
    let out = "";
    stream.on("data", (c: string) => (out += c));
    stream.on("end", () => resolve(out));
  });
}

// ---- cleanEnv ------------------------------------------------------------------------------------

test("cleanEnv: allowlist, XDG_*, keep, secrets, blocked names, extra on top", () => {
  const parent = {
    HOME: "/h",
    PATH: "/bin",
    LANG: "C",
    LC_ALL: "C.UTF-8",
    TERM: "xterm",
    TMPDIR: "/t",
    SHELL: "/bin/zsh",
    USER: "u",
    XDG_DATA_HOME: "/x/data",
    XDG_CONFIG_HOME: "/x/cfg",
    CLAUDE_CONFIG_DIR: "/cc",
    ANTHROPIC_API_KEY: "sk-secret",
    CLAUDECODE: "1",
    CLAUDE_CODE_ENTRYPOINT: "cli",
    CLAUDE_CODE_SESSION_ID: "s1",
    CLAUDE_SESSION_ID: "s2",
    CLAUDE_FOO_SESSION_BAR: "s3",
    AWS_SECRET: "nope",
    EDITOR: "vim",
    SSH_AUTH_SOCK: "/run/agent.sock",
    GIT_SSH_COMMAND: "ssh -i /k",
    HTTPS_PROXY: "http://proxy:3128",
    GH_TOKEN: "gho_secret",
  };
  const table: {
    name: string;
    opts: Parameters<typeof cleanEnv>[0];
    expect: Record<string, string>;
  }[] = [
    {
      name: "default: passthrough + XDG only",
      opts: { parent },
      expect: {
        HOME: "/h",
        PATH: "/bin",
        LANG: "C",
        LC_ALL: "C.UTF-8",
        TERM: "xterm",
        TMPDIR: "/t",
        SHELL: "/bin/zsh",
        USER: "u",
        XDG_DATA_HOME: "/x/data",
        XDG_CONFIG_HOME: "/x/cfg",
        SSH_AUTH_SOCK: "/run/agent.sock",
        GIT_SSH_COMMAND: "ssh -i /k",
        HTTPS_PROXY: "http://proxy:3128",
      },
    },
    {
      name: "keep copies a vendor config dir",
      opts: { parent: { HOME: "/h", CLAUDE_CONFIG_DIR: "/cc" }, keep: ["CLAUDE_CONFIG_DIR"] },
      expect: { HOME: "/h", CLAUDE_CONFIG_DIR: "/cc" },
    },
    {
      name: "keep of an unset var adds nothing",
      opts: { parent: { HOME: "/h" }, keep: ["CLAUDE_CONFIG_DIR"] },
      expect: { HOME: "/h" },
    },
    {
      name: "secrets copy the key",
      opts: { parent: { HOME: "/h", ANTHROPIC_API_KEY: "k" }, secrets: ["ANTHROPIC_API_KEY"] },
      expect: { HOME: "/h", ANTHROPIC_API_KEY: "k" },
    },
    {
      name: "blocked names never pass, even via keep",
      opts: {
        parent,
        keep: [
          "CLAUDECODE",
          "CLAUDE_CODE_ENTRYPOINT",
          "CLAUDE_CODE_SESSION_ID",
          "CLAUDE_SESSION_ID",
        ],
      },
      expect: {
        HOME: "/h",
        PATH: "/bin",
        LANG: "C",
        LC_ALL: "C.UTF-8",
        TERM: "xterm",
        TMPDIR: "/t",
        SHELL: "/bin/zsh",
        USER: "u",
        SSH_AUTH_SOCK: "/run/agent.sock",
        GIT_SSH_COMMAND: "ssh -i /k",
        HTTPS_PROXY: "http://proxy:3128",
        XDG_DATA_HOME: "/x/data",
        XDG_CONFIG_HOME: "/x/cfg",
      },
    },
    {
      name: "extra applies last and wins",
      opts: {
        parent: { HOME: "/h", PATH: "/bin" },
        extra: { PATH: "/override", WISE_STEP_TOKEN: "t" },
      },
      expect: { HOME: "/h", PATH: "/override", WISE_STEP_TOKEN: "t" },
    },
  ];
  for (const row of table) {
    assert.deepEqual(cleanEnv(row.opts), row.expect, row.name);
  }
  assert.equal(PASSTHROUGH_VARS.length, 25);
  assert.ok(PASSTHROUGH_VARS.includes("SSH_AUTH_SOCK"), "git over ssh needs the agent socket");
  assert.ok(!(PASSTHROUGH_VARS as readonly string[]).includes("GH_TOKEN"), "tokens never pass");
});

test("isBlockedVar", () => {
  const blocked = [
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_X_SESSION_Y",
  ];
  const allowed = ["CLAUDE_CONFIG_DIR", "HOME", "ANTHROPIC_API_KEY", "CLAUDE", "XDG_DATA_HOME"];
  for (const n of blocked) assert.equal(isBlockedVar(n), true, n);
  for (const n of allowed) assert.equal(isBlockedVar(n), false, n);
});

test("cleanEnv defaults to process.env", () => {
  const env = cleanEnv();
  assert.equal(env.PATH, process.env.PATH);
  assert.equal("CLAUDECODE" in env, false);
});

// ---- createLineSplitter --------------------------------------------------------------------------

test("createLineSplitter reassembles partial lines and strips CR", () => {
  const s = createLineSplitter();
  assert.deepEqual(s.feed('{"a":'), []);
  assert.deepEqual(s.feed('1}\n{"b":2}\r\n{"c'), ['{"a":1}', '{"b":2}']);
  assert.deepEqual(s.feed('":3}'), []);
  assert.deepEqual(s.finish(), ['{"c":3}']);
  assert.deepEqual(s.finish(), []);
});

// ---- spawnClean --------------------------------------------------------------------------------------

test("spawnClean: exact env, cwd, stdout NDJSON, exit code", async () => {
  const script = `process.stdout.write(JSON.stringify({ env: process.env, cwd: process.cwd() }) + "\\n"); process.exit(3);`;
  const p = spawnClean(NODE, ["-e", script], {
    cwd: CWD,
    env: { PATH: process.env.PATH ?? "", WISE_X: "1" },
  });
  assert.ok(p.pid > 0);
  p.stdin.end();
  const [out, exit] = await Promise.all([collect(p.stdout), p.exited]);
  const parsed = JSON.parse(out.trim()) as { env: Record<string, string>; cwd: string };
  // macOS libSystem adds __CF_USER_TEXT_ENCODING to every spawned process; it is not ours.
  const keys = Object.keys(parsed.env).filter((k) => !k.startsWith("__CF_"));
  assert.deepEqual(keys.toSorted(), ["PATH", "WISE_X"]);
  assert.equal(parsed.cwd, (await import("node:fs")).realpathSync(CWD));
  assert.equal(exit.code, 3);
  assert.equal(exit.signal, null);
  assert.equal(exit.timedOut, false);
  assert.equal(exit.error, undefined);
});

test("spawnClean: stderr is captured and capped", async () => {
  const p = spawnClean(NODE, ["-e", 'process.stderr.write("x".repeat(100)); process.exit(1);'], {
    cwd: CWD,
    env: { PATH: process.env.PATH ?? "" },
    stderrCap: 10,
  });
  p.stdin.end();
  const exit = await p.exited;
  assert.equal(exit.code, 1);
  assert.equal(exit.stderr, "xxxxxxxxxx");
});

test("spawnClean: stdin is delivered to the child", async () => {
  const p = spawnClean(
    NODE,
    [
      "-e",
      'let d="";process.stdin.on("data",c=>d+=c);process.stdin.on("end",()=>{process.stdout.write(d.toUpperCase());});',
    ],
    { cwd: CWD, env: { PATH: process.env.PATH ?? "" } },
  );
  p.stdin.write("hello\n");
  p.stdin.end();
  const [out, exit] = await Promise.all([collect(p.stdout), p.exited]);
  assert.equal(out, "HELLO\n");
  assert.equal(exit.code, 0);
});

test("spawnClean: timeout sends SIGTERM to the group and marks timedOut", async () => {
  const p = spawnClean(NODE, ["-e", "setInterval(() => {}, 1000);"], {
    cwd: CWD,
    env: { PATH: process.env.PATH ?? "" },
    timeoutMs: 200,
    killGraceMs: 2000,
  });
  const exit = await p.exited;
  assert.equal(exit.timedOut, true);
  assert.equal(exit.signal, "SIGTERM");
});

test("spawnClean: SIGKILL follows when the child ignores SIGTERM", async () => {
  const p = spawnClean(
    NODE,
    ["-e", 'process.on("SIGTERM", () => {}); setInterval(() => {}, 1000);'],
    {
      cwd: CWD,
      env: { PATH: process.env.PATH ?? "" },
      timeoutMs: 200,
      killGraceMs: 200,
    },
  );
  const exit = await p.exited;
  assert.equal(exit.timedOut, true);
  assert.equal(exit.signal, "SIGKILL");
});

test("spawnClean: kill() signals the whole process group", async () => {
  // The child spawns a grandchild in the same group; both must die with one kill().
  const script = `
    const { spawn } = require("node:child_process");
    const g = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000);"], { stdio: "ignore" });
    process.stdout.write(String(g.pid) + "\\n");
    setInterval(() => {}, 1000);
  `;
  const p = spawnClean(NODE, ["-e", script], { cwd: CWD, env: { PATH: process.env.PATH ?? "" } });
  const grandchildPid = await new Promise<number>((resolve) => {
    p.stdout.once("data", (c: string) => resolve(Number(c.trim())));
  });
  p.kill("SIGTERM");
  const exit = await p.exited;
  assert.equal(exit.signal, "SIGTERM");
  // Give the grandchild a moment to die, then probe it.
  const alive = (): boolean => {
    try {
      process.kill(grandchildPid, 0);
      return true;
    } catch {
      return false;
    }
  };
  for (let i = 0; i < 20 && alive(); i++) await new Promise((r) => setTimeout(r, 50));
  assert.equal(alive(), false, "grandchild should be gone");
});

test("spawnClean: missing binary settles with error", async () => {
  const p = spawnClean("/nonexistent/wise-no-such-binary", [], {
    cwd: CWD,
    env: { PATH: process.env.PATH ?? "" },
  });
  const exit = await p.exited;
  assert.equal(exit.code, null);
  assert.match(exit.error ?? "", /ENOENT/);
});
