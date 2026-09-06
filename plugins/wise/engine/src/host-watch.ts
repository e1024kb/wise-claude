// A stdio MCP server lives exactly as long as its host. The SDK's stdio transport only listens
// for `data` and `error` on stdin, so when the host dies the server outlives it as an orphan
// (ppid 1) and, on Bun, spins the event loop on the dead pipe at full CPU. This watch closes the
// transport on stdin EOF, on a stdout write error (EPIPE) and when the parent is gone.

import type { EventEmitter } from "node:events";

export type HostWatchOpts = {
  stdin: EventEmitter;
  stdout: EventEmitter;
  /** Current parent pid; `1` (launchd / init) means the host exited. */
  ppid: () => number;
  /** Parent poll period, default 5000. */
  intervalMs?: number;
  onGone: (reason: string) => void;
};

/** Start watching; returns the stop function (idempotent). `onGone` fires at most once. */
export function watchHost(opts: HostWatchOpts): () => void {
  let fired = false;
  let stopped = false;
  const gone = (reason: string): void => {
    if (fired) return;
    fired = true;
    stop();
    opts.onGone(reason);
  };
  const onEnd = (): void => gone("stdin closed");
  const onStdoutError = (): void => gone("stdout error");
  const timer = setInterval(() => {
    if (opts.ppid() === 1) gone("host exited");
  }, opts.intervalMs ?? 5000);
  timer.unref?.();
  opts.stdin.on("end", onEnd);
  opts.stdin.on("close", onEnd);
  opts.stdout.on("error", onStdoutError);
  const stop = (): void => {
    if (stopped) return;
    stopped = true;
    clearInterval(timer);
    opts.stdin.off("end", onEnd);
    opts.stdin.off("close", onEnd);
    opts.stdout.off("error", onStdoutError);
  };
  return stop;
}

/** Exit soon after the transport closed, in case the runtime keeps the loop alive on a dead pipe. */
export function exitAfterClose(
  code: number,
  exit: (code: number) => void = (c) => process.exit(c),
): void {
  const t = setTimeout(() => exit(code), 50);
  t.unref?.();
}
