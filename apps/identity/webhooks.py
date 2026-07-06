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

# Keycloak resource type we replicate. Group/role/client events are acked and
# ignored — identity projection only cares about USER changes.
USER_RESOURCE_TYPE = "USER"

# Admin operations that carry a user representation to project.
UPSERT_OPERATIONS = frozenset({"CREATE", "UPDATE"})
DELETE_OPERATIONS = frozenset({"DELETE"})


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

    event = parse_admin_event(payload)
    if event is None:
        # Well-formed but not a user event we replicate. Ack so Keycloak does
        # not retry it forever.
        return JsonResponse({"status": "ignored"}, status=202)

    # Dedupe synchronously (a single fast insert) so re-delivered events never
    # enqueue duplicate work. The unique constraint on event_id is the source
    # of truth even under concurrent delivery.
    try:
        with transaction.atomic():
            _, created = KeycloakReplicationEvent.objects.get_or_create(
                event_id=event.event_id,
                defaults={
                    "event_type": event.event_type,
                    "operation": event.operation,
                    "keycloak_sub": event.sub,
                    "payload": {
                        "event_id": event.event_id,
                        "type": event.event_type,
                        "operation": event.operation,
                        "sub": event.sub,
                        "representation": event.representation,
                    },
                },
            )
    except IntegrityError:
        created = False

    if not created:
        return _accepted(event.event_id, "duplicate")

    process_keycloak_admin_event.delay(event_id=event.event_id)
    return _accepted(event.event_id, "accepted")
