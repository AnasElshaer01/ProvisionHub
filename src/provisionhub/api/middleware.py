"""Correlation-ID middleware.

Reads X-Correlation-ID from the request if present, else generates a uuid4.
Stashes it on `request.state.correlation_id` so handlers (and the events
repo) can pick it up, and echoes it in the response header so callers can
quote it in support tickets.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

CORRELATION_HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = correlation_id
        return response
