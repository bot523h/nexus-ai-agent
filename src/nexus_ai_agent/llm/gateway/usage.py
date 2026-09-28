"""Truthful usage and cost accounting (W2, LAW 11 — never invent a number).

Two rules, both enforced structurally:

1. Token counts come from the provider or they are ``None``. The gateway never
   estimates tokens from characters and never reports an estimate as if it were
   a measurement: ``Usage.source`` is either ``PROVIDER`` or ``UNKNOWN``.
2. A cost is computed only when *both* inputs exist — provider-reported tokens
   and an operator-pinned price for the answering model. Otherwise
   ``estimated_cost_usd`` is ``None``.

Why the price table is tiny and explicit rather than comprehensive: a stale
price is a fabricated number with extra confidence attached. Prices are pinned
per model, the table carries a version string, and any model not listed yields
``None`` (rendered as ``usage: unknown`` in observability) instead of a
plausible-looking guess. Only an explicitly free endpoint or an operator-pinned
zero price means free.
A model name alone does not identify the billing contract or local hosting cost.

Token-per-minute rate limiting is *not* implemented here on purpose: it needs
provider-reported usage for the request that is about to be sent, which no
provider gives us in advance. Pretending otherwise would mean estimating tokens
from characters and throttling on a guess. Recorded as a residual gap in the W2
report instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

from nexus_ai_agent.llm.gateway.contract import Usage, UsageSource

__all__ = [
    "DEFAULT_PRICE_TABLE",
    "PRICE_TABLE_VERSION",
    "ModelPrice",
    "apply_cost",
    "price_for",
]

#: Bump whenever a price changes, so a cost figure can be tied to a table.
PRICE_TABLE_VERSION = "2026-09-28"


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens. ``None`` means "no pinned price", not "free"."""

    input_usd_per_million: float | None
    output_usd_per_million: float | None

    def __post_init__(self) -> None:
        for value in (self.input_usd_per_million, self.output_usd_per_million):
            if value is not None and (not isfinite(value) or value < 0):
                raise ValueError("pinned prices must be finite and nonnegative")

    @property
    def is_known(self) -> bool:
        return self.input_usd_per_million is not None and self.output_usd_per_million is not None


#: Models this repository actually configures (config/settings.py). Anything
#: absent is UNKNOWN — deliberately, and honestly.
DEFAULT_PRICE_TABLE: Mapping[str, ModelPrice] = {
    # Model identity does not prove which billing tier the credential uses.
    "gemini-2.0-flash": ModelPrice(None, None),
    "gemini-2.5-flash": ModelPrice(None, None),
    "gemini-2.5-flash-image": ModelPrice(None, None),
    # The same applies to Groq.
    "llama-3.3-70b-versatile": ModelPrice(None, None),
    # OpenRouter ":free" endpoints are priced at zero by definition.
    "meta-llama/llama-3.3-70b-instruct:free": ModelPrice(0.0, 0.0),
    # A local-model alias does not establish a hosting/billing contract.
    "local-model": ModelPrice(None, None),
}


def price_for(
    model: str,
    *,
    table: Mapping[str, ModelPrice] | None = None,
) -> ModelPrice | None:
    """Return the pinned price for *model*, or ``None`` when there is none."""

    source = DEFAULT_PRICE_TABLE if table is None else table
    return source.get(model)


def apply_cost(
    usage: Usage,
    *,
    model: str,
    table: Mapping[str, ModelPrice] | None = None,
) -> Usage:
    """Attach a cost to *usage* when — and only when — it can be computed truthfully.

    Returns the same object when nothing can be added, so this is safe to call
    unconditionally on every response.
    """

    if usage.source is not UsageSource.PROVIDER:
        return usage
    price = price_for(model, table=table)
    if price is None or not price.is_known:
        return usage
    if usage.input_tokens is None or usage.output_tokens is None:
        return usage
    cost = (
        usage.input_tokens * (price.input_usd_per_million or 0.0)
        + usage.output_tokens * (price.output_usd_per_million or 0.0)
    ) / 1_000_000.0
    return Usage(
        source=usage.source,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        estimated_cost_usd=round(cost, 8),
        reported_model=usage.reported_model,
    )
