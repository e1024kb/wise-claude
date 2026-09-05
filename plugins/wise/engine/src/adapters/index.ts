// Adapter registry. One adapter per harness; harnesses without one raise HARNESS_UNAVAILABLE.

import type { Adapter, ErrorCode, Harness } from "../types.ts";
import { claudeAdapter } from "./claude.ts";

export class AdapterError extends Error {
  readonly code: ErrorCode;
  constructor(code: ErrorCode, message: string) {
    super(message);
    this.name = "AdapterError";
    this.code = code;
  }
}

const ADAPTERS: Partial<Record<Harness, Adapter>> = {
  claude: claudeAdapter,
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
export { cleanEnv, spawnClean } from "./spawn.ts";
