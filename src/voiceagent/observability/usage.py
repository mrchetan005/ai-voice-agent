"""Session usage accumulation -> cost.

The livekit bridge (or any channel) feeds normalized usage snapshots/deltas
into a :class:`UsageCollector`; at session end ``finalize()`` yields the raw
quantities plus the priced total from :mod:`voiceagent.observability.cost`.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from voiceagent.observability.cost import compute_cost


class UsageCollector:
    def __init__(self) -> None:
        self._units: defaultdict[str, defaultdict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )

    def add(self, provider: str, unit: str, amount: float) -> None:
        try:
            value = float(amount)
        except (TypeError, ValueError):
            return
        self._units[provider][unit] += value

    def merge(self, usage: dict[str, dict[str, float]]) -> None:
        """Accumulate a delta of {provider: {unit: amount}}."""
        for provider, units in usage.items():
            for unit, amount in units.items():
                self.add(provider, unit, amount)

    def replace(self, usage: dict[str, dict[str, float]]) -> None:
        """Overwrite with a cumulative snapshot (for sources that re-send totals)."""
        for provider, units in usage.items():
            for unit, amount in units.items():
                try:
                    value = float(amount)
                except (TypeError, ValueError):
                    continue
                self._units[provider][unit] = value

    @property
    def usage(self) -> dict[str, dict[str, float]]:
        return {p: dict(u) for p, u in self._units.items()}

    def finalize(self) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
        usage = self.usage
        return usage, compute_cost(usage)
