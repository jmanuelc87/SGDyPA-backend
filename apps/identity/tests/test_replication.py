from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.identity.replication import (
    ProjectionAttributes,
    deactivate_user_projection,
    upsert_user_projection,
)


class ProjectionAttributesTests(TestCase):
    def test_from_representation_composes_display_name_from_names(self) -> None:
        attrs = ProjectionAttributes.from_representation(
            {"firstName": "Ada", "lastName": "Lovelace"}
        )

        self.assertEqual(attrs.display_name, "Ada Lovelace")

    def test_from_representation_maps_enabled_and_email_verified(self) -> None:
        attrs = ProjectionAttributes.from_representation(
            {"enabled": False, "emailVerified": True, "email": "ada@example.com"}
        )

        self.assertIs(attrs.enabled, False)
        self.assertIs(attrs.email_verified, True)
        self.assertEqual(attrs.email, "ada@example.com")

    def test_from_representation_leaves_missing_display_name_none(self) -> None:
        attrs = ProjectionAttributes.from_representation({"email": "x@example.com"})

        self.assertIsNone(attrs.display_name)
        self.assertIsNone(attrs.enabled)

    def test_from_claims_never_sets_enabled(self) -> None:
        attrs = ProjectionAttributes.from_claims(
            {"email": "x@example.com", "name": "X Y", "enabled": False}
        )

        # A login token never disables a user; `enabled` is ignored from claims.
        self.assertIsNone(attrs.enabled)


class UpsertUserProjectionTests(TestCase):
    def test_creates_user_keyed_on_sub(self) -> None:
        user, created, changed = upsert_user_projection(
            "kc-new",
            ProjectionAttributes(
                email="new@example.com",
                first_name="New",
                last_name="User",
                display_name="New User",
                email_verified=True,
            ),
            source="test",
        )

        self.assertTrue(created)
        self.assertEqual(user.keycloak_sub, "kc-new")
        self.assertEqual(user.username, "kc-new")
        self.assertEqual(user.email, "new@example.com")
        self.assertEqual(user.display_name, "New User")
        self.assertIs(user.email_verified, True)
        self.assertIn("email", changed)

    def test_updates_existing_user_by_sub(self) -> None:
        User = get_user_model()
        existing = User.objects.create_user(
            username="local",
            email="stale@example.com",
            keycloak_sub="kc-1",
        )

        user, created, changed = upsert_user_projection(
            "kc-1",
            ProjectionAttributes(email="fresh@example.com", display_name="Fresh"),
            source="test",
        )

        existing.refresh_from_db()
        self.assertFalse(created)
        self.assertEqual(user.pk, existing.pk)
        self.assertEqual(existing.email, "fresh@example.com")
        self.assertEqual(existing.display_name, "Fresh")
        self.assertCountEqual(changed, ["email", "display_name"])

    def test_never_matches_on_email_only_on_sub(self) -> None:
        User = get_user_model()
        User.objects.create_user(
            username="local",
            email="shared@example.com",
            keycloak_sub="kc-existing",
        )

        # Same email, different sub -> a brand new projection, never a join on
        # email (ADR-0002).
        user, created, _ = upsert_user_projection(
            "kc-different",
            ProjectionAttributes(email="shared@example.com"),
            source="test",
        )

        self.assertTrue(created)
        self.assertNotEqual(user.keycloak_sub, "kc-existing")
        self.assertEqual(User.objects.filter(email="shared@example.com").count(), 2)

    def test_enabled_false_deactivates_via_upsert(self) -> None:
        User = get_user_model()
        User.objects.create_user(
            username="local", email="a@example.com", keycloak_sub="kc-1"
        )

        user, _, changed = upsert_user_projection(
            "kc-1",
            ProjectionAttributes(enabled=False),
            source="test",
        )

        self.assertIs(user.is_active, False)
        self.assertIn("is_active", changed)

    def test_empty_attrs_change_nothing(self) -> None:
        User = get_user_model()
        User.objects.create_user(
            username="local", email="a@example.com", keycloak_sub="kc-1"
        )

        _, created, changed = upsert_user_projection(
            "kc-1", ProjectionAttributes(), source="test"
        )

        self.assertFalse(created)
        self.assertEqual(changed, [])

    def test_empty_sub_raises(self) -> None:
        with self.assertRaises(ValueError):
            upsert_user_projection("", ProjectionAttributes(), source="test")

    def test_upsert_logs_at_info_without_reserved_key_collision(self) -> None:
        # assertLogs forces the logger to INFO and builds real LogRecords, so a
        # reserved `extra` key (e.g. "created") would raise KeyError here — as it
        # does under `celery worker --loglevel=INFO`.
        with self.assertLogs("apps.identity.replication", level="INFO") as logs:
            upsert_user_projection(
                "kc-log",
                ProjectionAttributes(email="log@example.com"),
                source="test",
            )

        self.assertTrue(
            any("keycloak.projection.upserted" in line for line in logs.output)
        )


class DeactivateUserProjectionTests(TestCase):
    def test_deactivates_active_user(self) -> None:
        User = get_user_model()
        user = User.objects.create_user(
            username="local", email="a@example.com", keycloak_sub="kc-1"
        )

        changed = deactivate_user_projection("kc-1", source="test")

        user.refresh_from_db()
        self.assertTrue(changed)
        self.assertIs(user.is_active, False)

    def test_no_op_when_already_inactive(self) -> None:
        User = get_user_model()
        User.objects.create_user(
            username="local",
            email="a@example.com",
            keycloak_sub="kc-1",
            is_active=False,
        )

        self.assertFalse(deactivate_user_projection("kc-1", source="test"))

    def test_missing_user_returns_false(self) -> None:
        self.assertFalse(deactivate_user_projection("kc-unknown", source="test"))
