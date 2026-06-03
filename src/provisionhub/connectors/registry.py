"""ConnectorRegistry — the ONE place that knows which connectors exist.

`main.py` calls `registry.register(...)` once per app at startup. The
service iterates `registry.enabled()` and never names a connector.

Duplicate-name registration raises rather than silently overwriting; a
double-register is almost always a wiring mistake.
"""

from __future__ import annotations

from collections.abc import Iterable

from .base import Connector


class ConnectorRegistry:
    def __init__(self) -> None:
        # dict preserves insertion order -> deterministic iteration for tests.
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector) -> None:
        if connector.name in self._connectors:
            raise ValueError(f"connector already registered: {connector.name!r}")
        self._connectors[connector.name] = connector

    def enabled(self) -> Iterable[Connector]:
        """Yield the connectors the service should run, in registration order.

        Today: everything registered. Tomorrow: filter by an enabled flag
        or per-tenant policy. Callers don't care which.
        """
        return self._connectors.values()

    def get(self, name: str) -> Connector:
        return self._connectors[name]

    def __len__(self) -> int:
        return len(self._connectors)
