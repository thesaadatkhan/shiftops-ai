"""Estimated USD cost of model calls, computed in Python from provider-reported token usage.

Display only: budgets and stops are enforced on tokens and calls in agent.py, never on these estimates.

Source: OpenAI API pricing, https://developers.openai.com/api/docs/pricing
        (model page https://developers.openai.com/api/docs/models/gpt-5.6-luna), verified 2026-10-10.
Rates are USD per 1M tokens, Standard tier (the agent sends no service_tier, so the default tier is assumed).
Prompts over 272K input tokens use the long-context rates for the whole request; the agent's 20K-token
allowance keeps runs far below that, but the rule is applied anyway.

Usage accounting (https://developers.openai.com/api/docs/guides/reasoning and the SDK's ResponseUsage):
- cached_tokens and cache_write_tokens are breakdowns of input_tokens, priced at their own rates;
  the remaining input tokens are priced as uncached input.
- Reasoning tokens are billed as output and are already included in output_tokens, so they are not added again.
- Local Python/SQLite tools have no API charge; their results cost money only as input to later model calls.
"""

PRICING = {
    "gpt-5.6-luna": {
        "currency": "USD",
        "tier": "Standard",
        "verified": "2026-10-10",
        "source": "https://developers.openai.com/api/docs/pricing",
        "per_million": {"input": 0.20, "cached_input": 0.02, "cache_write": 0.25, "output": 1.20},
        "long_context_over_input_tokens": 272_000,
        "long_context_per_million": {"input": 0.40, "cached_input": 0.04, "cache_write": 0.50, "output": 1.80},
    },
}


def rates_for(model):
    """Verified rates for a model, or None if this project has not verified its price."""
    return PRICING.get(model)


def estimate_cost(rates, input_tokens, output_tokens, cached_tokens, cache_write_tokens):
    """Estimated USD for one call, or None ("unknown") if pricing or any needed usage number is missing
    or inconsistent. Never guesses a missing number."""
    counts = (input_tokens, output_tokens, cached_tokens, cache_write_tokens)
    if rates is None or any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in counts):
        return None
    if cached_tokens + cache_write_tokens > input_tokens:
        return None
    long_context = input_tokens > rates["long_context_over_input_tokens"]
    per_million = rates["long_context_per_million" if long_context else "per_million"]
    uncached = input_tokens - cached_tokens - cache_write_tokens
    return (uncached * per_million["input"]
            + cached_tokens * per_million["cached_input"]
            + cache_write_tokens * per_million["cache_write"]
            + output_tokens * per_million["output"]) / 1_000_000


def format_usd(amount):
    return "unknown" if amount is None else f"${amount:.6f}"


def describe(rates):
    if rates is None:
        return "Estimated cost: unknown (no verified price for this model)"
    r = rates["per_million"]
    return (f"Estimated cost uses {rates['tier']}-tier {rates['currency']} rates per 1M tokens, verified "
            f"{rates['verified']}: input ${r['input']:.2f}, cached input ${r['cached_input']:.2f}, "
            f"cache write ${r['cache_write']:.2f}, output ${r['output']:.2f}")
