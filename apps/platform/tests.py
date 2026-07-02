from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import Mock, patch
from uuid import UUID

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import path
from rest_framework import serializers
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import APIException, NotAuthenticated, Throttled
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.platform.error_codes import ErrorCode
from apps.platform.exceptions import StableAPIException
from apps.platform.middleware import TenantContextMiddleware
from apps.platform.tenancy import (
    get_current_organization_id,
    set_current_organization_for_transaction,
    user_has_organization_membership,
)
from apps.platform.views import HealthCheckView


class ValidationProbeSerializer(serializers.Serializer):
    due_date = serializers.DateField(required=True)


class ValidationProbeView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def post(self, request: Request) -> Response:
        serializer = ValidationProbeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data)


class BusinessErrorProbeView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def get(self, request: Request) -> Response:
        raise StableAPIException(
            code=ErrorCode.SELF_APPROVAL_FORBIDDEN,
            message="No puedes aprobar tu propia solicitud.",
            status_code=409,
        )


class SensitiveAPIExceptionView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def get(self, request: Request) -> Response:
        raise APIException("SELECT * FROM secret_table on db.internal.local")


class UnhandledExceptionView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def get(self, request: Request) -> Response:
        raise RuntimeError(
            "Traceback with host api.internal.local and SELECT * FROM users"
        )


class DjangoHttp404View(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def get(self, request: Request) -> Response:
        raise Http404("Object matching query does not exist.")


class DjangoPermissionDeniedView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def get(self, request: Request) -> Response:
        raise DjangoPermissionDenied("You cannot see this resource.")


class ChallengeAuthentication(BaseAuthentication):
    def authenticate(self, request: Request) -> None:
        raise NotAuthenticated()

    def authenticate_header(self, request: Request) -> str:
        return 'Bearer realm="api"'


class ChallengeAuthView(APIView):
    authentication_classes = [ChallengeAuthentication]
    permission_classes: list[type] = []

    def get(self, request: Request) -> Response:  # pragma: no cover - never reached
        return Response({})


class ThrottledProbeView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    def get(self, request: Request) -> Response:
        raise Throttled(wait=42)


urlpatterns = [
    path("api/v1/health-checks", HealthCheckView.as_view()),
    path("api/v1/validation-probes", ValidationProbeView.as_view()),
    path("api/v1/business-errors", BusinessErrorProbeView.as_view()),
    path("api/v1/sensitive-errors", SensitiveAPIExceptionView.as_view()),
    path("api/v1/unhandled-errors", UnhandledExceptionView.as_view()),
    path("api/v1/django-404-probes", DjangoHttp404View.as_view()),
    path("api/v1/django-403-probes", DjangoPermissionDeniedView.as_view()),
    path("api/v1/challenge-auth-probes", ChallengeAuthView.as_view()),
    path("api/v1/throttled-probes", ThrottledProbeView.as_view()),
]

handler404 = "apps.platform.api_errors.api_not_found"
handler500 = "apps.platform.api_errors.api_server_error"


@override_settings(ROOT_URLCONF=__name__, DEBUG=False)
class APIErrorEnvelopeTests(SimpleTestCase):
    def setUp(self) -> None:
        self.client.raise_request_exception = False

    def test_health_check_uses_api_prefix_and_utc_iso_datetimes(self) -> None:
        response = self.client.get("/api/v1/health-checks")

        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            response.json()["checked_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        )

    def test_validation_errors_use_stable_envelope(self) -> None:
        response = self.client.post(
            "/api/v1/validation-probes",
            data={},
            content_type="application/json",
            HTTP_X_REQUEST_ID="req_test_validation",
        )

        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "validation_failed")
        self.assertEqual(payload["error"]["message"], "Request validation failed.")
        self.assertEqual(payload["error"]["request_id"], "req_test_validation")
        self.assertIn("due_date", payload["error"]["details"])
        self.assertEqual(response["X-Request-Id"], "req_test_validation")

    def test_business_errors_can_use_initial_stable_codes(self) -> None:
        response = self.client.get(
            "/api/v1/business-errors",
            HTTP_X_REQUEST_ID="req_test_business",
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "self_approval_forbidden")
        self.assertEqual(response.json()["error"]["request_id"], "req_test_business")

    def test_api_404_uses_error_envelope(self) -> None:
        response = self.client.get(
            "/api/v1/missing-resources",
            HTTP_X_REQUEST_ID="req_test_missing",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"],
            {
                "code": "not_found",
                "message": "Not found.",
                "details": [],
                "request_id": "req_test_missing",
            },
        )

    def test_django_http404_uses_not_found_envelope(self) -> None:
        response = self.client.get(
            "/api/v1/django-404-probes",
            HTTP_X_REQUEST_ID="req_test_django_404",
        )

        self.assertEqual(response.status_code, 404)
        payload = response.json()["error"]
        self.assertEqual(payload["code"], "not_found")
        self.assertEqual(payload["message"], "Not found.")
        self.assertEqual(payload["request_id"], "req_test_django_404")

    def test_django_permission_denied_uses_permission_denied_envelope(self) -> None:
        response = self.client.get(
            "/api/v1/django-403-probes",
            HTTP_X_REQUEST_ID="req_test_django_403",
        )

        self.assertEqual(response.status_code, 403)
        payload = response.json()["error"]
        self.assertEqual(payload["code"], "permission_denied")
        self.assertEqual(
            payload["message"],
            "You do not have permission to perform this action.",
        )
        self.assertEqual(payload["request_id"], "req_test_django_403")

    def test_auth_challenge_header_is_preserved_on_envelope(self) -> None:
        response = self.client.get("/api/v1/challenge-auth-probes")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], 'Bearer realm="api"')
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")
        self.assertEqual(response["Content-Type"], "application/json")

    def test_throttle_retry_after_header_is_preserved_on_envelope(self) -> None:
        response = self.client.get("/api/v1/throttled-probes")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "42")
        self.assertEqual(response.json()["error"]["code"], "throttled")
        self.assertEqual(response["Content-Type"], "application/json")

    def test_error_responses_do_not_leak_sensitive_internals(self) -> None:
        for url in ("/api/v1/sensitive-errors", "/api/v1/unhandled-errors"):
            with self.subTest(url=url):
                response = self.client.get(url)
                payload = response.content.decode()

                self.assertEqual(response.status_code, 500)
                self.assertIn('"error"', payload)
                self.assertNotIn("Traceback", payload)
                self.assertNotIn("SELECT", payload)
                self.assertNotIn("secret_table", payload)
                self.assertNotIn("api.internal.local", payload)
                self.assertNotIn("db.internal.local", payload)

    def test_initial_stable_error_code_catalog_is_defined(self) -> None:
        self.assertGreaterEqual(
            {code.value for code in ErrorCode},
            {
                "scope_frozen",
                "illegal_transition",
                "legal_hold_active",
                "self_approval_forbidden",
                "validation_failed",
                "stale_state",
            },
        )


