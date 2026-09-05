// `bash` step: `bash -c <run>` in the run cwd under the P6 clean env, wall-clock timeout,
// whole trimmed stdout as the single declared output, last stdout line as the verdict.

import { cleanEnv, spawnClean } from "../adapters/spawn.ts";
import type { Env } from "../paths.ts";
import type { BashStep } from "../types.ts";
import { DEFAULT_STEP_TIMEOUT_MS, headline } from "./agent.ts";

const STDERR_TAIL = 800;
const STDOUT_CAP = 1024 * 1024;

export type BashResult = {
  ok: boolean;
  code: number | null;
  timedOut: boolean;
  stdout: string;
  stderr: string;
  verdict: string;
  error?: string;
  outputs: Record<string, string>;
};

export type BashHandle = {
  pid: number;
  kill: (signal?: NodeJS.Signals) => void;
  result: Promise<BashResult>;
};

export type BashStepOpts = {
  cwd: string;
  parentEnv?: Env;
  defaultTimeoutMs?: number;
};

function lastLine(text: string): string {
  const lines = text
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  return lines.at(-1) ?? "";
}

/** Start the step; `kill` is available at once, `result` settles on exit. */
export function startBashStep(step: BashStep, opts: BashStepOpts): BashHandle {
  const timeoutMs =
    step.timeout !== undefined
      ? step.timeout * 1000
      : (opts.defaultTimeoutMs ?? DEFAULT_STEP_TIMEOUT_MS);
  const env = cleanEnv(opts.parentEnv ? { parent: opts.parentEnv } : {});
  const proc = spawnClean("bash", ["-c", step.run], { cwd: opts.cwd, env, timeoutMs });
  let stdout = "";
  proc.stdout.on("data", (chunk: string) => {
    if (stdout.length < STDOUT_CAP) stdout += chunk.slice(0, STDOUT_CAP - stdout.length);
  });
  proc.stdin.end();
  const result = proc.exited.then((exit): BashResult => {
    const trimmed = stdout.trim();
    const ok = exit.code === 0 && !exit.timedOut && exit.error === undefined;
    const outputs: Record<string, string> = {};
    const name = step.outputs?.[0];
    if (ok && name !== undefined) outputs[name] = trimmed;
    if (ok) {
      return {
        ok,
        code: exit.code,
        timedOut: false,
        stdout: trimmed,
        stderr: exit.stderr,
        verdict: headline(lastLine(trimmed)) || "ok",
        outputs,
      };
    }
    const tail = exit.stderr.trim().slice(-STDERR_TAIL);
    const error = exit.timedOut
      ? `timed out after ${timeoutMs} ms`
      : exit.error !== undefined
        ? `spawn failed: ${exit.error}`
        : tail || `exit code ${String(exit.code)}${exit.signal ? ` (${exit.signal})` : ""}`;
    return {
      ok,
      code: exit.code,
      timedOut: exit.timedOut,
      stdout: trimmed,
      stderr: exit.stderr,
      verdict: headline(`failed: ${lastLine(error) || error}`),
      error,
      outputs,
    };
  });
  return { pid: proc.pid, kill: proc.kill, result };
}

export function runBashStep(step: BashStep, opts: BashStepOpts): Promise<BashResult> {
  return startBashStep(step, opts).result;
}
