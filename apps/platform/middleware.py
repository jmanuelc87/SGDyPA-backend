from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse

from apps.platform.tenancy import (
    organization_context,
    set_current_organization_for_transaction,
    user_has_organization_membership,
)

ORGANIZATION_HEADER = "X-Organization-Id"


class TenantContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not _is_authenticated(request):
            return self.get_response(request)

        header_value = request.headers.get(ORGANIZATION_HEADER)
        if not header_value:
            return _error_response(
                "organization_header_required",
                f"{ORGANIZATION_HEADER} is required.",
                status=400,
            )

        try:
            organization_id = UUID(header_value)
        except ValueError:
            return _error_response(
                "invalid_organization_id",
                f"{ORGANIZATION_HEADER} must be a UUID.",
                status=400,
            )

        if not user_has_organization_membership(request.user, organization_id):
            return _error_response(
                "organization_membership_required",
                "User is not a member of the requested organization.",
                status=403,
            )

        with organization_context(organization_id), transaction.atomic():
            set_current_organization_for_transaction(organization_id)
            return self.get_response(request)


def _is_authenticated(request: HttpRequest) -> bool:
    user = getattr(request, "user", None)
    return bool(user is not None and getattr(user, "is_authenticated", False))


def _error_response(code: str, message: str, *, status: int) -> JsonResponse:
    return JsonResponse(
        {
            "error": {
                "code": code,
                "message": message,
                "details": [],
            }
        },
        status=status,
    )
