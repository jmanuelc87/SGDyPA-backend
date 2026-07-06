from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.identity.models import KeycloakReplicationEvent
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
