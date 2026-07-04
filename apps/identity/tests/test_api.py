from __future__ import annotations

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
