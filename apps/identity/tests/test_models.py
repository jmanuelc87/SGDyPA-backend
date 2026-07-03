from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.identity.models import Membership, Organization


class IdentityModelTests(TestCase):
    def setUp(self) -> None:
        User = get_user_model()
        self.user = User.objects.create_user(
            username="auditor",
            email="auditor@example.com",
            keycloak_sub="kc-sub-auditor",
        )
        self.organization = Organization.objects.create(
            name="Org Auditoria",
            slug="org-auditoria",
        )

    def test_active_membership_authorizes_by_surrogate_user_and_org(
        self,
    ) -> None:
        Membership.objects.create(
            organization=self.organization,
            user=self.user,
            status=Membership.Status.ACTIVE,
        )

        self.assertTrue(self.user.has_organization_membership(self.organization.id))

    def test_membership_keeps_invitation_scope_and_expiration(self) -> None:
        expires_at = timezone.now() + timedelta(days=7)

        membership = Membership.objects.create(
            organization=self.organization,
            user=self.user,
            status=Membership.Status.INVITED,
            scope={"audit_process_ids": ["proc_123"], "access": "read_only"},
            expires_at=expires_at,
        )

        self.assertEqual(membership.scope["access"], "read_only")
        self.assertEqual(membership.expires_at, expires_at)
        self.assertEqual(Membership.objects.invited().count(), 1)

    def test_expired_membership_does_not_authorize_tenant_access(self) -> None:
        Membership.objects.create(
            organization=self.organization,
            user=self.user,
            status=Membership.Status.ACTIVE,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        self.assertFalse(self.user.has_organization_membership(self.organization.id))
