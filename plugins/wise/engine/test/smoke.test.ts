import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { buildId, pluginVersion, runtimeName } from "../src/index.ts";

const ENGINE = join(dirname(fileURLToPath(import.meta.url)), "..");

test("pluginVersion reads plugin.json", () => {
  assert.match(pluginVersion(), /^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$/);
  assert.match(buildId(), /^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?\+[0-9a-f]{10}$/);
  assert.equal(buildId(), buildId());
});

test("runtimeName is bun or node", () => {
  assert.ok(["bun", "node"].includes(runtimeName()));
});

test("engine.sh version runs on the current runtime", () => {
  const out = execFileSync(join(ENGINE, "engine.sh"), ["version"], { encoding: "utf8" });
  // `<plugin version>+<10-hex source fingerprint>`: the daemon handshake id.
  assert.match(out, /^wise-engine \d+\.\d+\.\d+(-[0-9A-Za-z.]+)?\+[0-9a-f]{10} \((bun|node) /);
});

test("unknown command exits 64", () => {
  assert.throws(
    () => execFileSync(join(ENGINE, "engine.sh"), ["bogus"], { encoding: "utf8", stdio: "pipe" }),
    (e: unknown) => (e as { status?: number }).status === 64,
  );
});