ORG_A = UUID("11111111-1111-1111-1111-111111111111")
ORG_B = UUID("22222222-2222-2222-2222-222222222222")


@dataclass(frozen=True)
class FakeMembership:
    organization_id: UUID


class FakeUser:
    is_authenticated = True

    def __init__(self, organization_ids: list[UUID]) -> None:
        self.memberships = [FakeMembership(org_id) for org_id in organization_ids]


class TenantContextMiddlewareTests(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()

    @patch("apps.platform.middleware.set_current_organization_for_transaction")
    @patch("apps.platform.middleware.transaction.atomic")
    def test_sets_transaction_local_org_for_member_request(
        self,
        atomic_mock: Mock,
        set_current_org_mock: Mock,
    ) -> None:
        atomic_mock.return_value.__enter__.return_value = None
        atomic_mock.return_value.__exit__.return_value = None
        seen_context = []

        def get_response(request: HttpRequest) -> HttpResponse:
            seen_context.append(get_current_organization_id())
            return HttpResponse(status=204)

        request = self.factory.get("/", HTTP_X_ORGANIZATION_ID=str(ORG_A))
        request.user = FakeUser([ORG_A])

        response = TenantContextMiddleware(get_response)(request)

        self.assertEqual(response.status_code, 204)
        self.assertEqual(seen_context, [ORG_A])
        set_current_org_mock.assert_called_once_with(ORG_A)
        self.assertIsNone(get_current_organization_id())

    @patch("apps.platform.middleware.set_current_organization_for_transaction")
    @patch("apps.platform.middleware.transaction.atomic")
    def test_rejects_non_member_request(
        self,
        atomic_mock: Mock,
        set_current_org_mock: Mock,
    ) -> None:
        atomic_mock.return_value.__enter__.return_value = None
        atomic_mock.return_value.__exit__.return_value = None

        request = self.factory.get("/", HTTP_X_ORGANIZATION_ID=str(ORG_B))
        request.user = FakeUser([ORG_A])

        response = TenantContextMiddleware(Mock())(request)

        self.assertEqual(response.status_code, 403)
        # The tenant GUC must be set before the membership lookup so the
        # fail-closed RLS policy on the MEMBERSHIP table does not mask the row.
        set_current_org_mock.assert_called_once_with(ORG_B)

    def test_rejects_missing_header_for_authenticated_request(self) -> None:
        request = self.factory.get("/")
        request.user = FakeUser([ORG_A])

        response = TenantContextMiddleware(Mock())(request)

        self.assertEqual(response.status_code, 400)

    def test_bootstrap_routes_skip_header_requirement(self) -> None:
        for exempt_path in ("/me", "/organizations", "/organizations/", "/admin/"):
            with self.subTest(path=exempt_path):
                request = self.factory.get(exempt_path)
                request.user = FakeUser([ORG_A])

                def get_response(_: HttpRequest) -> HttpResponse:
                    return HttpResponse(status=200)

                response = TenantContextMiddleware(get_response)(request)

                self.assertEqual(response.status_code, 200)

    def test_membership_check_supports_user_membership_hook(self) -> None:
        user = Mock(is_authenticated=True)
        user.has_organization_membership.return_value = True

        self.assertTrue(user_has_organization_membership(user, ORG_A))
        user.has_organization_membership.assert_called_once_with(ORG_A)

    @patch("apps.platform.tenancy.connection")
    def test_set_current_organization_uses_transaction_local_guc(
        self,
        connection_mock: Mock,
    ) -> None:
        cursor = connection_mock.cursor.return_value.__enter__.return_value
        connection_mock.vendor = "postgresql"

        set_current_organization_for_transaction(ORG_A)

        cursor.execute.assert_called_once_with(
            "SELECT set_config('app.current_org', %s, true)",
            [str(ORG_A)],
        )
