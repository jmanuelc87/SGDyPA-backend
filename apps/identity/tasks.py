from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

from apps.identity.models import KeycloakReplicationEvent
from apps.identity.replication import (
    ProjectionAttributes,
    deactivate_user_projection,
    upsert_user_projection,
)
from celery import shared_task  # type: ignore[import-untyped]

logger = logging.getLogger("apps.identity.tasks")

DELETE_OPERATIONS = frozenset({"DELETE"})
SOURCE = "admin-event"


@shared_task(  # type: ignore[untyped-decorator]
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    max_retries=5,
)
def process_keycloak_admin_event(
    self: object,
    *,
    event_id: str,
) -> dict[str, Any]:
    """Apply a deduped Keycloak admin event to the local User projection.

    Idempotent: the event row is created by the webhook before enqueue, and a
    non-null ``processed_at`` short-circuits re-runs (Celery retries, redelivery)
    so a projection write is applied at most once per Keycloak event.
    """

    try:
        record = KeycloakReplicationEvent.objects.get(event_id=event_id)
    except KeycloakReplicationEvent.DoesNotExist:
        logger.warning("keycloak.event.missing_record", extra={"event_id": event_id})
        return {"event_id": event_id, "status": "missing"}

    if record.processed_at is not None:
        return {"event_id": event_id, "status": "already_processed"}

    payload = record.payload or {}
    operation = str(payload.get("operation") or record.operation).upper()
    sub = str(payload.get("sub") or record.keycloak_sub)

    if not sub:
        record.error = "event carried no keycloak sub"
        record.processed_at = timezone.now()
        record.save(update_fields=["error", "processed_at"])
        return {"event_id": event_id, "status": "skipped_no_sub"}

    if operation in DELETE_OPERATIONS:
        deactivate_user_projection(sub, source=SOURCE)
        result_status = "deactivated"
    else:
        representation = payload.get("representation") or {}
        attrs = ProjectionAttributes.from_representation(representation)
        _, created, _ = upsert_user_projection(sub, attrs, source=SOURCE)
        result_status = "created" if created else "updated"

    record.error = ""
    record.processed_at = timezone.now()
    record.save(update_fields=["error", "processed_at"])

    return {"event_id": event_id, "status": result_status, "sub": sub}
