from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import Mock, patch
from uuid import UUID

from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, SimpleTestCase

from apps.platform.middleware import TenantContextMiddleware
from apps.platform.tenancy import (
    get_current_organization_id,
    set_current_organization_for_transaction,
    user_has_organization_membership,
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

    def test_rejects_non_member_request(self) -> None:
        request = self.factory.get("/", HTTP_X_ORGANIZATION_ID=str(ORG_B))
        request.user = FakeUser([ORG_A])

        response = TenantContextMiddleware(Mock())(request)

        self.assertEqual(response.status_code, 403)

    def test_rejects_missing_header_for_authenticated_request(self) -> None:
        request = self.factory.get("/")
        request.user = FakeUser([ORG_A])

        response = TenantContextMiddleware(Mock())(request)

        self.assertEqual(response.status_code, 400)

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
