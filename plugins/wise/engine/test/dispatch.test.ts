import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { cmdDispatch, cmdModels } from "../src/dispatch.ts";
import type { DispatchResult } from "../src/dispatch.ts";
import { main } from "../src/cli.ts";
import type { Adapter, Effort, Harness, RunReq, RunRes } from "../src/types.ts";

function io(): {
  out: (s: string) => void;
  err: (s: string) => void;
  o: () => string;
  e: () => string;
} {
  let out = "";
  let err = "";
  return {
    out: (s) => (out += s),
    err: (s) => (err += s),
    o: () => out,
    e: () => err,
  };
}

function fakeAdapter(harness: Harness, res: Partial<RunRes>, seen: RunReq[]): Adapter {
  return {
    id: harness,
    probeAuth: () => Promise.resolve({ ok: true }),
    run: (req) => {
      seen.push(req);
      return Promise.resolve({
        text: "all green, merged",
        usage: { input: 1, output: 2, cache_read: 0, cache_write: 0 },
        exit: "ok",
        ...res,
      } as RunRes);
    },
    effortMap: (e: Effort) => e,
  };
}

test("models: JSON rows carry harness, id and efforts; unknown harness is exit 2", () => {
  const t = io();
  assert.equal(cmdModels([], {}, t), 0);
  const rows = JSON.parse(t.o()) as { harness: string; id: string; efforts: string[] }[];
  assert.ok(rows.some((r) => r.harness === "claude" && r.id === "claude-fable-5-1"));
  assert.ok(rows.some((r) => r.harness === "codex" && r.efforts.includes("high")));
  const grok = rows.filter((r) => r.harness === "grok");
  assert.ok(grok.length > 0 && grok.every((r) => r.efforts.length === 0));

  const bad = io();
  assert.equal(cmdModels(["nope"], {}, bad), 2);
  assert.match(bad.e(), /unknown harness nope/);
});

test("models via the cli main, --text one row per line", async () => {
  let out = "";
  const code = await main(["models", "codex", "--text"], {
    out: (s) => (out += s),
    err: () => {},
    env: {},
  });
  assert.equal(code, 0);
  assert.match(out, /codex\tgpt-6-astra\t/);
});

test("dispatch: runs the chosen harness with model, effort and mode; prints one JSON result", async () => {
  const dir = mkdtempSync(join(tmpdir(), "wise-dispatch-"));
  const promptFile = join(dir, "prompt.md");
  writeFileSync(promptFile, "watch the PR");
  const seen: RunReq[] = [];
  const t = io();
  const code = await cmdDispatch(
    {
      harness: "codex",
      model: "gpt-6-astra",
      effort: "high",
      mode: "full-access",
      "prompt-file": promptFile,
      cwd: dir,
      "timeout-s": "60",
      "allowed-tools": "Bash(git:*),Bash(gh:*)",
    },
    t,
    (h) => fakeAdapter(h, {}, seen),
  );
  assert.equal(code, 0, t.e());
  const req = seen[0] as RunReq;
  assert.equal(req.prompt, "watch the PR");
  assert.equal(req.model, "gpt-6-astra");
  assert.equal(req.effort, "high");
  assert.equal(req.mode, "full-access");
  assert.equal(req.timeout_ms, 60_000);
  assert.deepEqual(req.allowed_tools, ["Bash(git:*)", "Bash(gh:*)"]);
  const j = JSON.parse(t.o()) as DispatchResult;
  assert.equal(j.ok, true);
  assert.equal(j.harness, "codex");
  assert.equal(j.verdict, "all green, merged");
});

test("dispatch: an effort the model does not list is an error, not a silent clamp", async () => {
  const t = io();
  const code = await cmdDispatch({ harness: "grok", prompt: "p", effort: "high" }, t, (h) =>
    fakeAdapter(h, {}, []),
  );
  assert.equal(code, 64);
  assert.match(t.e(), /grok-4\.6 takes no effort flag/);
});

test("dispatch: off-catalog model passes through with a warning; failed child is exit 1", async () => {
  const t = io();
  const seen: RunReq[] = [];
  const code = await cmdDispatch(
    { harness: "claude", model: "claude-nova-9", prompt: "p" },
    t,
    (h) => fakeAdapter(h, { exit: "timeout", error: "deadline" }, seen),
  );
  assert.equal(code, 1);
  assert.equal((seen[0] as RunReq).model, "claude-nova-9");
  const j = JSON.parse(t.o()) as DispatchResult;
  assert.equal(j.ok, false);
  assert.equal(j.exit, "timeout");
  assert.ok(j.warnings.some((w) => w.includes("not in the catalog")));
});

test("dispatch: missing harness / prompt are usage errors", async () => {
  const a = io();
  assert.equal(await cmdDispatch({ harness: "vim" }, a), 64);
  const b = io();
  assert.equal(await cmdDispatch({ harness: "claude" }, b), 64);
  assert.match(b.e(), /--prompt-file/);
});
