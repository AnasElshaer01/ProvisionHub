"""Retry helper — 3 attempts, exponential backoff, classifies retriable errors.

Used by ProvisioningService to wrap connector calls. Pure helper: it does
NOT write `connector_calls` rows. The service does, so the audit-write
contract lives in exactly one place.

Retriable:
  - httpx.HTTPStatusError where 5xx or 429
  - httpx.TransportError (DNS, connect, read, timeout)

Not retriable:
  - 4xx other than 429 (these are bugs in our mapping; retrying just
    spams the audit table and wastes the user's time)
  - Anything else (assume bug, fail fast)

`sleep` is injectable so tests can pass an AsyncMock and run instantly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx


# (1s, 4s, 16s) — exponential, matches PLAN.md and CLAUDE.md.
DEFAULT_BACKOFFS_S: tuple[float, ...] = (1.0, 4.0, 16.0)


@dataclass
class AttemptOutcome:
    """One execution of the wrapped call. The service writes one
    connector_calls row per outcome — success or failure."""

    attempt: int                       # 1-indexed
    succeeded: bool
    result: Any = None                 # what the call returned (on success)
    error: BaseException | None = None
    response_status: int | None = None # populated for HTTPStatusError
    latency_ms: int | None = None      # filled by the caller (run_with_retry times each call)


def is_retriable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code == 429
    if isinstance(exc, httpx.TransportError):
        return True
    return False


async def run_with_retry(
    call: Callable[[], Awaitable[Any]],
    *,
    backoffs_s: tuple[float, ...] = DEFAULT_BACKOFFS_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[AttemptOutcome]:
    """Invoke `call` up to len(backoffs_s) times. Returns one AttemptOutcome per
    attempt — the caller writes them all to the audit table.

    Stops early on success or on a non-retriable error.
    """
    outcomes: list[AttemptOutcome] = []
    max_attempts = len(backoffs_s)

    import time as _time  # local import — keeps the helper dependency-free at module level

    for i, _ in enumerate(backoffs_s, start=1):
        start = _time.perf_counter()
        try:
            result = await call()
        except BaseException as exc:  # noqa: BLE001 — we classify below
            latency_ms = int((_time.perf_counter() - start) * 1000)
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            outcomes.append(AttemptOutcome(
                attempt=i, succeeded=False, error=exc,
                response_status=status, latency_ms=latency_ms,
            ))
            if not is_retriable(exc) or i == max_attempts:
                return outcomes
            await sleep(backoffs_s[i - 1])
            continue
        latency_ms = int((_time.perf_counter() - start) * 1000)
        outcomes.append(AttemptOutcome(
            attempt=i, succeeded=True, result=result, latency_ms=latency_ms,
        ))
        return outcomes

    return outcomes  # unreachable when backoffs_s is non-empty
