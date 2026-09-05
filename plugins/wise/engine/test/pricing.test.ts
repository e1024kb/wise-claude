// M6.1 pricing: table lookup, alias and snapshot canonicalisation, cost arithmetic, and the
// reported-wins / api-key-only / unknown-model rules of `priceUsage`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { canonicalModel, costOf, PRICES, priceFor, priceUsage } from "../src/pricing.ts";
import type { Usage } from "../src/types.ts";

const u = (over: Partial<Usage> = {}): Usage => ({
  input: 1_000_000,
  output: 100_000,
  cache_read: 500_000,
  cache_write: 200_000,
  pool: "api-key",
  ...over,
});

test("canonicalModel: Claude aliases, snapshot and -latest suffixes, inherit and empty", () => {
  assert.equal(canonicalModel("claude", "opus"), "claude-opus-5");
  assert.equal(canonicalModel("claude", "Sonnet"), "claude-sonnet-5");
  assert.equal(canonicalModel("claude", "haiku"), "claude-haiku-4-5");
  assert.equal(canonicalModel("claude", "claude-opus-4-8-20260401"), "claude-opus-4-8");
  assert.equal(canonicalModel("codex", "gpt-5-codex"), "gpt-5-codex");
  assert.equal(canonicalModel("grok", "grok-4.6-latest"), "grok-4.6");
  // Aliases are Claude's: on another harness the word is just a model id.
  assert.equal(canonicalModel("codex", "opus"), "opus");
  assert.equal(canonicalModel("claude", "inherit"), "");
  assert.equal(canonicalModel("claude", "  "), "");
});

test("priceFor: every table row is positive and self-consistent; unknown ids have no price", () => {
  for (const [id, p] of Object.entries(PRICES)) {
    assert.ok(p.input > 0 && p.output > 0, id);
    assert.ok(p.cache_read <= p.input, `${id}: cache read is a discount`);
    assert.ok(p.cache_write >= 0, id);
  }
  assert.deepEqual(priceFor("claude", "haiku"), {
    input: 1,
    output: 5,
    cache_read: 0.1,
    cache_write: 1.25,
  });
  assert.equal(priceFor("claude", "inherit"), undefined);
  assert.equal(priceFor("gemini", "gemini-2.5-pro"), undefined);
  assert.equal(priceFor("codex", "gpt-4o"), undefined);
});

test("costOf: Anthropic buckets are separate, OpenAI cached tokens sit inside input", () => {
  // Haiku 4.5: 1M in ($1) + 100k out ($0.50) + 500k cache read ($0.05) + 200k cache write ($0.25).
  assert.equal(costOf(u(), PRICES["claude-haiku-4-5"]!), 1.8);
  // gpt-5: 500k fresh in ($0.625) + 500k cached ($0.0625) + 100k out ($1.00); no write charge.
  assert.equal(costOf(u(), PRICES["gpt-5"]!), 1.6875);
  // A cached count larger than input never goes negative: the fresh part clamps to zero.
  const clamped = costOf(
    u({ input: 10, cache_read: 50, output: 0, cache_write: 0 }),
    PRICES["gpt-5"]!,
  );
  assert.ok(clamped >= 0 && clamped < 0.00001, String(clamped));
  assert.equal(
    costOf(u({ input: 0, output: 0, cache_read: 0, cache_write: 0 }), PRICES["gpt-5"]!),
    0,
  );
});

test("priceUsage: reported cost wins under either pool and is labelled reported", () => {
  const api = priceUsage(u({ cost_usd: 0.42 }), "claude", "haiku");
  assert.equal(api.usage.cost_usd, 0.42);
  assert.equal(api.usage.cost_source, "reported");
  assert.equal(api.unknownModel, undefined);
  const sub = priceUsage(u({ pool: "subscription", cost_usd: 0.07 }), "claude", "inherit");
  assert.equal(sub.usage.cost_usd, 0.07);
  assert.equal(sub.usage.cost_source, "reported");
});

test("priceUsage: tokens-only api-key usage is priced; subscription tokens-only stays none", () => {
  const priced = priceUsage(u(), "codex", "gpt-5-codex");
  assert.equal(priced.usage.cost_usd, 1.6875);
  assert.equal(priced.usage.cost_source, "priced");
  assert.equal(priced.unknownModel, undefined);
  const sub = priceUsage(u({ pool: "subscription" }), "codex", "gpt-5-codex");
  assert.equal(sub.usage.cost_usd, undefined);
  assert.equal(sub.usage.cost_source, "none");
});

test("priceUsage: unknown model under api-key is none and names the model; inherit reads as inherit", () => {
  const unknown = priceUsage(u(), "codex", "gpt-9-turbo");
  assert.equal(unknown.usage.cost_usd, undefined);
  assert.equal(unknown.usage.cost_source, "none");
  assert.equal(unknown.unknownModel, "gpt-9-turbo");
  assert.equal(priceUsage(u(), "claude", "").unknownModel, "inherit");
  assert.equal(priceUsage(u(), "claude", "inherit").unknownModel, "inherit");
});

test("priceUsage: already-labelled usage passes through untouched (no double pricing)", () => {
  const once = priceUsage(u(), "claude", "haiku").usage;
  const twice = priceUsage(once, "claude", "mystery");
  assert.deepEqual(twice.usage, once);
  assert.equal(twice.unknownModel, undefined);
  const input = u();
  priceUsage(input, "claude", "haiku");
  assert.equal(input.cost_source, undefined, "the input object is not mutated");
});
