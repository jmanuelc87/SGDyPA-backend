from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse

from apps.platform.tenancy import (
    organization_context,
    set_current_organization_for_transaction,
    user_has_organization_membership,
)

ORGANIZATION_HEADER = "X-Organization-Id"

# Routes that run before the client has selected an active organization, or that
# are inherently tenant-agnostic. These must not require the X-Organization-Id
# header. `/me` and `/organizations` are the SPA bootstrap/org-selector endpoints
# the client hits to discover its memberships; `/admin` is cross-tenant.
EXEMPT_PATH_PREFIXES: tuple[str, ...] = (
    "/me",
    "/organizations",
    "/admin",
)


class RequestIDMiddleware:
    header_name = "HTTP_X_REQUEST_ID"
    response_header = "X-Request-Id"

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.META.get(self.header_name) or f"req_{uuid4().hex}"
        request.request_id = request_id
        response = self.get_response(request)
        response[self.response_header] = request_id
        return response


class TenantContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not _is_authenticated(request) or _is_exempt(request):
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

        with organization_context(organization_id), transaction.atomic():
            # Set the requested org's transaction-local GUC *before* checking
            # membership. The MEMBERSHIP table carries organization_id, so the
            # fail-closed tenant RLS policy applies to it too; a membership
            # lookup run with app.current_org unset would return zero rows and
            # 403 every valid tenant request. With the GUC scoped to the
            # requested org, RLS permits exactly that org's membership rows, and
            # the lookup still filters by the authenticated user.
            set_current_organization_for_transaction(organization_id)

            if not user_has_organization_membership(request.user, organization_id):
                return _error_response(
                    "organization_membership_required",
                    "User is not a member of the requested organization.",
                    status=403,
                )

            return self.get_response(request)


def _is_authenticated(request: HttpRequest) -> bool:
    user = getattr(request, "user", None)
    return bool(user is not None and getattr(user, "is_authenticated", False))


def _is_exempt(request: HttpRequest) -> bool:
    path = request.path
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in EXEMPT_PATH_PREFIXES
    )


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
