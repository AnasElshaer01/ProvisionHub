"""Bearer-token auth dependency.

One static token in settings for v1. In a real deployment this would be a
JWT or a per-IdP token; the dependency signature stays the same either way.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status

from ..config import settings


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.scim_bearer_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")


# Re-exported as the route Depends() target — keeps the import in routes short.
AuthDep = Depends(require_bearer)
