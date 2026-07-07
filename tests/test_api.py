from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.identity.authorization import assign_membership_role, seed_system_roles
from apps.identity.models import Membership, Organization, Role


class IdentityAPITests(TestCase):
    def setUp(self) -> None:
        seed_system_roles()
        User = get_user_model()
        self.user = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            keycloak_sub="kc-admin",
            display_name="Admin Tenant",
            email_verified=True,
        )
        self.invited_user = User.objects.create_user(
            username="external",
            email="external@example.com",
            keycloak_sub="kc-external",
            display_name="External Auditor",
        )
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        self.membership = Membership.objects.create(
            organization=self.organization,
            user=self.user,
            status=Membership.Status.ACTIVE,
        )
        assign_membership_role(self.membership, Role.objects.get(code="P6"))
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_me_bootstraps_profile_memberships_roles_and_orgs(self) -> None:
        response = self.client.get("/api/v1/me")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["profile"]["email"], "admin@example.com")
        self.assertEqual(payload["memberships"][0]["id"], str(self.membership.id))
        self.assertEqual(payload["roles"][0]["code"], "P6")
        self.assertEqual(payload["orgs"][0]["slug"], "acme")

    def test_identity_collections_are_scoped_to_active_organization(self) -> None:
        response = self.client.get(
            "/api/v1/users", HTTP_X_ORGANIZATION_ID=str(self.organization.id)
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["email"], "admin@example.com")

    def test_invite_membership_and_assign_then_remove_role(self) -> None:
        create_response = self.client.post(
            "/api/v1/memberships",
            {
                "user_id": str(self.invited_user.id),
                "status": Membership.Status.INVITED,
                "alcance": {"audit_process_ids": ["audit-1"]},
                "expira_en": "2030-01-01T00:00:00Z",
            },
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.organization.id),
            HTTP_IDEMPOTENCY_KEY="11111111-1111-4111-8111-111111111111",
        )

        self.assertEqual(create_response.status_code, 201)
        membership_id = create_response.json()["id"]
        self.assertTrue(create_response.json()["es_invitada"])
        role = Role.objects.get(code="P7")

        Membership.objects.filter(id=membership_id).update(
            status=Membership.Status.ACTIVE
        )
        add_response = self.client.post(
            f"/api/v1/memberships/{membership_id}/roles",
            {"role_id": str(role.id)},
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.organization.id),
            HTTP_IDEMPOTENCY_KEY="22222222-2222-4222-8222-222222222222",
        )
        self.assertEqual(add_response.status_code, 201)
        self.assertEqual(add_response.json()["role"]["code"], "P7")

        delete_response = self.client.delete(
            f"/api/v1/memberships/{membership_id}/roles/{role.id}",
            HTTP_X_ORGANIZATION_ID=str(self.organization.id),
        )
        self.assertEqual(delete_response.status_code, 204)

    def _active_member_client(self, *, role_code: str) -> tuple[APIClient, Membership]:
        User = get_user_model()
        member = User.objects.create_user(
            username=f"member-{role_code}",
            email=f"{role_code.lower()}@example.com",
            keycloak_sub=f"kc-{role_code.lower()}",
        )
        membership = Membership.objects.create(
            organization=self.organization,
            user=member,
            status=Membership.Status.ACTIVE,
        )
        assign_membership_role(membership, Role.objects.get(code=role_code))
        client = APIClient()
        client.force_authenticate(user=member)
        return client, membership

    def test_member_without_manage_capability_cannot_invite(self) -> None:
        # P7 (Auditor Externo) is an active member but only holds `read`.
        client, _ = self._active_member_client(role_code="P7")

        response = client.post(
            "/api/v1/memberships",
            {"user_id": str(self.invited_user.id)},
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.organization.id),
            HTTP_IDEMPOTENCY_KEY="33333333-3333-4333-8333-333333333333",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")
        self.assertFalse(
            Membership.objects.filter(
                organization=self.organization, user=self.invited_user
            ).exists()
        )

    def test_member_without_manage_capability_cannot_assign_role(self) -> None:
        client, membership = self._active_member_client(role_code="P7")

        response = client.post(
            f"/api/v1/memberships/{membership.id}/roles",
            {"role_id": str(Role.objects.get(code="P1").id)},
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.organization.id),
            HTTP_IDEMPOTENCY_KEY="44444444-4444-4444-8444-444444444444",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")

    def test_remove_unknown_role_returns_404_not_500(self) -> None:
        response = self.client.delete(
            f"/api/v1/memberships/{self.membership.id}/roles/"
            "00000000-0000-4000-8000-000000000000",
            HTTP_X_ORGANIZATION_ID=str(self.organization.id),
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_duplicate_membership_invite_returns_400_not_500(self) -> None:
        payload = {"user_id": str(self.invited_user.id)}
        first = self.client.post(
            "/api/v1/memberships",
            payload,
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.organization.id),
            HTTP_IDEMPOTENCY_KEY="55555555-5555-4555-8555-555555555555",
        )
        self.assertEqual(first.status_code, 201)

        second = self.client.post(
            "/api/v1/memberships",
            payload,
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.organization.id),
            HTTP_IDEMPOTENCY_KEY="66666666-6666-4666-8666-666666666666",
        )

        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["error"]["code"], "validation_failed")
        self.assertEqual(
            Membership.objects.filter(
                organization=self.organization, user=self.invited_user
            ).count(),
            1,
        )


class BearerAuthWriteTests(TestCase):
    """Exercise the real bearer path (no force_authenticate) so the DRF
    authentication + CSRF behaviour is actually covered. force_authenticate
    bypasses authenticators and CSRF, which previously hid the SessionAuthentication
    CSRF enforcement on writes."""

    def setUp(self) -> None:
        seed_system_roles()
        User = get_user_model()
        self.user = User.objects.create_user(
            username="admin",
            email="admin@example.com",
            keycloak_sub="kc-admin",
        )
        self.invited_user = User.objects.create_user(
            username="external",
            email="external@example.com",
            keycloak_sub="kc-external",
        )
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        self.membership = Membership.objects.create(
            organization=self.organization,
            user=self.user,
            status=Membership.Status.ACTIVE,
        )
        assign_membership_role(self.membership, Role.objects.get(code="P6"))
        # No force_authenticate: requests go through the bearer middleware.
        # enforce_csrf_checks=True so DRF's CSRF enforcement is actually exercised
        # (the default test client bypasses it via _dont_enforce_csrf_checks).
        self.client = APIClient(enforce_csrf_checks=True)

    def test_bearer_post_write_does_not_require_csrf(self) -> None:
        claims = {"sub": "kc-admin"}
        with patch(
            "apps.identity.authentication.authenticate_bearer_token",
            return_value=(self.user, claims),
        ):
            response = self.client.post(
                "/api/v1/memberships",
                {"user_id": str(self.invited_user.id)},
                format="json",
                HTTP_AUTHORIZATION="Bearer dummy-token",
                HTTP_X_ORGANIZATION_ID=str(self.organization.id),
                HTTP_IDEMPOTENCY_KEY="77777777-7777-4777-8777-777777777777",
            )

        self.assertEqual(response.status_code, 201, response.content)

    def test_write_without_bearer_is_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/memberships",
            {"user_id": str(self.invited_user.id)},
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.organization.id),
            HTTP_IDEMPOTENCY_KEY="88888888-8888-4888-8888-888888888888",
        )

        self.assertIn(response.status_code, (401, 403))
