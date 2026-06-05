#!/usr/bin/env python
"""Full report of provisioning activity — inbound SCIM + outbound connector calls."""

import asyncio
import json
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from src.provisionhub.config import settings
from src.provisionhub.db.orm import UserORM, ProvisioningEventORM, ConnectorCallORM, UserRemoteIdORM


async def run_report():
    """Query and display provisioning flow: inbound → outbound."""
    engine = create_async_engine(settings.database_url, echo=False)

    async with AsyncSession(engine) as session:
        # --- Inbound: Users created ---
        print("\n" + "="*80)
        print("INBOUND — Users Created")
        print("="*80)
        users = await session.execute(select(UserORM))
        user_rows = users.scalars().all()

        if not user_rows:
            print("(no users)")
        else:
            for u in user_rows:
                print(f"\n  ID: {u.id}")
                print(f"  External ID: {u.external_id}")
                print(f"  Username: {u.user_name}")
                print(f"  Active: {u.active}")
                print(f"  Created: {u.created_at}")

        # --- Inbound: Provisioning events ---
        print("\n" + "="*80)
        print("INBOUND — Provisioning Events (audit)")
        print("="*80)
        events = await session.execute(
            select(ProvisioningEventORM).order_by(ProvisioningEventORM.received_at)
        )
        event_rows = events.scalars().all()

        if not event_rows:
            print("(no events)")
        else:
            for evt in event_rows:
                user_name = None
                if user_rows:
                    for u in user_rows:
                        if u.id == evt.user_id:
                            user_name = u.user_name
                            break

                print(f"\n  Event ID: {evt.id}")
                print(f"  Correlation ID: {evt.correlation_id}")
                print(f"  Operation: {evt.op}")
                print(f"  User: {user_name} ({evt.user_id})")
                print(f"  Received: {evt.received_at}")
                if evt.payload:
                    print(f"  Payload (excerpt): {json.dumps(evt.payload, indent=4)[:200]}...")

        # --- Outbound: Connector calls ---
        print("\n" + "="*80)
        print("OUTBOUND — Connector Calls (per attempt)")
        print("="*80)
        calls = await session.execute(
            select(ConnectorCallORM).order_by(ConnectorCallORM.created_at)
        )
        call_rows = calls.scalars().all()

        if not call_rows:
            print("(no connector calls)")
        else:
            for call in call_rows:
                status_emoji = "✓" if call.succeeded else "✗"
                print(f"\n  {status_emoji} Connector: {call.connector}")
                print(f"     Event: {call.event_id}")
                print(f"     Endpoint: {call.endpoint}")
                print(f"     Attempt: {call.attempt}")
                print(f"     Succeeded: {call.succeeded}")
                if call.response_status:
                    print(f"     HTTP Status: {call.response_status}")
                if call.latency_ms:
                    print(f"     Latency: {call.latency_ms}ms")
                if call.error:
                    print(f"     Error: {call.error}")
                if call.response:
                    print(f"     Response (excerpt): {json.dumps(call.response, indent=4)[:200]}...")
                print(f"     Timestamp: {call.created_at}")

        # --- Outbound: Remote IDs (results) ---
        print("\n" + "="*80)
        print("OUTBOUND — Remote IDs (downstream identities)")
        print("="*80)
        remotes = await session.execute(select(UserRemoteIdORM))
        remote_rows = remotes.scalars().all()

        if not remote_rows:
            print("(no remote IDs)")
        else:
            for remote in remote_rows:
                user_name = None
                if user_rows:
                    for u in user_rows:
                        if u.id == remote.user_id:
                            user_name = u.user_name
                            break

                print(f"\n  User: {user_name} ({remote.user_id})")
                print(f"  Connector: {remote.connector}")
                print(f"  Remote ID: {remote.remote_id}")

        # --- Summary ---
        print("\n" + "="*80)
        print("SUMMARY")
        print("="*80)
        print(f"  Users created: {len(user_rows)}")
        print(f"  Inbound events: {len(event_rows)}")
        print(f"  Connector calls: {len(call_rows)}")
        print(f"  Remote IDs stored: {len(remote_rows)}")
        print()

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_report())
