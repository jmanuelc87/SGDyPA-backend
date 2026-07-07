from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import Max

from apps.identity.models import Organization, User
from apps.trail.models import TrailEntry


def append_trail_entry(
    *,
    organization: Organization,
    actor: User,
    action: str,
    target_entity: str,
    target_id: UUID,
    payload: dict[str, Any] | None = None,
) -> TrailEntry:
    """Synchronously append one audit ledger entry for a committed domain action.

    The caller should invoke this inside the same ``transaction.atomic()`` block as the
    domain write. If the outer transaction rolls back, this insert rolls back with it;
    if no transaction is active, this function still performs the sequence allocation
    and insert atomically.
    """

    entry_payload = payload or {}
    with transaction.atomic():
        # Serialize sequence allocation per tenant by locking the Organization row.
        locked_organization = Organization.objects.select_for_update().get(
            pk=organization.pk
        )
        previous_entry = (
            TrailEntry.objects.filter(organization=locked_organization)
            .order_by("-sequence")
            .first()
        )
        sequence = (previous_entry.sequence + 1) if previous_entry else 1
        previous_hash = previous_entry.entry_hash if previous_entry else ""
        entry_hash = _hash_entry(
            organization_id=locked_organization.id,
            actor_id=actor.id,
            actor_email_snapshot=actor.email,
            actor_display_name_snapshot=actor.display_name,
            action=action,
            target_entity=target_entity,
            target_id=target_id,
            payload=entry_payload,
            sequence=sequence,
            previous_hash=previous_hash,
        )
        return TrailEntry.objects.create(
            organization=locked_organization,
            actor=actor,
            actor_email_snapshot=actor.email,
            actor_display_name_snapshot=actor.display_name,
            action=action,
            target_entity=target_entity,
            target_id=target_id,
            payload=entry_payload,
            sequence=sequence,
            previous_hash=previous_hash,
            entry_hash=entry_hash,
        )


def next_sequence_for_organization(organization: Organization) -> int:
    """Return the next trail sequence for read-only previews/tests.

    Do not use this helper to allocate sequences; use ``append_trail_entry`` so the
    organization row lock serializes concurrent writers.
    """

    latest = TrailEntry.objects.filter(organization=organization).aggregate(
        max_sequence=Max("sequence")
    )["max_sequence"]
    return (latest or 0) + 1


def _hash_entry(
    *,
    organization_id: UUID,
    actor_id: int,
    actor_email_snapshot: str,
    actor_display_name_snapshot: str,
    action: str,
    target_entity: str,
    target_id: UUID,
    payload: dict[str, Any],
    sequence: int,
    previous_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "organization_id": str(organization_id),
            "actor_id": actor_id,
            "actor_email_snapshot": actor_email_snapshot,
            "actor_display_name_snapshot": actor_display_name_snapshot,
            "action": action,
            "target_entity": target_entity,
            "target_id": str(target_id),
            "payload": payload,
            "sequence": sequence,
            "previous_hash": previous_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
