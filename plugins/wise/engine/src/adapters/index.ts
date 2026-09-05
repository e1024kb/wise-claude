// Adapter registry. One adapter per harness; harnesses without one raise HARNESS_UNAVAILABLE.

import type { Adapter, ErrorCode, Harness } from "../types.ts";
import { claudeAdapter } from "./claude.ts";
import { codexAdapter } from "./codex.ts";
import { grokAdapter } from "./grok.ts";

export class AdapterError extends Error {
  readonly code: ErrorCode;
  constructor(code: ErrorCode, message: string) {
    super(message);
    this.name = "AdapterError";
    this.code = code;
  }
}

// gemini stays absent until M5.3 (best effort).
const ADAPTERS: Partial<Record<Harness, Adapter>> = {
  claude: claudeAdapter,
  codex: codexAdapter,
  grok: grokAdapter,
};

export function hasAdapter(harness: Harness): boolean {
  return ADAPTERS[harness] !== undefined;
}

export function adapterFor(harness: Harness): Adapter {
  const adapter = ADAPTERS[harness];
  if (!adapter) {
    throw new AdapterError("HARNESS_UNAVAILABLE", `no adapter for harness "${harness}" yet`);
  }
  return adapter;
}

export { claudeAdapter, startClaude } from "./claude.ts";
export type { ClaudeRun } from "./claude.ts";
export { codexAdapter, startCodex } from "./codex.ts";
export type { CodexRun } from "./codex.ts";
export { grokAdapter, startGrok } from "./grok.ts";
export type { GrokRun } from "./grok.ts";
export { cleanEnv, spawnClean } from "./spawn.ts";
