from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.identity.models import (
    KeycloakReplicationEvent,
    Membership,
    Organization,
)
from apps.identity.tasks import process_keycloak_admin_event


def make_record(
    *,
    event_id: str = "evt-1",
    operation: str = "CREATE",
    sub: str = "kc-sub-1",
    representation: dict[str, Any] | None = None,
) -> KeycloakReplicationEvent:
    if representation is None and operation != "DELETE":
        representation = {
            "id": sub,
            "email": "user@example.com",
            "firstName": "Test",
            "lastName": "User",
            "enabled": True,
            "emailVerified": True,
        }
    record: KeycloakReplicationEvent = KeycloakReplicationEvent.objects.create(
        event_id=event_id,
        event_type=f"admin.USER-{operation}",
        operation=operation,
        keycloak_sub=sub,
        payload={
            "event_id": event_id,
            "type": f"admin.USER-{operation}",
            "operation": operation,
            "sub": sub,
            "representation": representation,
        },
    )
    return record


def make_group_record(
    *,
    event_id: str = "gevt-1",
    operation: str = "CREATE",
    sub: str = "kc-sub-1",
    group_id: str = "gid-1",
    group_path: str = "/acme",
    group_name: str = "Acme",
) -> KeycloakReplicationEvent:
    return KeycloakReplicationEvent.objects.create(
        event_id=event_id,
        event_type=f"admin.GROUP_MEMBERSHIP-{operation}",
        operation=operation,
        keycloak_sub=sub,
        payload={
            "kind": "group_membership",
            "event_id": event_id,
            "operation": operation,
            "sub": sub,
            "group_id": group_id,
            "group_path": group_path,
            "group_name": group_name,
        },
    )


def run(event_id: str) -> dict[str, Any]:
    result: dict[str, Any] = process_keycloak_admin_event.apply(
        kwargs={"event_id": event_id}
    ).get()
    return result


class ProcessKeycloakAdminEventTests(TestCase):
    def test_create_event_upserts_projection_and_marks_processed(self) -> None:
        record = make_record()

        result = run("evt-1")

        User = get_user_model()
        user = User.objects.get(keycloak_sub="kc-sub-1")
        self.assertEqual(result["status"], "created")
        self.assertEqual(user.email, "user@example.com")
        self.assertEqual(user.display_name, "Test User")
        self.assertIs(user.email_verified, True)
        record.refresh_from_db()
        self.assertIsNotNone(record.processed_at)

    def test_rerun_is_idempotent(self) -> None:
        make_record()

        run("evt-1")
        second = run("evt-1")

        self.assertEqual(second["status"], "already_processed")
        User = get_user_model()
        self.assertEqual(User.objects.filter(keycloak_sub="kc-sub-1").count(), 1)

    def test_delete_event_deactivates_projection(self) -> None:
        User = get_user_model()
        User.objects.create_user(
            username="local", email="a@example.com", keycloak_sub="kc-sub-1"
        )
        make_record(operation="DELETE", representation=None)

        result = run("evt-1")

        user = User.objects.get(keycloak_sub="kc-sub-1")
        self.assertEqual(result["status"], "deactivated")
        self.assertIs(user.is_active, False)

    def test_disable_via_update_deactivates_projection(self) -> None:
        User = get_user_model()
        User.objects.create_user(
            username="local", email="a@example.com", keycloak_sub="kc-sub-1"
        )
        make_record(
            operation="UPDATE",
            representation={"id": "kc-sub-1", "enabled": False},
        )

        result = run("evt-1")

        user = User.objects.get(keycloak_sub="kc-sub-1")
        self.assertEqual(result["status"], "updated")
        self.assertIs(user.is_active, False)

    def test_missing_record_returns_missing(self) -> None:
        result = run("evt-unknown")

        self.assertEqual(result["status"], "missing")


class GroupMembershipEventTests(TestCase):
    def _user(self, sub: str = "kc-sub-1"):
        return get_user_model().objects.create_user(username=sub, keycloak_sub=sub)

    def test_create_adds_keycloak_membership_without_roles(self) -> None:
        user = self._user()
        make_group_record(operation="CREATE")

        result = run("gevt-1")

        self.assertEqual(result["status"], "group_joined")
        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.origin, Membership.Origin.KEYCLOAK)
        self.assertEqual(membership.status, Membership.Status.ACTIVE)
        self.assertEqual(membership.roles.count(), 0)

    def test_delete_soft_removes_membership_and_leaves_manual_alone(self) -> None:
        user = self._user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_id="gid-1"
        )
        Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.ACTIVE,
            origin=Membership.Origin.KEYCLOAK,
        )
        make_group_record(operation="DELETE")

        result = run("gevt-1")

        self.assertEqual(result["status"], "group_removed")
        membership = Membership.objects.get(user=user, organization=org)
        self.assertEqual(membership.status, Membership.Status.REVOKED)

    def test_group_event_for_unknown_user_is_skipped(self) -> None:
        make_group_record(operation="CREATE", sub="nobody")

        result = run("gevt-1")

        self.assertEqual(result["status"], "skipped_no_user")
        self.assertFalse(Membership.objects.exists())

    def test_group_event_rerun_is_idempotent(self) -> None:
        self._user()
        make_group_record(operation="CREATE")

        run("gevt-1")
        second = run("gevt-1")

        self.assertEqual(second["status"], "already_processed")
        self.assertEqual(Membership.objects.count(), 1)


def make_group_object_record(
    *,
    event_id: str = "gobj-1",
    operation: str = "CREATE",
    group_id: str = "gid-1",
    group_path: str = "/acme",
    group_name: str = "Acme",
) -> KeycloakReplicationEvent:
    return KeycloakReplicationEvent.objects.create(
        event_id=event_id,
        event_type=f"admin.GROUP-{operation}",
        operation=operation,
        keycloak_sub="",
        payload={
            "kind": "group",
            "event_id": event_id,
            "operation": operation,
            "group_id": group_id,
            "group_path": group_path,
            "group_name": group_name,
        },
    )


class GroupEventTests(TestCase):
    def test_create_precreates_organization_without_user(self) -> None:
        make_group_object_record(operation="CREATE")

        result = run("gobj-1")

        self.assertEqual(result["status"], "org_created")
        org = Organization.objects.get(keycloak_group_id="gid-1")
        self.assertEqual(org.name, "Acme")
        self.assertFalse(Membership.objects.exists())

    def test_update_syncs_existing_organization(self) -> None:
        Organization.objects.create(name="Acme", slug="acme", keycloak_group_id="gid-1")
        make_group_object_record(operation="UPDATE", group_name="Acme Renamed")

        result = run("gobj-1")

        self.assertEqual(result["status"], "org_synced")
        org = Organization.objects.get(keycloak_group_id="gid-1")
        self.assertEqual(org.name, "Acme Renamed")
