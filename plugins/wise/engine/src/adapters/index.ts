// Adapter registry. One adapter per harness; harnesses without one raise HARNESS_UNAVAILABLE.

import type { Adapter, ErrorCode, Harness } from "../types.ts";
import { claudeAdapter } from "./claude.ts";
import { codexAdapter } from "./codex.ts";
import { cursorAdapter } from "./cursor.ts";
import { geminiAdapter } from "./gemini.ts";
import { grokAdapter } from "./grok.ts";

export class AdapterError extends Error {
  readonly code: ErrorCode;
  constructor(code: ErrorCode, message: string) {
    super(message);
    this.name = "AdapterError";
    this.code = code;
  }
}

// gemini (M5.3) is best effort: registered, but not validated against a working login.
const ADAPTERS: Partial<Record<Harness, Adapter>> = {
  claude: claudeAdapter,
  codex: codexAdapter,
  cursor: cursorAdapter,
  gemini: geminiAdapter,
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
export { cursorAdapter, startCursor } from "./cursor.ts";
export type { CursorRun } from "./cursor.ts";
export { geminiAdapter, startGemini } from "./gemini.ts";
export type { GeminiRun } from "./gemini.ts";
export { grokAdapter, startGrok } from "./grok.ts";
export type { GrokRun } from "./grok.ts";
export { cleanEnv, spawnClean } from "./spawn.ts";
