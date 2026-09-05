// List-price table for `auth: api-key` children whose CLI reports tokens but no dollar figure
// (T6, M6.1). Codex is the main case: `codex exec --json` gives `usage` only. Claude and grok
// report `total_cost_usd`, which always wins over this table. Subscription runs are never priced
// here: their `cost_usd` stays whatever the CLI reported (Claude gives list-price equivalents).
//
// Sources, read 2026-09-05:
//   Claude  https://platform.claude.com/docs/en/about-claude/pricing (cache write = 5-minute rate,
//           cache read = 0.1x input; Fable 5.1 reads at 0.025x)
//   OpenAI  https://developers.openai.com/api/docs/pricing (no cache-write charge; the codex
//           variants gpt-5-codex / gpt-5.1-codex / gpt-5.2-codex were announced at the price of
//           their base model and are carried here at those rates)
//   xAI     https://docs.x.ai/docs/models (rates for prompts under 200k tokens; the >=200k tier is
//           not modelled)
// USD per million tokens. Update the table and the date together.

import { CLAUDE_ALIASES } from "./models.ts";
import type { CostSource, Harness, Usage } from "./types.ts";

export type Price = {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  /**
   * OpenAI counts cached prompt tokens inside `input_tokens`; Anthropic and xAI report them as a
   * separate bucket. `true` prices `input - cache_read` at the input rate.
   */
  cached_in_input?: boolean;
};

const claude = (input: number, output: number, cacheReadRatio = 0.1): Price => ({
  input,
  output,
  cache_read: input * cacheReadRatio,
  cache_write: input * 1.25,
});
const openai = (input: number, cached: number, output: number): Price => ({
  input,
  output,
  cache_read: cached,
  cache_write: 0,
  cached_in_input: true,
});
const xai = (input: number, cached: number, output: number): Price => ({
  input,
  output,
  cache_read: cached,
  cache_write: 0,
});

export const PRICES: Readonly<Record<string, Price>> = {
  // Claude 5 family plus the Haiku 4.5 tier the workflows fall back to.
  "claude-fable-5-1": claude(10, 50, 0.025),
  "claude-fable-5": claude(10, 50),
  "claude-opus-5": claude(5, 25),
  "claude-opus-4-8": claude(5, 25),
  "claude-opus-4-7": claude(5, 25),
  "claude-opus-4-6": claude(5, 25),
  "claude-sonnet-5": claude(2, 10),
  "claude-sonnet-4-6": claude(3, 15),
  "claude-haiku-4-5": claude(1, 5),
  // OpenAI gpt-5 family and the codex variants `codex exec -m` accepts.
  "gpt-5": openai(1.25, 0.125, 10),
  "gpt-5-mini": openai(0.25, 0.025, 2),
  "gpt-5-nano": openai(0.05, 0.005, 0.4),
  "gpt-5-codex": openai(1.25, 0.125, 10),
  "gpt-5.1": openai(1.25, 0.125, 10),
  "gpt-5.1-codex": openai(1.25, 0.125, 10),
  "gpt-5.2": openai(1.75, 0.175, 14),
  "gpt-5.2-codex": openai(1.75, 0.175, 14),
  "gpt-5.3-codex": openai(1.75, 0.175, 14),
  "gpt-5.4": openai(2.5, 0.25, 15),
  "gpt-5.5": openai(5, 0.5, 30),
  // GPT-5.6 tiers (2026-06) and GPT-6 Astra (2026-09-03); cached input at the usual 0.1x.
  "gpt-5.6-sol": openai(4, 0.4, 20),
  "gpt-5.6-terra": openai(2, 0.2, 12),
  "gpt-5.6-luna": openai(0.2, 0.02, 1.2),
  "gpt-6-astra": openai(10, 1, 50),
  // xAI grok 4 family.
  "grok-4.6": xai(2, 0.5, 6),
  "grok-4.5": xai(2, 0.3, 6),
  "grok-4.3": xai(1.25, 0.2, 2.5),
  "grok-build-0.1": xai(1, 0.2, 2),
};

/**
 * Table key for a model id as the engine or the CLI names it: lower-cased, Claude aliases
 * expanded, a dated snapshot suffix (`-20260401`) and an `-latest` suffix dropped. `inherit`
 * and the empty pin have no key (the child's default is not known here).
 */
export function canonicalModel(harness: Harness, model: string): string {
  const m = model.trim().toLowerCase();
  if (!m || m === "inherit") return "";
  if (harness === "claude" && CLAUDE_ALIASES[m] !== undefined) return CLAUDE_ALIASES[m] as string;
  return m.replace(/-\d{8}$/, "").replace(/-latest$/, "");
}

export function priceFor(harness: Harness, model: string): Price | undefined {
  const key = canonicalModel(harness, model);
  return key ? PRICES[key] : undefined;
}

/** Dollar cost of `u` at `price`, rounded to a millionth. */
export function costOf(u: Usage, price: Price): number {
  const fresh = price.cached_in_input ? Math.max(0, u.input - u.cache_read) : u.input;
  const usd =
    (fresh * price.input +
      u.cache_read * price.cache_read +
      u.cache_write * price.cache_write +
      u.output * price.output) /
    1_000_000;
  return Math.round(usd * 1e6) / 1e6;
}

export type Priced = {
  usage: Usage;
  /** Set when an api-key child gave no cost and its model has no table row. */
  unknownModel?: string;
};

/**
 * Label and, where needed, price one child's usage. A reported `cost_usd` wins and is labelled
 * `reported`. Tokens-only usage under `api-key` is priced from the table (`priced`), or left at
 * `none` with `unknownModel` set for the caller to warn once. Subscription usage without a cost
 * is `none`. Usage that already carries a `cost_source` passes through unchanged, so a fold that
 * was priced upstream (a unit phase) is never priced twice.
 */
export function priceUsage(u: Usage, harness: Harness, model: string): Priced {
  if (u.cost_source !== undefined) return { usage: u };
  const usage: Usage = { ...u };
  let source: CostSource = "none";
  if (u.cost_usd !== undefined) {
    source = "reported";
  } else if (u.pool === "api-key") {
    const price = priceFor(harness, model);
    if (price) {
      usage.cost_usd = costOf(u, price);
      source = "priced";
    } else {
      usage.cost_source = "none";
      return { usage, unknownModel: model.trim() || "inherit" };
    }
  }
  usage.cost_source = source;
  return { usage };
}
