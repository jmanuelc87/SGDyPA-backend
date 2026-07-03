from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from django.db import transaction
from django.http import HttpRequest, JsonResponse

from apps.platform.api_errors import build_error_json_response
from apps.platform.error_codes import ErrorCode
from apps.platform.models import IdempotencyRecord

P = ParamSpec("P")
R = TypeVar("R", bound=JsonResponse)

IDEMPOTENCY_HEADER = "Idempotency-Key"


def require_idempotency_key(view_func: Callable[P, R]) -> Callable[P, JsonResponse]:
    """Require a UUID Idempotency-Key on side-effecting POST views and replay responses.

    The decorated view is executed only for the first request with a given key. Its
    JSON response body and status code are persisted, and later requests with the
    same key receive that stored response without running the view again.
    """

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: P.args, **kwargs: P.kwargs) -> JsonResponse:
        if request.method != "POST":
            return view_func(request, *args, **kwargs)

        raw_key = request.headers.get(IDEMPOTENCY_HEADER, "").strip()
        if not raw_key:
            return build_error_json_response(
                code=ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
                message="Idempotency-Key header is required for this operation.",
                details=[],
                status_code=400,
                request=request,
            )

        try:
            idempotency_key = uuid.UUID(raw_key)
        except ValueError:
            return build_error_json_response(
                code=ErrorCode.IDEMPOTENCY_KEY_INVALID,
                message="Idempotency-Key header must be a UUID.",
                details=[],
                status_code=400,
                request=request,
            )

        with transaction.atomic():
            record, created = (
                IdempotencyRecord.objects.select_for_update().get_or_create(
                    key=idempotency_key,
                    defaults={"method": request.method, "path": request.path},
                )
            )
            if not created:
                return JsonResponse(record.response_body, status=record.status_code)

            response = view_func(request, *args, **kwargs)
            record.status_code = response.status_code
            record.response_body = _json_response_body(response)
            record.save(update_fields=["status_code", "response_body", "updated_at"])
            return response

    return wrapper


def _json_response_body(response: JsonResponse) -> dict[str, Any]:
    payload = json.loads(response.content.decode(response.charset))
    if not isinstance(payload, dict):
        return {"data": payload}
    return payload
