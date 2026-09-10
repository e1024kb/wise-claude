// The model catalog: which models the pre-flight offers per harness and which efforts each of
// them takes. Deterministic and hand-picked (2026-09-10); ids are what the CLIs accept
// (`claude --model`, `codex -m`, `cursor-agent --model`, `grok -m`, `gemini -m`). The `model.<group>` question lists
// `MODEL_CATALOG[harness]` in this order; `effort.<group>` lists the chosen model's `efforts`.
// A model with one effort is pinned to it without a question; one with none runs unflagged.

import { EFFORTS } from "./types.ts";
import type { Effort, Harness } from "./types.ts";

export type CatalogModel = {
  /** Id as the harness CLI takes it. */
  id: string;
  label: string;
  description: string;
  /** Efforts offered, low to high. Empty: the CLI gets no effort flag. */
  efforts: readonly Effort[];
};

const LMH: readonly Effort[] = ["low", "medium", "high"];

export const MODEL_CATALOG: Readonly<Record<Harness, readonly CatalogModel[]>> = {
  claude: [
    { id: "claude-fable-5-1", label: "Fable 5.1", description: "latest Fable", efforts: LMH },
    { id: "claude-opus-5", label: "Opus 5", description: "current Opus", efforts: LMH },
    { id: "claude-opus-4-8", label: "Opus 4.8", description: "previous Opus", efforts: LMH },
    {
      id: "claude-sonnet-5",
      label: "Sonnet 5",
      description: "latest Sonnet",
      efforts: ["low", "medium"],
    },
    {
      id: "claude-haiku-4-5",
      label: "Haiku 4.5",
      description: "cheap tier for simple steps",
      efforts: ["medium"],
    },
  ],
  codex: [
    { id: "gpt-6-astra", label: "GPT-6 Astra", description: "OpenAI flagship", efforts: LMH },
    { id: "gpt-5.6-sol", label: "GPT-5.6 Sol", description: "5.6 top tier", efforts: LMH },
    { id: "gpt-5.6-luna", label: "GPT-5.6 Luna", description: "5.6 cheap tier", efforts: LMH },
    { id: "gpt-5.5", label: "GPT-5.5", description: "previous generation", efforts: LMH },
  ],
  // Keep Cursor on its current native model pool: the flagship first, then the faster coding model.
  cursor: [
    {
      id: "grok-4.6",
      label: "Grok 4.6",
      description: "Cursor's frontier model for complex agentic work",
      efforts: [],
    },
    {
      id: "composer-2.5",
      label: "Composer 2.5",
      description: "Cursor's fast, cost-efficient coding model",
      efforts: [],
    },
  ],
  grok: [{ id: "grok-4.6", label: "Grok 4.6", description: "xAI current model", efforts: [] }],
  gemini: [
    {
      id: "gemini-3.8-flash",
      label: "Gemini 3.8 Flash",
      description: "latest Flash",
      efforts: [],
    },
    {
      id: "gemini-3.5-flash-lite",
      label: "Gemini 3.5 Flash-Lite",
      description: "cheapest Gemini",
      efforts: [],
    },
  ],
};

/** Claude CLI aliases resolve to the current generation of their family. */
export const CLAUDE_ALIASES: Readonly<Record<string, string>> = {
  fable: "claude-fable-5-1",
  opus: "claude-opus-5",
  sonnet: "claude-sonnet-5",
  haiku: "claude-haiku-4-5",
};

export function catalogFor(harness: Harness): readonly CatalogModel[] {
  return MODEL_CATALOG[harness];
}

/** The catalog entry for `id` (a Claude alias expands first), or undefined. */
export function catalogModel(harness: Harness, id: string | undefined): CatalogModel | undefined {
  if (id === undefined) return undefined;
  const m = id.trim().toLowerCase();
  const key = harness === "claude" ? (CLAUDE_ALIASES[m] ?? m) : m;
  return catalogFor(harness).find((c) => c.id === key);
}

/** `pinned` when the catalog has it, else the harness's first entry. */
export function defaultModel(harness: Harness, pinned?: string): CatalogModel {
  return catalogModel(harness, pinned) ?? (catalogFor(harness)[0] as CatalogModel);
}

/**
 * The effort a model runs at when the workflow asked for `wanted`: `wanted` itself when the model
 * lists it, else the highest listed effort below it, else the lowest listed one. `undefined` for
 * a model without effort control.
 */
export function defaultEffort(model: CatalogModel, wanted?: string): Effort | undefined {
  const efforts = model.efforts;
  if (efforts.length === 0) return undefined;
  const want = (wanted ?? "").trim().toLowerCase();
  if ((efforts as readonly string[]).includes(want)) return want as Effort;
  const rank = (EFFORTS as readonly string[]).indexOf(want);
  if (rank >= 0) {
    for (let i = rank - 1; i >= 0; i--) {
      const candidate = EFFORTS[i] as Effort;
      if (efforts.includes(candidate)) return candidate;
    }
  }
  return efforts[0];
}
