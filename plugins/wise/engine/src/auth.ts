// Auth probe (P5, plan M2.7): every harness a run needs is probed before a run dir exists.
// Each harness goes through its adapter's `probeAuth`; harnesses without an adapter fail closed
// with `AUTH_REQUIRED` and the login command to show the user. E12 fallback harnesses are not
// part of the up-front set: the executor probes them lazily on first use (`probeOne`) and skips
// a logged-out fallback with a `warn` instead of failing the run. `installedHarnesses` is the
// pre-flight side: which installed CLIs a `harness.<group>` question may offer.

import { accessSync, constants, statSync } from "node:fs";
import { delimiter, isAbsolute, join } from "node:path";
import { domainError } from "./rpc.ts";
import type { RpcError } from "./rpc.ts";
import { HARNESSES } from "./types.ts";
import type { Adapter, AuthMode, Harness, Resolved, WorkflowDef } from "./types.ts";

/** Login command per harness, shown in `AUTH_REQUIRED.data.login_cmd`. */
export const LOGIN_CMDS: Readonly<Record<Harness, string>> = {
  claude: "claude auth login",
  codex: "codex login",
  gemini: "gemini (interactive, then /auth)",
  grok: "grok login",
};

export type HarnessNeed = { harness: Harness; auth: AuthMode };
export type AdapterLookup = (harness: Harness) => Adapter | undefined;

/** Unique harness/auth pairs over the enabled `agent` steps, in step order. */
export function collectNeeds(
  def: WorkflowDef,
  enabled: ReadonlySet<string>,
  resolved: Readonly<Record<string, Resolved>>,
): HarnessNeed[] {
  const seen = new Set<string>();
  const out: HarnessNeed[] = [];
  const add = (harness: Harness, auth: HarnessNeed["auth"]): void => {
    const key = `${harness}/${auth}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ harness, auth });
  };
  for (const step of def.steps) {
    if (!enabled.has(step.id)) continue;
    if (step.type === "agent") {
      add(resolved[step.id]?.harness ?? step.harness ?? "claude", step.auth ?? "subscription");
    } else if (step.type === "units") {
      // M4.2: the unit phases resolved under `<step>.<phase>`; the step's own harness otherwise.
      const prefix = `${step.id}.`;
      const phases = Object.keys(resolved).filter((k) => k.startsWith(prefix));
      if (phases.length === 0) add(step.harness ?? "claude", step.auth ?? "subscription");
      for (const k of phases) add(resolved[k]?.harness ?? "claude", step.auth ?? "subscription");
    }
  }
  return out;
}

/** True when `bin` names an executable file on `PATH` (absolute paths are checked as is). */
export function binOnPath(bin: string, env: NodeJS.ProcessEnv = process.env): boolean {
  const candidates = isAbsolute(bin)
    ? [bin]
    : (env.PATH ?? "")
        .split(delimiter)
        .filter(Boolean)
        .map((dir) => join(dir, bin));
  for (const candidate of candidates) {
    try {
      accessSync(candidate, constants.X_OK);
      if (statSync(candidate).isFile()) return true;
    } catch {
      // not here
    }
  }
  return false;
}

/**
 * Harnesses a `harness.<group>` question offers besides a group's default: every harness with
 * an adapter whose CLI is installed (its `bin` on PATH; an adapter without `bin` counts as
 * installed), in `HARNESSES` order. Being logged in is not required: the question is asked
 * whenever more than one CLI is installed, and a logged-out pick is flagged in its option
 * (`loggedOutHarnesses`) and refused by the run's auth probe with the login command. Harnesses
 * every unlocked group already defaults to are left out (the group offers its default itself).
 * No I/O beyond the PATH lookup.
 */
export function installedHarnesses(
  def: WorkflowDef,
  lookup: AdapterLookup,
  env: NodeJS.ProcessEnv = process.env,
): Harness[] {
  const groups = (def.tuning?.groups ?? []).filter((g) => !g.locked);
  const out: Harness[] = [];
  for (const h of HARNESSES) {
    const everyGroupDefaultsToIt = groups.every((g) => (g.default.harness ?? "claude") === h);
    if (everyGroupDefaultsToIt) continue;
    const adapter = lookup(h);
    if (!adapter) continue;
    if (adapter.bin !== undefined && !binOnPath(adapter.bin, env)) continue;
    out.push(h);
  }
  return out;
}

/** The subset of `harnesses` whose subscription login probe fails; probed in order. */
export async function loggedOutHarnesses(
  harnesses: readonly Harness[],
  lookup: AdapterLookup,
): Promise<Harness[]> {
  const out: Harness[] = [];
  for (const h of harnesses) {
    const probe = await probeOne(h, "subscription", lookup);
    if (!probe.ok) out.push(h);
  }
  return out;
}

export function authRequired(harness: Harness, loginCmd: string, detail?: string): RpcError {
  return domainError(
    "AUTH_REQUIRED",
    `${harness}: not logged in${detail ? ` (${detail})` : ""}; run \`${loginCmd}\``,
    { harness, login_cmd: loginCmd },
  );
}

export type ProbeOutcome = { ok: boolean; login_cmd: string; detail?: string };

/** One probe that never throws: a missing adapter or a probe error is a failed outcome. */
export async function probeOne(
  harness: Harness,
  auth: AuthMode,
  lookup: AdapterLookup,
): Promise<ProbeOutcome> {
  const adapter = lookup(harness);
  if (!adapter) {
    return { ok: false, login_cmd: LOGIN_CMDS[harness], detail: "no adapter in this build" };
  }
  try {
    const probe = await adapter.probeAuth(auth);
    return { ok: probe.ok, login_cmd: probe.login_cmd ?? LOGIN_CMDS[harness] };
  } catch (err) {
    return { ok: false, login_cmd: LOGIN_CMDS[harness], detail: (err as Error).message };
  }
}

/**
 * Probe each need in order and throw `AUTH_REQUIRED` on the first failure. A harness without an
 * adapter is reported the same way, so the caller creates nothing it cannot run.
 */
export async function probeHarnesses(
  needs: readonly HarnessNeed[],
  lookup: AdapterLookup,
): Promise<void> {
  for (const need of needs) {
    const adapter = lookup(need.harness);
    if (!adapter) {
      throw authRequired(need.harness, LOGIN_CMDS[need.harness], "no adapter in this build");
    }
    let probe: { ok: boolean; login_cmd?: string };
    try {
      probe = await adapter.probeAuth(need.auth);
    } catch (err) {
      throw authRequired(need.harness, LOGIN_CMDS[need.harness], (err as Error).message);
    }
    if (!probe.ok) {
      throw authRequired(need.harness, probe.login_cmd ?? LOGIN_CMDS[need.harness], need.auth);
    }
  }
}
