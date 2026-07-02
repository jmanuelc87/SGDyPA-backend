from django.test import SimpleTestCase, override_settings
from django.urls import path
from rest_framework import serializers
from rest_framework.exceptions import APIException
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.platform.error_codes import ErrorCode
from apps.platform.exceptions import StableAPIException
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


urlpatterns = [
    path("api/v1/health-checks", HealthCheckView.as_view()),
    path("api/v1/validation-probes", ValidationProbeView.as_view()),
    path("api/v1/business-errors", BusinessErrorProbeView.as_view()),
    path("api/v1/sensitive-errors", SensitiveAPIExceptionView.as_view()),
    path("api/v1/unhandled-errors", UnhandledExceptionView.as_view()),
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
