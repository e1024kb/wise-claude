import { test } from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { exitAfterClose, watchHost } from "../src/host-watch.ts";

function harness(ppid: () => number) {
  const stdin = new EventEmitter();
  const stdout = new EventEmitter();
  const reasons: string[] = [];
  const stop = watchHost({ stdin, stdout, ppid, intervalMs: 5, onGone: (r) => reasons.push(r) });
  return { stdin, stdout, reasons, stop };
}

test("watchHost: stdin end closes once and removes the listeners", () => {
  const h = harness(() => 42);
  h.stdin.emit("end");
  h.stdin.emit("close");
  assert.deepEqual(h.reasons, ["stdin closed"]);
  assert.equal(h.stdin.listenerCount("end"), 0);
  assert.equal(h.stdout.listenerCount("error"), 0);
});

test("watchHost: stdout error is a host loss", () => {
  const h = harness(() => 42);
  h.stdout.emit("error", new Error("EPIPE"));
  assert.deepEqual(h.reasons, ["stdout error"]);
  h.stop();
});

test("watchHost: an orphaned process (ppid 1) is detected by the poll", async () => {
  let ppid = 42;
  const h = harness(() => ppid);
  await new Promise((r) => setTimeout(r, 20));
  assert.deepEqual(h.reasons, []);
  ppid = 1;
  await new Promise((r) => setTimeout(r, 30));
  assert.deepEqual(h.reasons, ["host exited"]);
});

test("watchHost: stop before any signal means onGone never fires", () => {
  const h = harness(() => 42);
  h.stop();
  h.stdin.emit("end");
  assert.deepEqual(h.reasons, []);
});

test("exitAfterClose calls exit with the code after a short delay", async () => {
  const codes: number[] = [];
  exitAfterClose(3, (c) => codes.push(c));
  assert.deepEqual(codes, []);
  await new Promise((r) => setTimeout(r, 80));
  assert.deepEqual(codes, [3]);
});
