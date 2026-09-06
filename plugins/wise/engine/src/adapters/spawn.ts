// Clean-env child spawning shared by every harness adapter (research-ts-engine.md P6).
// The child starts from an empty environment, gets an allowlist of parent variables,
// runs in its own process group so the whole tree can be signalled, and is killed
// on timeout (SIGTERM, then SIGKILL after a grace period).

import { spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import type { Readable, Writable } from "node:stream";
import type { Env } from "../paths.ts";

/**
 * Parent variables every child may inherit: the shell basics, then what git, ssh, gpg, gh and
 * the network stack need to reach a remote from a clean env (without `SSH_AUTH_SOCK` every
 * `git@github.com` call fails with "Permission denied (publickey)"; the phase runners hit that
 * first, at `claim`). Vendor tokens stay out: gh reads its own keyring through HOME.
 */
export const PASSTHROUGH_VARS = [
  "HOME",
  "PATH",
  "LANG",
  "LC_ALL",
  "TERM",
  "TMPDIR",
  "SHELL",
  "USER",
  // git over ssh, ssh signing, gpg signing
  "SSH_AUTH_SOCK",
  "SSH_AGENT_PID",
  "GIT_SSH",
  "GIT_SSH_COMMAND",
  "GIT_CONFIG_GLOBAL",
  "GNUPGHOME",
  "GPG_TTY",
  // gh host and config dir (never GH_TOKEN)
  "GH_HOST",
  "GH_CONFIG_DIR",
  // proxies and trust store
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "NO_PROXY",
  "http_proxy",
  "https_proxy",
  "no_proxy",
  "SSL_CERT_FILE",
  "SSL_CERT_DIR",
] as const;

/** Variables no child may ever inherit, whatever the allowlist says. */
export function isBlockedVar(name: string): boolean {
  return name === "CLAUDECODE" || name.startsWith("CLAUDE_CODE_") || /^CLAUDE_.*SESSION/.test(name);
}

export type CleanEnvOpts = {
  /** Parent environment; defaults to `process.env`. */
  parent?: Env;
  /** Extra parent variables to copy when set (vendor config dirs, e.g. `CLAUDE_CONFIG_DIR`). */
  keep?: readonly string[];
  /** Vendor key variables copied from the parent (only under `api-key`). */
  secrets?: readonly string[];
  /** Applied last, on top of everything copied (the request's `env`). */
  extra?: Record<string, string>;
};

/** Build the child environment from an empty object (P6 clean env). */
export function cleanEnv(opts: CleanEnvOpts = {}): Record<string, string> {
  const parent = opts.parent ?? process.env;
  const out: Record<string, string> = {};
  const copy = (name: string): void => {
    if (isBlockedVar(name)) return;
    const value = parent[name];
    if (value !== undefined) out[name] = value;
  };
  for (const name of PASSTHROUGH_VARS) copy(name);
  for (const name of Object.keys(parent)) if (name.startsWith("XDG_")) copy(name);
  for (const name of opts.keep ?? []) copy(name);
  for (const name of opts.secrets ?? []) copy(name);
  Object.assign(out, opts.extra ?? {});
  return out;
}

export type SpawnOpts = {
  cwd: string;
  env: Record<string, string>;
  /** Wall clock; `0` or absent disables the timer. */
  timeoutMs?: number;
  /** SIGTERM to SIGKILL grace, default 5000. */
  killGraceMs?: number;
  /** Cap on retained stderr, default 64 KiB. */
  stderrCap?: number;
};

export type SpawnExit = {
  code: number | null;
  signal: NodeJS.Signals | null;
  timedOut: boolean;
  /** Retained stderr (capped). */
  stderr: string;
  /** Set when the process could not be started at all (ENOENT, EACCES). */
  error?: string;
};

export type Spawned = {
  pid: number;
  child: ChildProcess;
  stdin: Writable;
  stdout: Readable;
  /** Settles when the process has exited and its stdio has closed. */
  exited: Promise<SpawnExit>;
  /** Signal the child's whole process group. */
  kill(signal?: NodeJS.Signals): void;
};

const DEFAULT_GRACE_MS = 5_000;
const DEFAULT_STDERR_CAP = 64 * 1024;

/** Signal a whole process group, falling back to the child alone when the group is gone. */
export function killGroup(child: ChildProcess, signal: NodeJS.Signals = "SIGTERM"): void {
  const pid = child.pid;
  if (pid === undefined) return;
  if (process.platform !== "win32") {
    try {
      process.kill(-pid, signal);
      return;
    } catch {
      // Group already gone or not ours: fall through to the direct kill.
    }
  }
  try {
    child.kill(signal);
  } catch {
    // Already exited.
  }
}

/** Spawn `cmd` detached in its own process group with exactly `opts.env`. */
export function spawnClean(cmd: string, args: readonly string[], opts: SpawnOpts): Spawned {
  const child = spawn(cmd, args, {
    cwd: opts.cwd,
    env: opts.env,
    detached: process.platform !== "win32",
    stdio: ["pipe", "pipe", "pipe"],
  });
  const stdin = child.stdin as Writable;
  const stdout = child.stdout as Readable;
  const stderrStream = child.stderr as Readable;
  stdout.setEncoding("utf8");
  stderrStream.setEncoding("utf8");

  const cap = opts.stderrCap ?? DEFAULT_STDERR_CAP;
  let stderr = "";
  stderrStream.on("data", (chunk: string) => {
    if (stderr.length < cap) stderr += chunk.slice(0, cap - stderr.length);
  });
  // A closed child stdin (or one the child never reads) must not crash the parent.
  stdin.on("error", () => {});

  let timedOut = false;
  let spawnError: string | undefined;
  let termTimer: NodeJS.Timeout | undefined;
  let killTimer: NodeJS.Timeout | undefined;
  const kill = (signal: NodeJS.Signals = "SIGTERM"): void => killGroup(child, signal);

  if (opts.timeoutMs && opts.timeoutMs > 0) {
    termTimer = setTimeout(() => {
      timedOut = true;
      kill("SIGTERM");
      killTimer = setTimeout(() => kill("SIGKILL"), opts.killGraceMs ?? DEFAULT_GRACE_MS);
    }, opts.timeoutMs);
  }

  const exited = new Promise<SpawnExit>((resolve) => {
    const settle = (code: number | null, signal: NodeJS.Signals | null): void => {
      if (termTimer) clearTimeout(termTimer);
      if (killTimer) clearTimeout(killTimer);
      const exit: SpawnExit = { code, signal, timedOut, stderr };
      if (spawnError !== undefined) exit.error = spawnError;
      resolve(exit);
    };
    child.on("error", (err: NodeJS.ErrnoException) => {
      spawnError = err.message;
      // `close` never fires when the spawn itself failed.
      if (child.pid === undefined) settle(null, null);
    });
    child.on("close", settle);
  });

  return { pid: child.pid ?? -1, child, stdin, stdout, exited, kill };
}

/** Reassemble NDJSON lines across arbitrary chunk boundaries. */
export type LineSplitter = {
  /** Returns the complete lines in this chunk (without the newline). */
  feed(chunk: string): string[];
  /** Returns the trailing partial line, if any. */
  finish(): string[];
};

export function createLineSplitter(): LineSplitter {
  let buffer = "";
  return {
    feed(chunk: string): string[] {
      buffer += chunk;
      const parts = buffer.split("\n");
      buffer = parts.pop() ?? "";
      return parts.map((l) => (l.endsWith("\r") ? l.slice(0, -1) : l));
    },
    finish(): string[] {
      const rest = buffer;
      buffer = "";
      return rest.length > 0 ? [rest] : [];
    },
  };
}
