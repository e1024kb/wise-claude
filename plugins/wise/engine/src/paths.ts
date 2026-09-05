// Filesystem roots shared by ledger, profile and defs. XDG rules from the Python helper.
import { realpathSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";

export type Env = Readonly<Record<string, string | undefined>>;
/** Injectable environment; every field falls back to the live process. */
export type RootOpts = { env?: Env; home?: string; cwd?: string };

export function envOf(opts: RootOpts): Env {
  return opts.env ?? process.env;
}
export function homeOf(opts: RootOpts): string {
  return opts.home ?? envOf(opts).HOME ?? homedir();
}
export function cwdOf(opts: RootOpts): string {
  return opts.cwd ?? process.cwd();
}

/** `$XDG_DATA_HOME/wise`, else `~/.local/share/wise`. */
export function wiseDataRoot(opts: RootOpts = {}): string {
  const xdg = envOf(opts).XDG_DATA_HOME;
  const base = xdg ? xdg : join(homeOf(opts), ".local", "share");
  return join(base, "wise");
}

/** `$CLAUDE_PLUGIN_DATA`, else `$WISE_DATA_DIR`, else `wiseDataRoot()`. */
export function pluginDataRoot(opts: RootOpts = {}): string {
  const env = envOf(opts);
  const override = env.CLAUDE_PLUGIN_DATA || env.WISE_DATA_DIR;
  return override ? override : wiseDataRoot(opts);
}

/** Like Python's non-strict `Path.resolve()`: realpath the nearest existing ancestor, keep the rest. */
export function realpathLoose(p: string): string {
  let head = resolve(p);
  const tail: string[] = [];
  for (;;) {
    try {
      return join(realpathSync(head), ...tail);
    } catch {
      const parent = dirname(head);
      if (parent === head) return join(head, ...tail);
      tail.unshift(basename(head));
      head = parent;
    }
  }
}

/** Absolute (symlink-resolved) cwd with `/` replaced by `-`; matches Claude Code's project slug. */
export function cwdSlug(cwd: string = process.cwd()): string {
  return realpathLoose(cwd).replaceAll("/", "-");
}

/** Per-workspace runs root: `<data_root>/runs/<cwd-slug>/`. */
export function wiseRunsRootForCwd(opts: RootOpts = {}): string {
  return join(wiseDataRoot(opts), "runs", cwdSlug(opts.cwd));
}

export function runDirFor(runsRoot: string, runId: string): string {
  return join(runsRoot, runId);
}
