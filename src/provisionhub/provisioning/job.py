"""ProvisioningJob — work as data.

The SCIM route builds one of these and hands it to JobQueue.submit().
It carries everything the ProvisioningService needs to do its work
without the route or queue having any opinion on what that work is.

`op` is the lifecycle verb: create | update | patch | delete. Each maps
to a method on the Connector ABC. The service does the dispatch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Op = Literal["create", "update", "patch", "delete"]


class ProvisioningJob(BaseModel):
    event_id: str        # FK back to provisioning_events.id, the audit row
    correlation_id: str  # request-scoped trace id
    user_id: str         # FK to users.id; service loads canonical User from this
    op: Op
