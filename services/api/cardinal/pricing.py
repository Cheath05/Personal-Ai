"""Claude list prices (USD per million tokens) and cost math. Local models cost nothing."""

# input, output. Cache reads bill at 0.1x input, 5-minute cache writes at 1.25x input.
CLAUDE_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-5-5": (4.00, 20.00),
}
# Unknown models are priced like Opus so the budget errs on the safe side.
FALLBACK_PRICE = (5.00, 25.00)


def claude_cost(model: str, input_tokens: int, output_tokens: int,
                cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
    price_in, price_out = CLAUDE_PRICES.get(model, FALLBACK_PRICE)
    return (
        input_tokens * price_in
        + cache_read_tokens * price_in * 0.1
        + cache_write_tokens * price_in * 1.25
        + output_tokens * price_out
    ) / 1_000_000


def max_call_cost(model: str, max_tokens: int, est_input_tokens: int) -> float:
    """Worst-case cost of one call, used to avoid overshooting the monthly cap."""
    return claude_cost(model, est_input_tokens, max_tokens)
