from __future__ import annotations

import hmac
import json
from hashlib import sha256
from typing import Any
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from apps.identity.models import KeycloakReplicationEvent
from apps.identity.webhooks import parse_group_event

SECRET = "test-secret"
WEBHOOK_URL = "/api/v1/identity/keycloak/events"


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def admin_event(
    *,
    event_id: str = "evt-1",
    operation: str = "CREATE",
    sub: str = "kc-sub-1",
    representation: dict[str, Any] | None = None,
    resource_type: str = "USER",
) -> dict[str, Any]:
    if representation is None and operation != "DELETE":
        representation = {
            "id": sub,
            "email": "user@example.com",
            "firstName": "Test",
            "lastName": "User",
            "enabled": True,
            "emailVerified": True,
        }
    payload: dict[str, Any] = {
        "id": event_id,
        "type": f"admin.USER-{operation}",
        "operationType": operation,
        "resourceType": resource_type,
        "resourcePath": f"users/{sub}",
    }
    if representation is not None:
        # Keycloak serializes the representation as a JSON string.
        payload["representation"] = json.dumps(representation)
    return payload


@override_settings(
    KEYCLOAK_WEBHOOK={"SECRET": SECRET, "SIGNATURE_HEADER": "X-Keycloak-Signature"},
    # Pin replication OFF so this class isolates the core USER webhook path
    # regardless of the ambient .env setting; group handling is covered by
    # KeycloakGroupWebhookTests.
    KEYCLOAK_ORG_REPLICATION={"ENABLED": False},
)
class KeycloakWebhookTests(TestCase):
    def setUp(self) -> None:
        self.client = Client()

    def _post(self, payload: dict[str, Any], *, signature: str | None = None) -> Any:
        body = json.dumps(payload).encode("utf-8")
        sig = signature if signature is not None else sign(body)
        return self.client.post(
            WEBHOOK_URL,
            data=body,
            content_type="application/json",
            HTTP_X_KEYCLOAK_SIGNATURE=sig,
        )

    def test_valid_signed_create_is_accepted_and_enqueued(self) -> None:
        with patch(
            "apps.identity.webhooks.process_keycloak_admin_event.delay"
        ) as delay:
            response = self._post(admin_event())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")
        record = KeycloakReplicationEvent.objects.get(event_id="evt-1")
        self.assertEqual(record.operation, "CREATE")
        self.assertEqual(record.keycloak_sub, "kc-sub-1")
        self.assertIsNone(record.processed_at)
        delay.assert_called_once_with(event_id="evt-1")

    def test_invalid_signature_is_rejected(self) -> None:
        with patch(
            "apps.identity.webhooks.process_keycloak_admin_event.delay"
        ) as delay:
            response = self._post(admin_event(), signature="deadbeef")

        self.assertEqual(response.status_code, 401)
        self.assertFalse(KeycloakReplicationEvent.objects.exists())
        delay.assert_not_called()

    def test_duplicate_event_is_deduped_and_enqueued_once(self) -> None:
        with patch(
            "apps.identity.webhooks.process_keycloak_admin_event.delay"
        ) as delay:
            first = self._post(admin_event())
            second = self._post(admin_event())

        self.assertEqual(first.json()["status"], "accepted")
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json()["status"], "duplicate")
        self.assertEqual(KeycloakReplicationEvent.objects.count(), 1)
        delay.assert_called_once_with(event_id="evt-1")

    def test_non_user_event_is_ignored(self) -> None:
        with patch(
            "apps.identity.webhooks.process_keycloak_admin_event.delay"
        ) as delay:
            response = self._post(admin_event(resource_type="GROUP"))

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "ignored")
        self.assertFalse(KeycloakReplicationEvent.objects.exists())
        delay.assert_not_called()

    def test_delete_event_is_accepted(self) -> None:
        with patch(
            "apps.identity.webhooks.process_keycloak_admin_event.delay"
        ) as delay:
            response = self._post(admin_event(operation="DELETE"))

        self.assertEqual(response.status_code, 202)
        record = KeycloakReplicationEvent.objects.get(event_id="evt-1")
        self.assertEqual(record.operation, "DELETE")
        self.assertEqual(record.keycloak_sub, "kc-sub-1")
        delay.assert_called_once_with(event_id="evt-1")

    def test_malformed_json_is_rejected(self) -> None:
        response = self.client.post(
            WEBHOOK_URL,
            data=b"not-json",
            content_type="application/json",
            HTTP_X_KEYCLOAK_SIGNATURE=sign(b"not-json"),
        )

        self.assertEqual(response.status_code, 400)

    def test_get_is_method_not_allowed(self) -> None:
        response = self.client.get(WEBHOOK_URL)

        self.assertEqual(response.status_code, 405)

    def test_missing_secret_fails_closed(self) -> None:
        with override_settings(KEYCLOAK_WEBHOOK={"SECRET": None}):
            response = self._post(admin_event(), signature="anything")

        self.assertEqual(response.status_code, 503)


