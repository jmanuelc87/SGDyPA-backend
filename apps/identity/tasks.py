from __future__ import annotations

import logging
from typing import Any

from django.contrib.auth import get_user_model
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
    kind = payload.get("kind")

    # A GROUP event (the group itself, not a membership) carries no user, so it
    # is handled before the sub requirement below.
    if kind == "group":
        result_status = _apply_group(payload, operation)
        record.error = ""
        record.processed_at = timezone.now()
        record.save(update_fields=["error", "processed_at"])
        return {"event_id": event_id, "status": result_status}

    sub = str(payload.get("sub") or record.keycloak_sub)
    if not sub:
        record.error = "event carried no keycloak sub"
        record.processed_at = timezone.now()
        record.save(update_fields=["error", "processed_at"])
        return {"event_id": event_id, "status": "skipped_no_sub"}

    # "user" is the default kind for back-compat with events stored before the
    # group-membership path existed.
    if kind == "group_membership":
        result_status = _apply_group_membership(payload, operation, sub)
    elif operation in DELETE_OPERATIONS:
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


def _apply_group(payload: dict[str, Any], operation: str) -> str:
    """Pre-create or refresh the local Organization from a GROUP admin event."""

    from apps.identity.org_replication import GroupRef, sync_organization_from_group

    group_ref = GroupRef(
        keycloak_group_id=payload.get("group_id") or None,
        path=payload.get("group_path") or None,
        name=payload.get("group_name") or None,
    )
    org = sync_organization_from_group(group_ref, source=SOURCE)
    if org is None:
        return "skipped_no_group"
    return "org_created" if operation == "CREATE" else "org_synced"


def _apply_group_membership(payload: dict[str, Any], operation: str, sub: str) -> str:
    """Apply a GROUP_MEMBERSHIP event to the user's org memberships.

    Incremental (add on CREATE, soft-remove on DELETE). If the local User
    projection does not exist yet (a group event delivered before the user's
    CREATE event), skip idempotently — login or a later user event backfills.
    """

    from apps.identity.org_replication import (
        GroupRef,
        apply_group_membership_change,
    )

    UserModel = get_user_model()
    try:
        user = UserModel.objects.get(keycloak_sub=sub)
    except UserModel.DoesNotExist:
        logger.info(
            "keycloak.group_event.user_missing",
            extra={"keycloak_sub": sub, "operation": operation},
        )
        return "skipped_no_user"

    group_ref = GroupRef(
        keycloak_group_id=payload.get("group_id") or None,
        path=payload.get("group_path") or None,
        name=payload.get("group_name") or None,
    )
    apply_group_membership_change(
        user, group_ref, added=operation not in DELETE_OPERATIONS, source=SOURCE
    )
    return "group_joined" if operation not in DELETE_OPERATIONS else "group_removed"
