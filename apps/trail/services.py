from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from django.db import transaction

from apps.identity.models import Organization, User
from apps.trail.models import LedgerHead, TrailEntry


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
        # Serialize appends per tenant by locking the LEDGER_HEAD row (ADR-0008): the lock
        # and the chain tip are the same row. The first append for a tenant seeds the row;
        # every later append takes the row lock, reads the tip, and advances it. The lock is
        # held to commit, so no concurrent writer of the same tenant reads a stale tip.
        head, _ = LedgerHead.objects.select_for_update().get_or_create(
            organization_id=organization.pk,
            defaults={"ultima_secuencia": 0, "ultimo_hash": ""},
        )
        sequence = head.ultima_secuencia + 1
        previous_hash = head.ultimo_hash
        entry_hash = _hash_entry(
            organization_id=organization.id,
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
        entry = TrailEntry.objects.create(
            organization_id=organization.pk,
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
        head.ultima_secuencia = sequence
        head.ultimo_hash = entry_hash
        head.save(update_fields=["ultima_secuencia", "ultimo_hash", "updated_at"])
        return entry


def next_sequence_for_organization(organization: Organization) -> int:
    """Return the next trail sequence for read-only previews/tests.

    Do not use this helper to allocate sequences; use ``append_trail_entry`` so the
    LEDGER_HEAD row lock serializes concurrent writers.
    """

    head = LedgerHead.objects.filter(organization=organization).first()
    return (head.ultima_secuencia if head else 0) + 1


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
