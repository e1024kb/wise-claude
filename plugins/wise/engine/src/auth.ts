// Auth probe (P5, plan M2.7): every harness a run needs is probed before a run dir exists.
// `claude` goes through the adapter's `probeAuth`; harnesses without an adapter yet (M5)
// fail closed with `AUTH_REQUIRED` and the login command to show the user.

import { domainError } from "./rpc.ts";
import type { RpcError } from "./rpc.ts";
import type { Adapter, AuthMode, Harness, Resolved, WorkflowDef } from "./types.ts";

/** Login command per harness, shown in `AUTH_REQUIRED.data.login_cmd`. */
export const LOGIN_CMDS: Readonly<Record<Harness, string>> = {
  claude: "claude auth login",
  codex: "codex login",
  gemini: "gemini",
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

export function authRequired(harness: Harness, loginCmd: string, detail?: string): RpcError {
  return domainError(
    "AUTH_REQUIRED",
    `${harness}: not logged in${detail ? ` (${detail})` : ""}; run \`${loginCmd}\``,
    { harness, login_cmd: loginCmd },
  );
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
