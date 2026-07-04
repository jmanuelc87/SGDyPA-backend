from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.identity.authorization import (
    AuthorizationError,
    Capability,
    assign_membership_role,
    membership_has_capability,
    require_capability,
    revoke_membership_role,
    seed_system_roles,
    user_has_capability,
)
from apps.identity.models import Membership, Organization, Role


class AuthorizationTests(TestCase):
    def setUp(self) -> None:
        seed_system_roles()
        User = get_user_model()
        self.user = User.objects.create_user(
            username="lider",
            email="lider@example.com",
            keycloak_sub="kc-sub-lider",
        )
        self.organization = Organization.objects.create(name="Acme", slug="acme")
        self.other_organization = Organization.objects.create(
            name="Other", slug="other"
        )
        self.membership = Membership.objects.create(
            organization=self.organization,
            user=self.user,
            status=Membership.Status.ACTIVE,
        )

    def test_system_roles_are_seeded_with_p1_to_p7_capabilities(self) -> None:
        self.assertEqual(
            list(Role.objects.values_list("code", flat=True)),
            ["P1", "P2", "P3", "P4", "P5", "P6", "P7"],
        )
        self.assertIn(
            Capability.COMMISSION_AUDIT, Role.objects.get(code="P5").capabilities
        )
        self.assertEqual(
            Role.objects.get(code="P6").capabilities,
            [Capability.MANAGE_MEMBERSHIPS, Capability.READ],
        )

    def test_membership_role_assignment_and_revocation_are_org_scoped(self) -> None:
        p1 = Role.objects.get(code="P1")
        assign_membership_role(self.membership, p1)

        self.assertTrue(
            user_has_capability(
                self.user, self.organization.id, Capability.MANAGE_FINDINGS
            )
        )
        self.assertFalse(
            user_has_capability(
                self.user, self.other_organization.id, Capability.MANAGE_FINDINGS
            )
        )

        revoke_membership_role(self.membership, p1)
        self.assertFalse(
            user_has_capability(
                self.user, self.organization.id, Capability.MANAGE_FINDINGS
            )
        )

    def test_require_capability_fails_closed_server_side(self) -> None:
        with self.assertRaises(AuthorizationError):
            require_capability(
                self.user, self.organization.id, Capability.APPROVE_DISPOSITION
            )

        assign_membership_role(self.membership, Role.objects.get(code="P5"))
        require_capability(
            self.user, self.organization.id, Capability.APPROVE_DISPOSITION
        )

    def test_inactive_membership_cannot_receive_or_use_roles(self) -> None:
        self.membership.status = Membership.Status.SUSPENDED
        self.membership.save(update_fields=["status"])

        with self.assertRaises(AuthorizationError):
            assign_membership_role(self.membership, Role.objects.get(code="P1"))

        self.membership.roles.add(Role.objects.get(code="P1"))
        self.assertFalse(
            membership_has_capability(self.membership, Capability.MANAGE_FINDINGS)
        )

    def test_p7_read_is_limited_to_invitation_scope(self) -> None:
        self.membership.scope = {"audit_process_ids": ["audit-1"]}
        self.membership.expires_at = timezone.now() + timedelta(days=1)
        self.membership.save(update_fields=["scope", "expires_at"])
        assign_membership_role(self.membership, Role.objects.get(code="P7"))

        self.assertTrue(
            membership_has_capability(
                self.membership,
                Capability.READ,
                object_scope={"audit_process_ids": "audit-1"},
            )
        )
        self.assertFalse(
            membership_has_capability(
                self.membership,
                Capability.READ,
                object_scope={"audit_process_ids": "audit-2"},
            )
        )
