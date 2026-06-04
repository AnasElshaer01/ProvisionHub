"""Jira Cloud REST API v3 connector.

Provisions users in Jira Cloud via the REST API v3. Auth is HTTP Basic
(email:api_token). Idempotency: lookup by email before create.

Jira Cloud user operations:
- create_user: search by email → POST /user if not found → return accountId
- update_user: PUT /user?accountId with displayName
- deactivate_user: DELETE /user?accountId
"""

from __future__ import annotations

import httpx

from ..domain.user import User
from .base import Connector


class JiraConnector(Connector):
    name = "jira"

    def __init__(self, base_url: str, email: str, token: str) -> None:
        """Create a Jira connector.

        Args:
            base_url: Jira instance base URL, e.g., https://yourorg.atlassian.net
            email: Jira admin email for API token auth
            token: Jira API token (from id.atlassian.com/manage-profile/security/api-tokens)
        """
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/rest/api/3",
            auth=httpx.BasicAuth(email, token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )

    async def create_user(self, user: User) -> str:
        """Provision user in Jira. Returns accountId (the remote_id).

        Idempotency: search by email first. If found, return existing accountId.
        Otherwise POST /user and return the new accountId.
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
                "/user/search",
                params={"query": email, "maxResults": 1},
            )
            search_resp.raise_for_status()
            results = search_resp.json()
            if results:
                # Account already exists — return its accountId.
                return results[0]["accountId"]

        # Build display name.
        if user.given_name and user.family_name:
            display_name = f"{user.given_name} {user.family_name}"
        elif user.given_name:
            display_name = user.given_name
        elif user.family_name:
            display_name = user.family_name
        else:
            display_name = user.user_name

        # Create the user.
        create_resp = await self._client.post(
            "/user",
            json={
                "emailAddress": email,
                "displayName": display_name,
                "products": [],
            },
        )
        create_resp.raise_for_status()
        return create_resp.json()["accountId"]

    async def update_user(self, remote_id: str, user: User) -> None:
        """Update user display name in Jira.

        Note: Jira Cloud v3 REST API has limited user profile update support.
        Email changes require Atlassian org-level admin operations.
        This updates displayName only.
        """
        display_name = user.user_name
        if user.given_name and user.family_name:
            display_name = f"{user.given_name} {user.family_name}"
        elif user.given_name:
            display_name = user.given_name
        elif user.family_name:
            display_name = user.family_name

        resp = await self._client.put(
            "/user",
            params={"accountId": remote_id},
            json={"displayName": display_name},
        )
        resp.raise_for_status()

    async def deactivate_user(self, remote_id: str) -> None:
        """Deactivate user in Jira by deleting their Jira account.

        This soft-deactivates the user (removes from Jira instance) but does
        not delete the underlying Atlassian account.
        """
        resp = await self._client.delete(
            "/user",
            params={"accountId": remote_id},
        )
        resp.raise_for_status()
