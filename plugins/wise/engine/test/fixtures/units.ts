// Fixtures for the unit-pipeline model phases: a starter scripted per phase (the phase is read
// from the prompt's first line) and the `gh` rules of a PR that goes through cleanly.
import { mkdirSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
import type { AgentHandle, AgentStarter } from "../../src/steps/agent.ts";
import type { ModelPhase } from "../../src/prompts/units/schemas.ts";
import { isModelPhase } from "../../src/prompts/units/schemas.ts";
import type { Harness, RunReq, RunRes } from "../../src/types.ts";
import { usage } from "./executor/fake.ts";
import { commitFile, result, startsWith } from "./git.ts";
import type { GhRule } from "./git.ts";

export type PhaseScript = (req: RunReq, nth: number) => RunRes | Promise<RunRes>;
export type ScriptedCall = { phase: ModelPhase; harness: Harness; req: RunReq };

/** The phase a rendered unit prompt belongs to (`# wise unit phase: <phase>`). */
export function phaseOf(req: RunReq): ModelPhase {
  const m = /^# wise unit phase: (\S+)/.exec(req.prompt);
  const phase = m?.[1] ?? "";
  if (!isModelPhase(phase)) throw new Error(`not a unit phase prompt: ${req.prompt.slice(0, 60)}`);
  return phase;
}

/** The engine-owned plan path the plan prompt asks for: `<runDir>/plans/PLAN-<ref>.md`. */
export function planFileFor(req: RunReq): string {
  const runDir = req.add_dirs?.[0];
  if (!runDir) throw new Error("plan prompt without add_dirs");
  return join(runDir, "plans", `PLAN-${basename(req.cwd)}.md`);
}

export function answer(json: unknown, extra: Partial<RunRes> = {}): RunRes {
  return { text: "done", json, usage: usage(), exit: "ok", ...extra };
}

/** One child per call, answered by the script of its phase; calls are recorded per phase. */
export function scriptedStarter(scripts: Partial<Record<ModelPhase, PhaseScript>>): {
  starter: AgentStarter;
  calls: ScriptedCall[];
  ofPhase: (phase: ModelPhase) => ScriptedCall[];
} {
  const calls: ScriptedCall[] = [];
  const counts: Partial<Record<ModelPhase, number>> = {};
  const starter: AgentStarter = (harness, req) => {
    const phase = phaseOf(req);
    calls.push({ phase, harness, req });
    const nth = (counts[phase] = (counts[phase] ?? 0) + 1);
    const script = scripts[phase];
    const done: Promise<RunRes> = script
      ? Promise.resolve().then(() => script(req, nth))
      : Promise.resolve({
          text: "",
          usage: usage(0, 0),
          exit: "error",
          error: `no script for phase ${phase}`,
        });
    const handle: AgentHandle = { pid: 0, done };
    return handle;
  };
  return { starter, calls, ofPhase: (phase) => calls.filter((c) => c.phase === phase) };
}

/** Plan child that writes the plan file and reports `ready`. */
export const planReady: PhaseScript = (req) => {
  const path = planFileFor(req);
  mkdirSync(join(path, ".."), { recursive: true });
  writeFileSync(path, `# ${basename(req.cwd)}: plan\n\n## Tasks\n\n1. do it\n`, "utf8");
  return answer({ plan_path: path, status: "ready" }, { cursor: "sess-plan" });
};

/** Implement child that makes one commit in the worktree. */
export const implementCommit: PhaseScript = (req, nth) => {
  commitFile(req.cwd, `feature-${nth}.txt`, `x${nth}\n`, `feat: task ${nth}`);
  return answer({ waves: 1, tasks: 1, done: 1, failed: 0, commits: 1 }, { cursor: "sess-impl" });
};

export const reviewApprove: PhaseScript = () =>
  answer({ findings: 0, blocking: 0, verdict: "approve" }, { cursor: "sess-review" });

export const watchGreen: PhaseScript = () =>
  answer({
    ci: "green",
    bot_reviews: "resolved",
    human_comment: false,
    merged: false,
    verdict: "ready",
  });

/** `gh` for PRs that are created (one per branch), get their reviewer, and merge with `--squash`. */
export function happyGh(opts: { prNumber?: number; squashFails?: boolean } = {}): {
  rule: GhRule;
  state: { pr?: "OPEN" | "MERGED"; prs: Record<string, { number: number; state: "OPEN" | "MERGED" }> };
} {
  const first = opts.prNumber ?? 5;
  const url = (n: number): string => `https://github.com/a/r/pull/${n}`;
  const state: ReturnType<typeof happyGh>["state"] = { prs: {} };
  const byNumber = (n: string) => Object.values(state.prs).find((p) => String(p.number) === n);
  const sync = (): void => {
    const f = Object.values(state.prs).find((p) => p.number === first);
    if (f) state.pr = f.state;
  };
  const rule: GhRule = (a) => {
    if (startsWith(a, "repo", "view")) return result('{"defaultBranchRef":{"name":"main"}}');
    if (startsWith(a, "pr", "list")) return result("[]");
    if (startsWith(a, "pr", "create")) {
      const branch = a[a.indexOf("--head") + 1] ?? "?";
      const n = first + Object.keys(state.prs).length;
      state.prs[branch] = { number: n, state: "OPEN" };
      sync();
      return result(`${url(n)}\n`);
    }
    if (startsWith(a, "pr", "view")) {
      const target = a[2] ?? "";
      const byNum = byNumber(target);
      if (byNum) {
        return result(
          JSON.stringify({ reviewRequests: [], state: byNum.state, number: byNum.number, url: url(byNum.number) }),
        );
      }
      const pr = state.prs[target];
      return pr === undefined
        ? result("no pull requests found", 1)
        : result(JSON.stringify({ number: pr.number, url: url(pr.number), state: pr.state, baseRefName: "main" }));
    }
    if (startsWith(a, "pr", "edit")) return result("");
    if (startsWith(a, "pr", "merge")) {
      const pr = byNumber(a[2] ?? "");
      if (!pr) return result("", 1, "no such PR");
      if (a[3] === "--squash" && opts.squashFails) {
        return result("", 1, "squash merges are not allowed on this repository");
      }
      pr.state = "MERGED";
      sync();
      return result("");
    }
    return undefined;
  };
  return { rule, state };
}
