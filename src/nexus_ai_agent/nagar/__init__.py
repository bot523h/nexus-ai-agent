"""Nagar — the model-optional cognition layer (additive, authority-free).

The deterministic substrate lives in ``creative.studio`` (CommandBus,
capability registry, authorization, pack lifecycle) and ``jobs`` (durable
lifecycle, fencing, independent verification).  ``nagar.cognition`` is the
*reasoning* seam above that substrate: it proposes, it never authorizes and
it never executes.

See ``docs/overnight/RECON.md`` and ``docs/overnight/MODEL_OPTIONAL.md``.
"""

from __future__ import annotations

__all__: list[str] = []
