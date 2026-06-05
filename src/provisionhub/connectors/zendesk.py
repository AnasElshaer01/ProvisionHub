"""Zendesk REST API v2 connector.

Provisions users in Zendesk via REST API v2. Auth is HTTP Basic (email/token:api_token).
Idempotency: lookup by email before create.

Zendesk user operations:
- create_user: search by email → POST /api/v2/users.json if not found → return user id
- update_user: PUT /api/v2/users/{id}.json with name
- deactivate_user: PUT /api/v2/users/{id}.json with suspended=true
"""

from __future__ import annotations

import httpx

from ..domain.user import User
from .base import Connector


class ZendeskConnector(Connector):
    name = "zendesk"

    def __init__(self, subdomain: str, email: str, api_token: str, org_id: str = "") -> None:
        """Create a Zendesk connector.

        Args:
            subdomain: Zendesk subdomain (e.g., "yourcompany" from yourcompany.zendesk.com)
            email: Zendesk admin email
            api_token: API token (from Admin → Apps & Integrations → API Tokens)
            org_id: Optional Zendesk organization id. When set, created users are
                attached to this organization. Unset → users land with no org.
        """
        self._org_id = org_id
        self._client = httpx.AsyncClient(
            base_url=f"https://{subdomain}.zendesk.com",
            auth=httpx.BasicAuth(f"{email}/token", api_token),
            headers={"Content-Type": "application/json"},
        )

    async def create_user(self, user: User) -> str:
        """Provision user in Zendesk. Returns user id (the remote_id).

        Idempotency: search by email first. If found, return existing id.
        Otherwise POST /api/v2/users.json and return the new id.
        """
        # Build email to search by.
        email = None
        if user.emails and user.emails[0].value:
            email = user.emails[0].value
        elif user.user_name:
            email = user.user_name

        if email:
            # Idempotency check: lookup by email.
            search_resp = await self._client.get(
                "/api/v2/users/search.json",
                params={"query": f"email:{email}"},
            )
            search_resp.raise_for_status()
            results = search_resp.json()
            if results.get("users"):
                # Account already exists — return its id as string.
                return str(results["users"][0]["id"])

        # Build display name.
        if user.given_name and user.family_name:
            name = f"{user.given_name} {user.family_name}"
        elif user.given_name:
            name = user.given_name
        elif user.family_name:
            name = user.family_name
        else:
            name = user.user_name

        user_payload = {
            "name": name,
            "email": email,
            "role": "agent",
        }
        if self._org_id:
            user_payload["organization_id"] = int(self._org_id)

        create_resp = await self._client.post(
            "/api/v2/users.json",
            json={"user": user_payload},
        )
        create_resp.raise_for_status()
        return str(create_resp.json()["user"]["id"])

    async def update_user(self, remote_id: str, user: User) -> None:
        """Update user name in Zendesk.

        Note: Zendesk API v2 supports limited user profile updates via this endpoint.
        Email/organization changes may require admin operations.
        This updates name only.
        """
        name = user.user_name
        if user.given_name and user.family_name:
            name = f"{user.given_name} {user.family_name}"
        elif user.given_name:
            name = user.given_name
        elif user.family_name:
            name = user.family_name

        resp = await self._client.put(
            f"/api/v2/users/{remote_id}.json",
            json={
                "user": {
                    "name": name,
                }
            },
        )
        resp.raise_for_status()

    async def deactivate_user(self, remote_id: str) -> None:
        """Deactivate user in Zendesk by suspending them.

        This soft-deactivates the user (suspended=true) but does not
        permanently delete them. Reversible by unsuspending.
        """
        resp = await self._client.put(
            f"/api/v2/users/{remote_id}.json",
            json={
                "user": {
                    "suspended": True,
                }
            },
        )
        resp.raise_for_status()
