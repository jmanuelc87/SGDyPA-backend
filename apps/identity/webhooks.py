from __future__ import annotations

import hmac
import json
import logging
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from apps.identity.models import KeycloakReplicationEvent
from apps.identity.tasks import process_keycloak_admin_event
from apps.platform.api_errors import build_error_json_response
from apps.platform.error_codes import ErrorCode

logger = logging.getLogger("apps.identity.webhooks")

# Keycloak resource types we replicate. USER changes drive the user projection;
# GROUP (create/update) pre-creates the local Organization and GROUP_MEMBERSHIP
# changes drive org-membership replication (both when enabled). All other event
# types (role mappings, client/realm config) are acked and ignored.
USER_RESOURCE_TYPE = "USER"
GROUP_RESOURCE_TYPE = "GROUP"
GROUP_MEMBERSHIP_RESOURCE_TYPE = "GROUP_MEMBERSHIP"

# Admin operations that carry a user representation to project.
UPSERT_OPERATIONS = frozenset({"CREATE", "UPDATE"})
DELETE_OPERATIONS = frozenset({"DELETE"})

# GROUP operations that pre-create/refresh the local Organization. DELETE is
# intentionally excluded: the Organization is PROTECT-referenced and a group
# deletion should not cascade-remove it.
GROUP_OPERATIONS = frozenset({"CREATE", "UPDATE"})

# GROUP_MEMBERSHIP operations: a user joined (CREATE) or left (DELETE) a group.
GROUP_MEMBERSHIP_OPERATIONS = frozenset({"CREATE", "DELETE"})


def _org_replication_enabled() -> bool:
    config = getattr(settings, "KEYCLOAK_ORG_REPLICATION", {}) or {}
    return bool(config.get("ENABLED"))


def _webhook_config() -> dict[str, Any]:
    return getattr(settings, "KEYCLOAK_WEBHOOK", {}) or {}


def verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Constant-time HMAC-SHA256 verification of the raw request body.

    Tolerates an optional ``sha256=`` prefix on the header value (some webhook
    extensions add it). Authentication is by shared secret, NOT JWT — this
    endpoint is a machine-to-machine callback, not a user-facing request.
    """

    if not signature_header:
        return False

    provided = signature_header.strip()
    if provided.lower().startswith("sha256="):
        provided = provided[len("sha256=") :]

    expected = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


@dataclass(frozen=True)
class AdminUserEvent:
    event_id: str
    event_type: str
    operation: str
    sub: str
    representation: dict[str, Any] | None


@dataclass(frozen=True)
class AdminGroupMembershipEvent:
    event_id: str
    event_type: str
    operation: str
    sub: str
    group_id: str
    group_path: str
    group_name: str


@dataclass(frozen=True)
class AdminGroupEvent:
    event_id: str
    event_type: str
    operation: str
    group_id: str
    group_path: str
    group_name: str


def _coerce_representation(value: Any) -> dict[str, Any] | None:
    """Keycloak admin events carry ``representation`` as a JSON string."""

    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _extract_sub(resource_path: Any, representation: dict[str, Any] | None) -> str:
    """Derive the Keycloak user id (the ``sub``) from the event.

    Prefers the representation's ``id``; falls back to the ``users/{id}`` segment
    of the admin ``resourcePath`` (which may have trailing sub-resource
    segments, e.g. ``users/{id}/role-mappings``).
    """

    if representation:
        rep_id = representation.get("id")
        if isinstance(rep_id, str) and rep_id:
            return rep_id

    if isinstance(resource_path, str):
        segments = [seg for seg in resource_path.split("/") if seg]
        for index, segment in enumerate(segments):
            if segment == "users" and index + 1 < len(segments):
                return segments[index + 1]
    return ""


def _extract_group_id(resource_path: Any) -> str:
    """Pull the group id from a ``users/{uid}/groups/{gid}`` admin resourcePath."""

    if isinstance(resource_path, str):
        segments = [seg for seg in resource_path.split("/") if seg]
        for index, segment in enumerate(segments):
            if segment == "groups" and index + 1 < len(segments):
                return segments[index + 1]
    return ""


def _extract_group_event_id(
    resource_path: Any, representation: dict[str, Any] | None
) -> str:
    """Derive the group id for a GROUP admin event.

    Prefers the representation's ``id`` (always the new/edited group). Falls back
    to a top-level ``groups/{id}`` resourcePath, but NOT ``groups/{parent}/children``
    (whose id is the parent, not the created subgroup) — there the id must come
    from the representation.
    """

    if representation:
        rep_id = representation.get("id")
        if isinstance(rep_id, str) and rep_id:
            return rep_id

    if isinstance(resource_path, str):
        segments = [seg for seg in resource_path.split("/") if seg]
        if len(segments) == 2 and segments[0] == "groups":
            return segments[1]
    return ""


def parse_admin_event(payload: dict[str, Any]) -> AdminUserEvent | None:
    """Normalize a Keycloak admin-event payload into an ``AdminUserEvent``.

    Returns ``None`` for anything that is not a top-level USER admin event
    (login events, group/role changes, unknown operations) so the caller can
    ack-and-ignore it.
    """

    if not isinstance(payload, dict):
        return None

    resource_type = payload.get("resourceType")
    if resource_type != USER_RESOURCE_TYPE:
        return None

    operation = payload.get("operationType")
    if not isinstance(operation, str):
        return None
    operation = operation.upper()
    if operation not in UPSERT_OPERATIONS and operation not in DELETE_OPERATIONS:
        return None

    event_id = payload.get("id") or payload.get("uid")
    if not isinstance(event_id, str) or not event_id:
        return None

    representation = _coerce_representation(payload.get("representation"))
    sub = _extract_sub(payload.get("resourcePath"), representation)
    if not sub:
        return None

    return AdminUserEvent(
        event_id=event_id,
        event_type=str(payload.get("type") or f"admin.USER-{operation}"),
        operation=operation,
        sub=sub,
        representation=representation,
    )


def parse_group_membership_event(
    payload: dict[str, Any],
) -> AdminGroupMembershipEvent | None:
    """Normalize a Keycloak GROUP_MEMBERSHIP admin event.

    Returns ``None`` for anything that is not a CREATE/DELETE GROUP_MEMBERSHIP
    event with a resolvable user sub and group id, so the caller can ack-ignore.
    """

    if not isinstance(payload, dict):
        return None

    if payload.get("resourceType") != GROUP_MEMBERSHIP_RESOURCE_TYPE:
        return None

    operation = payload.get("operationType")
    if not isinstance(operation, str):
        return None
    operation = operation.upper()
    if operation not in GROUP_MEMBERSHIP_OPERATIONS:
        return None

    event_id = payload.get("id") or payload.get("uid")
    if not isinstance(event_id, str) or not event_id:
        return None

    resource_path = payload.get("resourcePath")
    sub = _extract_sub(resource_path, None)
    group_id = _extract_group_id(resource_path)
    if not sub or not group_id:
        return None

    representation = _coerce_representation(payload.get("representation")) or {}
    group_path = representation.get("path")
    group_name = representation.get("name")

    return AdminGroupMembershipEvent(
        event_id=event_id,
        event_type=str(payload.get("type") or f"admin.GROUP_MEMBERSHIP-{operation}"),
        operation=operation,
        sub=sub,
        group_id=group_id,
        group_path=group_path if isinstance(group_path, str) else "",
        group_name=group_name if isinstance(group_name, str) else "",
    )


def _derive_group_path(
    resource_path: Any,
    representation: dict[str, Any] | None,
    group_name: str,
    operation: str,
) -> str:
    """Resolve the group path for a GROUP event, deriving it when it is absent.

    Keycloak's ``GroupRepresentation`` carries no ``path`` on a CREATE event, so
    an id-anchored org would be stored with an empty path and later fork a
    second, path-anchored row at login. For a TOP-LEVEL group the path is simply
    ``/{name}`` — and a top-level CREATE is unambiguous: its resourcePath is
    ``groups/{id}`` (a subgroup CREATE is ``groups/{parent}/children``). Derive
    only in that case; subgroups and non-CREATE events fall back to whatever the
    representation carries (empty is fine — a later membership/login event
    backfills the path).
    """

    rep_path = (representation or {}).get("path")
    if isinstance(rep_path, str) and rep_path.strip():
        return rep_path.strip()

    if operation == "CREATE" and group_name and isinstance(resource_path, str):
        segments = [seg for seg in resource_path.split("/") if seg]
        if len(segments) == 2 and segments[0] == "groups":
            return "/" + group_name
    return ""


def parse_group_event(payload: dict[str, Any]) -> AdminGroupEvent | None:
    """Normalize a Keycloak GROUP admin event (create/update of the group itself).

    Returns ``None`` for anything that is not a CREATE/UPDATE GROUP event with a
    resolvable group id, so the caller can ack-ignore it.
    """

    if not isinstance(payload, dict):
        return None

    if payload.get("resourceType") != GROUP_RESOURCE_TYPE:
        return None

    operation = payload.get("operationType")
    if not isinstance(operation, str):
        return None
    operation = operation.upper()
    if operation not in GROUP_OPERATIONS:
        return None

    event_id = payload.get("id") or payload.get("uid")
    if not isinstance(event_id, str) or not event_id:
        return None

    representation = _coerce_representation(payload.get("representation"))
    group_id = _extract_group_event_id(payload.get("resourcePath"), representation)
    if not group_id:
        return None

    group_name = (representation or {}).get("name")
    group_name = group_name if isinstance(group_name, str) else ""
    group_path = _derive_group_path(
        payload.get("resourcePath"), representation, group_name, operation
    )

    return AdminGroupEvent(
        event_id=event_id,
        event_type=str(payload.get("type") or f"admin.GROUP-{operation}"),
        operation=operation,
        group_id=group_id,
        group_path=group_path,
        group_name=group_name,
    )


def _accepted(event_id: str, status: str) -> JsonResponse:
    return JsonResponse({"status": status, "event_id": event_id}, status=202)


@csrf_exempt  # type: ignore[untyped-decorator]
def keycloak_events(request: HttpRequest) -> JsonResponse:
    """Receive Keycloak admin events and offload projection writes to Celery.

    Fail-closed and fast: verify the HMAC signature, dedupe by event id, enqueue
    a Celery task, and return 202 immediately. The actual projection write
    happens in the worker so Keycloak's event dispatch is never blocked.
    """

    if request.method != "POST":
        return build_error_json_response(
            code=ErrorCode.METHOD_NOT_ALLOWED,
            message="Only POST is allowed.",
            details=[],
            status_code=405,
            request=request,
        )

    config = _webhook_config()
    secret = config.get("SECRET")
    if not secret:
        # No secret configured -> replication endpoint is disabled. Fail closed
        # rather than accept unauthenticated writes.
        return build_error_json_response(
            code=ErrorCode.INTERNAL_ERROR,
            message="Keycloak replication webhook is not configured.",
            details=[],
            status_code=503,
            request=request,
        )

    header_name = config.get("SIGNATURE_HEADER", "X-Keycloak-Signature")
    signature = request.headers.get(header_name, "")
    if not verify_signature(request.body, signature, secret):
        logger.warning("keycloak.webhook.bad_signature")
        return build_error_json_response(
            code=ErrorCode.AUTHENTICATION_FAILED,
            message="Invalid webhook signature.",
            details=[],
            status_code=401,
            request=request,
        )

    try:
        payload = json.loads(request.body)
    except ValueError:
        return build_error_json_response(
            code=ErrorCode.PARSE_ERROR,
            message="Request body is not valid JSON.",
            details=[],
            status_code=400,
            request=request,
        )

    event_id, defaults = _event_ledger_entry(payload)
    if event_id is None:
        # Well-formed but not an event we replicate. Ack so Keycloak does not
        # retry it forever.
        return JsonResponse({"status": "ignored"}, status=202)

    # Dedupe synchronously (a single fast insert) so re-delivered events never
    # enqueue duplicate work. The unique constraint on event_id is the source
    # of truth even under concurrent delivery.
    try:
        with transaction.atomic():
            _, created = KeycloakReplicationEvent.objects.get_or_create(
                event_id=event_id,
                defaults=defaults,
            )
    except IntegrityError:
        created = False

    if not created:
        return _accepted(event_id, "duplicate")

    process_keycloak_admin_event.delay(event_id=event_id)
    return _accepted(event_id, "accepted")


def _event_ledger_entry(
    payload: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Build the dedupe-ledger row for a replicated event, or ``(None, {})``.

    Tries the USER path first; when org replication is enabled and the payload
    is not a USER event, tries the GROUP_MEMBERSHIP path. The stored payload
    carries a ``kind`` discriminator the Celery task branches on.
    """

    user_event = parse_admin_event(payload)
    if user_event is not None:
        return user_event.event_id, {
            "event_type": user_event.event_type,
            "operation": user_event.operation,
            "keycloak_sub": user_event.sub,
            "payload": {
                "kind": "user",
                "event_id": user_event.event_id,
                "type": user_event.event_type,
                "operation": user_event.operation,
                "sub": user_event.sub,
                "representation": user_event.representation,
            },
        }

    if _org_replication_enabled():
        membership_event = parse_group_membership_event(payload)
        if membership_event is not None:
            return membership_event.event_id, {
                "event_type": membership_event.event_type,
                "operation": membership_event.operation,
                "keycloak_sub": membership_event.sub,
                "payload": {
                    "kind": "group_membership",
                    "event_id": membership_event.event_id,
                    "type": membership_event.event_type,
                    "operation": membership_event.operation,
                    "sub": membership_event.sub,
                    "group_id": membership_event.group_id,
                    "group_path": membership_event.group_path,
                    "group_name": membership_event.group_name,
                },
            }

        group_event = parse_group_event(payload)
        if group_event is not None:
            return group_event.event_id, {
                "event_type": group_event.event_type,
                "operation": group_event.operation,
                "keycloak_sub": "",
                "payload": {
                    "kind": "group",
                    "event_id": group_event.event_id,
                    "type": group_event.event_type,
                    "operation": group_event.operation,
                    "group_id": group_event.group_id,
                    "group_path": group_event.group_path,
                    "group_name": group_event.group_name,
                },
            }

    return None, {}
