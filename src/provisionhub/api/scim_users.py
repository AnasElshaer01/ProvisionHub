"""SCIM 2.0 /Users router — POST + GET only for now.

Order of operations on POST:
  1. parse SCIM payload -> canonical User
  2. idempotency: lookup by externalId; if found, return existing (200)
  3. create user row
  4. write provisioning_events row (inbound audit)
  5. build ProvisioningJob and submit to JobQueue  <-- the seam
  6. commit, return SCIM-shaped response

The route NEVER calls ProvisioningService directly. It submits a job.
That's CLAUDE.md rule #1.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.events_repo import EventsRepository
from ..db.session import get_session
from ..db.users_repo import UserNotFoundError, UsersRepository
from ..domain.scim import SCIMUser, canonicalize
from ..domain.user import User
from ..provisioning.job import ProvisioningJob
from ..provisioning.queue import JobQueue
from .auth import require_bearer

router = APIRouter(prefix="/scim/v2", tags=["scim"], dependencies=[Depends(require_bearer)])


# --- queue dependency -------------------------------------------------------
# main.py overrides this so the route gets the real singleton JobQueue.
def get_queue() -> JobQueue:  # pragma: no cover - replaced at app startup
    raise RuntimeError("JobQueue dependency not wired; main.py should override this")


# --- SCIM response shaping --------------------------------------------------
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"


def to_scim(user: User) -> dict:
    """Canonical User -> SCIM response body."""
    return {
        "schemas": [USER_SCHEMA],
        "id": user.id,
        "externalId": user.external_id,
        "userName": user.user_name,
        "active": user.active,
        "name": {
            "givenName": user.given_name,
            "familyName": user.family_name,
        },
        "emails": [{"value": e.value, "primary": e.primary} for e in user.emails],
        "meta": {"resourceType": "User"},
    }


# --- POST /scim/v2/Users ----------------------------------------------------
@router.post("/Users", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    queue: JobQueue = Depends(get_queue),
) -> dict:
    scim = SCIMUser(**payload)
    user = canonicalize(scim)

    users = UsersRepository(session)
    events = EventsRepository(session)

    # Idempotency: re-POST with same externalId returns the existing user
    # with 200 (not 201 — nothing was created). Entra retries on transient
    # failure and this is the path that catches those.
    if user.external_id:
        existing = await users.get_by_external_id(user.external_id)
        if existing:
            return JSONResponse(status_code=status.HTTP_200_OK, content=to_scim(existing))

    # userName is UNIQUE — translate IntegrityError into a SCIM 409 explicitly.
    if await users.get_by_user_name(user.user_name):
        raise HTTPException(status.HTTP_409_CONFLICT, "userName already exists")

    created = await users.create(user)

    event_id = await events.record_inbound(
        correlation_id=request.state.correlation_id,
        op="create",
        user_id=created.id,
        payload=payload,
    )

    # Commit BEFORE submitting. The service opens its own session to load
    # the user; that session can only see committed data. (This also makes
    # the audit row durable before any background worker starts — switching
    # to the async-queue mode later doesn't change correctness here.)
    await session.commit()

    await queue.submit(ProvisioningJob(
        event_id=event_id,
        correlation_id=request.state.correlation_id,
        user_id=created.id,
        op="create",
    ))

    return to_scim(created)


# --- GET /scim/v2/Users/{id} ------------------------------------------------
@router.get("/Users/{user_id}")
async def get_user(user_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    users = UsersRepository(session)
    try:
        return to_scim(await users.get(user_id))
    except UserNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"user {user_id} not found")


# --- GET /scim/v2/Users  (list, with one filter: userName eq "x") -----------
@router.get("/Users")
async def list_users(
    filter: str | None = None,  # noqa: A002 - SCIM names the query param `filter`
    session: AsyncSession = Depends(get_session),
) -> dict:
    user_name = _parse_username_filter(filter)
    users = UsersRepository(session)
    rows = await users.list_(user_name=user_name)
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": len(rows),
        "Resources": [to_scim(u) for u in rows],
    }


def _parse_username_filter(expr: str | None) -> str | None:
    """The only filter we honor: `userName eq "x"`.

    Anything else -> 400. Documented gap; complex filters are a v2 concern.
    """
    if expr is None:
        return None
    parts = expr.strip().split(None, 2)
    if len(parts) == 3 and parts[0] == "userName" and parts[1].lower() == "eq":
        return parts[2].strip().strip('"').strip("'")
    raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unsupported filter: {expr!r}")
