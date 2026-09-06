// `models` and `dispatch` CLI commands: the catalog for skills that offer a harness/model/effort
// pick, and a one-off child run on any harness — how a skill (e.g. /wise-pr-watch-auto --on codex)
// runs its own procedure as a subagent of another CLI without going through a workflow.

import { readFileSync } from "node:fs";
import { adapterFor, hasAdapter } from "./adapters/index.ts";
import { catalogFor, catalogModel, defaultModel } from "./models.ts";
import { headline } from "./steps/agent.ts";
import { EFFORTS, HARNESSES, RUN_MODES } from "./types.ts";
import type { Adapter, Effort, Harness, RunMode, RunReq, RunRes } from "./types.ts";

export type DispatchIo = { out: (s: string) => void; err: (s: string) => void };

type Flags = Record<string, string | true>;

function str(flag: string | true | undefined): string | undefined {
  return typeof flag === "string" ? flag : undefined;
}

function isHarness(s: string): s is Harness {
  return (HARNESSES as readonly string[]).includes(s);
}

/**
 * `models [harness...] [--text]`: the model catalog per harness — id, label, description and the
 * efforts each model takes. Skills read this instead of hardcoding a list.
 */
export function cmdModels(positional: string[], flags: Flags, io: DispatchIo): number {
  const wanted = positional.length > 0 ? positional : [...HARNESSES];
  const rows: {
    harness: Harness;
    id: string;
    label: string;
    description: string;
    efforts: readonly Effort[];
  }[] = [];
  for (const name of wanted) {
    if (!isHarness(name)) {
      io.err(`models: unknown harness ${name} (one of ${HARNESSES.join(", ")})\n`);
      return 2;
    }
    for (const m of catalogFor(name)) rows.push({ harness: name, ...m });
  }
  if (flags.text === true) {
    for (const r of rows) {
      io.out(
        `${r.harness}\t${r.id}\t${r.label}\t${r.efforts.length > 0 ? r.efforts.join(",") : "-"}\t${r.description}\n`,
      );
    }
  } else {
    io.out(JSON.stringify(rows) + "\n");
  }
  return 0;
}

export type DispatchResult = {
  ok: boolean;
  exit: RunRes["exit"];
  harness: Harness;
  model: string;
  effort: Effort | null;
  mode: RunMode;
  verdict: string;
  text: string;
  usage: RunRes["usage"];
  error?: string;
  warnings: string[];
};

const DEFAULT_DISPATCH_TIMEOUT_S = 3600;

/**
 * `dispatch --harness <h> --prompt-file <path> [--model <id>] [--effort <e>] [--mode <m>]
 * [--cwd <dir>] [--timeout-s <n>] [--add-dir <dir>] [--allowed-tools <a,b>] [--text]`
 *
 * One child run, no daemon, no ledger: read the prompt, start the harness, wait, print one JSON
 * object (or the child's text under --text). Exit 0 on a clean child, 1 otherwise. A model off
 * the catalog passes through as typed; an effort the model does not list is an error, so a typo
 * never silently changes the spend.
 */
export async function cmdDispatch(
  flags: Flags,
  io: DispatchIo,
  adapters: (h: Harness) => Adapter = adapterFor,
): Promise<number> {
  const harnessRaw = str(flags.harness);
  if (harnessRaw === undefined || !isHarness(harnessRaw)) {
    io.err(`dispatch: --harness must be one of ${HARNESSES.join(", ")}\n`);
    return 64;
  }
  const harness = harnessRaw;
  if (!hasAdapter(harness)) {
    io.err(`dispatch: no adapter for harness ${harness}\n`);
    return 64;
  }
  const promptFile = str(flags["prompt-file"]);
  const promptInline = str(flags.prompt);
  if (promptFile === undefined && promptInline === undefined) {
    io.err("dispatch: --prompt-file <path> (or --prompt <text>) is required\n");
    return 64;
  }
  let prompt: string;
  try {
    prompt = promptInline ?? readFileSync(promptFile as string, "utf8");
  } catch (e) {
    io.err(`dispatch: cannot read prompt file: ${(e as Error).message}\n`);
    return 66;
  }
  if (prompt.trim().length === 0) {
    io.err("dispatch: prompt is empty\n");
    return 64;
  }

  const warnings: string[] = [];
  const modelFlag = str(flags.model);
  const inCatalog = catalogModel(harness, modelFlag);
  const model = modelFlag !== undefined ? (inCatalog?.id ?? modelFlag) : defaultModel(harness).id;
  if (modelFlag !== undefined && inCatalog === undefined) {
    warnings.push(`model ${modelFlag} is not in the catalog; passed through as typed`);
  }

  const effortFlag = str(flags.effort);
  let effort: Effort | undefined;
  if (effortFlag !== undefined) {
    if (!(EFFORTS as readonly string[]).includes(effortFlag)) {
      io.err(`dispatch: --effort must be one of ${EFFORTS.join(", ")}\n`);
      return 64;
    }
    const known = inCatalog ?? (modelFlag === undefined ? defaultModel(harness) : undefined);
    if (known && !known.efforts.includes(effortFlag as Effort)) {
      io.err(
        `dispatch: model ${known.id} takes ${known.efforts.length > 0 ? known.efforts.join(", ") : "no effort flag"}, not ${effortFlag}\n`,
      );
      return 64;
    }
    effort = effortFlag as Effort;
  }

  const modeFlag = str(flags.mode) ?? "auto";
  if (!(RUN_MODES as readonly string[]).includes(modeFlag)) {
    io.err(`dispatch: --mode must be one of ${RUN_MODES.join(", ")}\n`);
    return 64;
  }
  const mode = modeFlag as RunMode;

  const timeoutS = str(flags["timeout-s"]);
  const timeoutMs = (timeoutS !== undefined ? Number(timeoutS) : DEFAULT_DISPATCH_TIMEOUT_S) * 1000;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    io.err("dispatch: --timeout-s must be a positive number\n");
    return 64;
  }

  const req: RunReq = {
    prompt,
    model,
    cwd: str(flags.cwd) ?? process.cwd(),
    mode,
    timeout_ms: timeoutMs,
    auth: "subscription",
    ...(effort !== undefined ? { effort } : {}),
    ...(str(flags["add-dir"]) !== undefined ? { add_dirs: [str(flags["add-dir"]) as string] } : {}),
    ...(str(flags["allowed-tools"]) !== undefined
      ? { allowed_tools: (str(flags["allowed-tools"]) as string).split(",").filter(Boolean) }
      : {}),
  };

  const res = await adapters(harness).run(req, () => {});
  const result: DispatchResult = {
    ok: res.exit === "ok",
    exit: res.exit,
    harness,
    model: res.model ?? model,
    effort: effort ?? null,
    mode,
    verdict: headline(res.text),
    text: res.text,
    usage: res.usage,
    ...(res.error !== undefined ? { error: res.error } : {}),
    warnings: [...warnings, ...(res.warnings ?? [])],
  };
  if (flags.text === true) {
    io.out(res.text.endsWith("\n") || res.text.length === 0 ? res.text : res.text + "\n");
    if (!result.ok)
      io.err(`dispatch: child exit ${res.exit}${res.error ? `: ${res.error}` : ""}\n`);
  } else {
    io.out(JSON.stringify(result) + "\n");
  }
  return result.ok ? 0 : 1;
}