def group_membership_event(
    *,
    event_id: str = "gevt-1",
    operation: str = "CREATE",
    sub: str = "kc-sub-1",
    group_id: str = "gid-1",
    representation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if representation is None:
        representation = {"id": group_id, "name": "Acme", "path": "/acme"}
    return {
        "id": event_id,
        "type": f"admin.GROUP_MEMBERSHIP-{operation}",
        "operationType": operation,
        "resourceType": "GROUP_MEMBERSHIP",
        "resourcePath": f"users/{sub}/groups/{group_id}",
        "representation": json.dumps(representation),
    }


@override_settings(
    KEYCLOAK_WEBHOOK={"SECRET": SECRET, "SIGNATURE_HEADER": "X-Keycloak-Signature"},
    KEYCLOAK_ORG_REPLICATION={
        "ENABLED": True,
        "GROUPS_CLAIM": "groups",
        "ROLE_MAP": {},
    },
)
class KeycloakGroupWebhookTests(TestCase):
    def setUp(self) -> None:
        self.client = Client()

    def _post(self, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload).encode("utf-8")
        return self.client.post(
            WEBHOOK_URL,
            data=body,
            content_type="application/json",
            HTTP_X_KEYCLOAK_SIGNATURE=sign(body),
        )

    def test_group_create_is_accepted_and_enqueued(self) -> None:
        with patch(
            "apps.identity.webhooks.process_keycloak_admin_event.delay"
        ) as delay:
            response = self._post(group_membership_event())

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")
        record = KeycloakReplicationEvent.objects.get(event_id="gevt-1")
        self.assertEqual(record.payload["kind"], "group_membership")
        self.assertEqual(record.payload["group_id"], "gid-1")
        self.assertEqual(record.payload["sub"], "kc-sub-1")
        self.assertEqual(record.payload["group_path"], "/acme")
        delay.assert_called_once_with(event_id="gevt-1")

    def test_group_delete_is_accepted(self) -> None:
        with patch(
            "apps.identity.webhooks.process_keycloak_admin_event.delay"
        ) as delay:
            response = self._post(group_membership_event(operation="DELETE"))

        self.assertEqual(response.status_code, 202)
        record = KeycloakReplicationEvent.objects.get(event_id="gevt-1")
        self.assertEqual(record.operation, "DELETE")
        delay.assert_called_once_with(event_id="gevt-1")

    def test_group_event_deduped_on_event_id(self) -> None:
        with patch("apps.identity.webhooks.process_keycloak_admin_event.delay"):
            self._post(group_membership_event())
            second = self._post(group_membership_event())

        self.assertEqual(second.json()["status"], "duplicate")
        self.assertEqual(KeycloakReplicationEvent.objects.count(), 1)

    def test_role_mapping_event_is_ignored(self) -> None:
        payload = group_membership_event()
        payload["resourceType"] = "REALM_ROLE_MAPPING"

        with patch(
            "apps.identity.webhooks.process_keycloak_admin_event.delay"
        ) as delay:
            response = self._post(payload)

        self.assertEqual(response.json()["status"], "ignored")
        self.assertFalse(KeycloakReplicationEvent.objects.exists())
        delay.assert_not_called()

    def test_group_event_ignored_when_replication_disabled(self) -> None:
        with override_settings(KEYCLOAK_ORG_REPLICATION={"ENABLED": False}):
            with patch(
                "apps.identity.webhooks.process_keycloak_admin_event.delay"
            ) as delay:
                response = self._post(group_membership_event())

        self.assertEqual(response.json()["status"], "ignored")
        self.assertFalse(KeycloakReplicationEvent.objects.exists())
        delay.assert_not_called()

    def test_group_object_create_is_accepted_and_enqueued(self) -> None:
        payload = {
            "id": "gobj-1",
            "type": "admin.GROUP-CREATE",
            "operationType": "CREATE",
            "resourceType": "GROUP",
            "resourcePath": "groups/gid-1",
            "representation": json.dumps(
                {"id": "gid-1", "name": "Acme", "path": "/acme"}
            ),
        }

        with patch(
            "apps.identity.webhooks.process_keycloak_admin_event.delay"
        ) as delay:
            response = self._post(payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "accepted")
        record = KeycloakReplicationEvent.objects.get(event_id="gobj-1")
        self.assertEqual(record.payload["kind"], "group")
        self.assertEqual(record.payload["group_id"], "gid-1")
        self.assertEqual(record.keycloak_sub, "")
        delay.assert_called_once_with(event_id="gobj-1")

    def test_group_delete_is_ignored(self) -> None:
        payload = {
            "id": "gobj-2",
            "type": "admin.GROUP-DELETE",
            "operationType": "DELETE",
            "resourceType": "GROUP",
            "resourcePath": "groups/gid-1",
        }

        with patch(
            "apps.identity.webhooks.process_keycloak_admin_event.delay"
        ) as delay:
            response = self._post(payload)

        self.assertEqual(response.json()["status"], "ignored")
        self.assertFalse(KeycloakReplicationEvent.objects.exists())
        delay.assert_not_called()


class ParseGroupEventPathDerivationTests(TestCase):
    def _event(self, representation: dict[str, Any], resource_path: str) -> dict:
        return {
            "id": "gobj-1",
            "operationType": "CREATE",
            "resourceType": "GROUP",
            "resourcePath": resource_path,
            "representation": json.dumps(representation),
        }

    def test_representation_path_is_used_verbatim(self) -> None:
        event = parse_group_event(
            self._event(
                {"id": "gid-1", "name": "Acme", "path": "/acme"}, "groups/gid-1"
            )
        )
        assert event is not None
        self.assertEqual(event.group_path, "/acme")

    def test_top_level_create_derives_path_from_name(self) -> None:
        # Keycloak omits `path` on a GROUP CREATE representation; a top-level
        # group's path is unambiguously `/{name}`.
        event = parse_group_event(
            self._event({"id": "gid-1", "name": "Cognitactix"}, "groups/gid-1")
        )
        assert event is not None
        self.assertEqual(event.group_path, "/Cognitactix")

    def test_subgroup_create_does_not_guess_path(self) -> None:
        # A subgroup CREATE targets `groups/{parent}/children`; without the
        # parent path we must not invent `/child`.
        event = parse_group_event(
            self._event({"id": "gid-2", "name": "child"}, "groups/gid-1/children")
        )
        assert event is not None
        self.assertEqual(event.group_path, "")

    def test_update_without_representation_path_does_not_guess(self) -> None:
        payload = self._event({"id": "gid-1", "name": "Acme"}, "groups/gid-1")
        payload["operationType"] = "UPDATE"
        event = parse_group_event(payload)
        assert event is not None
        self.assertEqual(event.group_path, "")
