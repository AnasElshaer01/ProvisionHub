"""Quick end-to-end DB smoke test.

Runs against the real provisionhub.db (created by `alembic upgrade head`):
1. Parse a SCIM payload with the Pydantic wire model
2. canonicalize() it
3. Save through UsersRepository
4. Read back by id, external_id, user_name
5. Deactivate, confirm row updated
"""

from __future__ import annotations

import asyncio

from provisionhub.db.session import SessionLocal
from provisionhub.db.users_repo import UsersRepository
from provisionhub.domain.scim import SCIMUser, canonicalize


SCIM_PAYLOAD = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "externalId": "entra-7f8d3a1e",
    "userName": "alice@contoso.com",
    "active": True,
    "name": {"givenName": "Alice", "familyName": "Smith"},
    "emails": [
        {"value": "alice@work.com", "primary": True, "type": "work"},
        {"value": "alice@home.com", "primary": False, "type": "home"},
    ],
    "displayName": "Alice Smith",  # extra — should be tolerated, not crash
}


async def main() -> None:
    scim = SCIMUser(**SCIM_PAYLOAD)
    user = canonicalize(scim)
    print("canonical user:", user.model_dump())

    async with SessionLocal() as session:
        repo = UsersRepository(session)

        created = await repo.create(user)
        await session.commit()
        print("created id:", created.id)

        by_id = await repo.get(created.id)
        by_ext = await repo.get_by_external_id("entra-7f8d3a1e")
        by_name = await repo.get_by_user_name("alice@contoso.com")
        assert by_id.id == by_ext.id == by_name.id
        assert len(by_id.emails) == 2
        assert by_id.emails[0].value == "alice@work.com"
        print("read-back ok:", by_id.user_name, [e.value for e in by_id.emails])

        deactivated = await repo.set_active(created.id, False)
        await session.commit()
        assert deactivated.active is False
        print("deactivated:", deactivated.active)

        listed = await repo.list_(user_name="alice@contoso.com")
        assert len(listed) == 1
        print("filter list ok:", len(listed), "row(s)")


if __name__ == "__main__":
    asyncio.run(main())
