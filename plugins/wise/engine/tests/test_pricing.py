import pytest

from wise_engine.pricing import PRICES, canonical_model, cost_of, price_for, price_usage


def usage(**over):
    return (
        dict(
            input=1_000_000, output=100_000, cache_read=500_000, cache_write=200_000, pool="api-key"
        )
        | over
    )


@pytest.mark.parametrize(
    "harness,model,expected",
    [
        ("claude", "opus", "claude-opus-5"),
        ("claude", "Sonnet", "claude-sonnet-5"),
        ("claude", "haiku", "claude-haiku-4-5"),
        ("claude", "claude-opus-4-8-20260401", "claude-opus-4-8"),
        ("codex", "gpt-5-codex", "gpt-5-codex"),
        ("grok", "grok-4.6-latest", "grok-4.6"),
        ("codex", "opus", "opus"),
        ("claude", "inherit", ""),
        ("claude", "  ", ""),
    ],
)
def test_canonical_model(harness, model, expected):
    assert canonical_model(harness, model) == expected


def test_price_table():
    for price in PRICES.values():
        assert price["input"] > 0 and price["output"] > 0
        assert 0 <= price["cache_read"] <= price["input"]
        assert price["cache_write"] >= 0
    assert price_for("claude", "haiku") == dict(input=1, output=5, cache_read=0.1, cache_write=1.25)
    for harness, model in [
        ("claude", "inherit"),
        ("gemini", "gemini-2.5-pro"),
        ("codex", "gpt-4o"),
    ]:
        assert price_for(harness, model) is None


def test_cost_buckets_and_rounding():
    assert cost_of(usage(), PRICES["claude-haiku-4-5"]) == 1.8
    assert cost_of(usage(), PRICES["gpt-5"]) == 1.6875
    assert (
        0
        <= cost_of(usage(input=10, cache_read=50, output=0, cache_write=0), PRICES["gpt-5"])
        < 0.00001
    )
    assert cost_of(usage(input=0, output=0, cache_read=0, cache_write=0), PRICES["gpt-5"]) == 0
    assert (
        cost_of(
            usage(input=1, output=0, cache_read=0, cache_write=0),
            dict(input=0.5, output=0, cache_read=0, cache_write=0),
        )
        == 0.000001
    )


@pytest.mark.parametrize("pool", ["api-key", "subscription"])
def test_reported_cost_wins(pool):
    result = price_usage(usage(pool=pool, cost_usd=0.42), "claude", "inherit")
    assert result["usage"]["cost_usd"] == 0.42
    assert result["usage"]["cost_source"] == "reported"
    assert "unknownModel" not in result


def test_priced_subscription_unknown_and_idempotent():
    original = usage()
    priced = price_usage(original, "codex", "gpt-5-codex")
    assert priced["usage"]["cost_usd"] == 1.6875
    assert priced["usage"]["cost_source"] == "priced"
    assert "cost_source" not in original
    assert price_usage(priced["usage"], "codex", "mystery")["usage"] is priced["usage"]
    sub = price_usage(usage(pool="subscription"), "codex", "gpt-5-codex")
    assert sub["usage"]["cost_source"] == "none"
    assert "cost_usd" not in sub["usage"]
    for pin in ("gpt-9-turbo", "", "inherit"):
        result = price_usage(usage(), "codex", pin)
        assert result["unknownModel"] == (pin or "inherit")
        assert result["usage"]["cost_source"] == "none"
        assert "cost_usd" not in result["usage"]
