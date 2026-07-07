from __future__ import annotations

from importlib import import_module

from django.apps import apps as global_apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.identity.models import Membership, MembershipRole, Organization, Role
from apps.identity.org_replication import (
    GroupRef,
    apply_group_membership_change,
    parse_groups_from_claims,
    parse_role_codes_from_claims,
    reconcile_from_claims,
    reconcile_user_memberships,
    resolve_or_create_organization,
    sync_organization_from_group,
)
from apps.trail.models import TrailEntry

# The migration module name starts with a digit, so it can't be a normal import.
merge_duplicate_group_orgs = import_module(
    "apps.identity.migrations.0006_organization_keycloak_group_path_unique"
).merge_duplicate_group_orgs

User = get_user_model()


def make_user(sub: str = "kc-1", **extra):
    return User.objects.create_user(username=sub, keycloak_sub=sub, **extra)


def role_codes(membership: Membership) -> set[str]:
    return set(membership.roles.values_list("code", flat=True))


class ParseGroupsFromClaimsTests(TestCase):
    def test_absent_claim_returns_none(self) -> None:
        self.assertIsNone(parse_groups_from_claims({"sub": "x"}))

    def test_non_list_claim_returns_none(self) -> None:
        self.assertIsNone(parse_groups_from_claims({"groups": "/acme"}))

    def test_present_empty_list_returns_empty(self) -> None:
        self.assertEqual(parse_groups_from_claims({"groups": []}), [])

    def test_paths_become_refs_with_leaf_name(self) -> None:
        refs = parse_groups_from_claims({"groups": ["/acme", "/parent/team", "  "]})

        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0], GroupRef(path="/acme", name="acme"))
        self.assertEqual(refs[1], GroupRef(path="/parent/team", name="team"))

    def test_custom_claim_name(self) -> None:
        refs = parse_groups_from_claims({"orgs": ["/a"]}, groups_claim="orgs")

        self.assertEqual(refs, [GroupRef(path="/a", name="a")])


class ParseRoleCodesFromClaimsTests(TestCase):
    ROLE_MAP = {"auditor-lider": "P1", "tenant-admin": "P6"}

    def test_maps_realm_and_client_roles_dropping_unmapped(self) -> None:
        claims = {
            "realm_access": {"roles": ["auditor-lider", "offline_access"]},
            "resource_access": {"sgdypa-api": {"roles": ["tenant-admin"]}},
        }

        self.assertEqual(
            parse_role_codes_from_claims(claims, self.ROLE_MAP), {"P1", "P6"}
        )

    def test_empty_role_map_returns_empty(self) -> None:
        claims = {"realm_access": {"roles": ["auditor-lider"]}}
        self.assertEqual(parse_role_codes_from_claims(claims, {}), set())

    def test_no_role_claims_returns_empty(self) -> None:
        self.assertEqual(parse_role_codes_from_claims({}, self.ROLE_MAP), set())


class ResolveOrCreateOrganizationTests(TestCase):
    def test_matches_existing_by_group_id(self) -> None:
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_id="gid-1"
        )

        resolved = resolve_or_create_organization(
            GroupRef(keycloak_group_id="gid-1", path="/acme"), source="test"
        )

        self.assertEqual(resolved.pk, org.pk)

    def test_matches_existing_by_path_and_backfills_id(self) -> None:
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_path="/acme"
        )

        resolved = resolve_or_create_organization(
            GroupRef(keycloak_group_id="gid-1", path="/acme"), source="test"
        )

        self.assertEqual(resolved.pk, org.pk)
        org.refresh_from_db()
        self.assertEqual(org.keycloak_group_id, "gid-1")

    def test_matches_existing_by_id_and_backfills_path(self) -> None:
        # A GROUP CREATE event created an id-anchored row with no path; a later
        # ref carrying the path must adopt that row, not fork a new one.
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_id="gid-1"
        )

        resolved = resolve_or_create_organization(
            GroupRef(keycloak_group_id="gid-1", path="/acme"), source="test"
        )

        self.assertEqual(resolved.pk, org.pk)
        org.refresh_from_db()
        self.assertEqual(org.keycloak_group_path, "/acme")

    def test_group_create_then_login_yields_single_org(self) -> None:
        # The reported bug: GROUP CREATE (id + derived path) followed by a login
        # groups claim (path only) must resolve to the SAME org.
        sync_organization_from_group(
            GroupRef(
                keycloak_group_id="gid-1", path="/Cognitactix", name="Cognitactix"
            ),
            source="admin-event",
        )
        resolve_or_create_organization(GroupRef(path="/Cognitactix"), source="login")

        orgs = Organization.objects.filter(name="Cognitactix")
        self.assertEqual(orgs.count(), 1)
        self.assertEqual(orgs.first().keycloak_group_id, "gid-1")
        self.assertEqual(orgs.first().keycloak_group_path, "/Cognitactix")

    def test_trailing_slash_path_matches_normalized_row(self) -> None:
        org = resolve_or_create_organization(GroupRef(path="/acme"), source="test")

        resolved = resolve_or_create_organization(
            GroupRef(path="/acme/"), source="test"
        )

        self.assertEqual(resolved.pk, org.pk)
        self.assertEqual(
            Organization.objects.filter(keycloak_group_path="/acme").count(), 1
        )

    def test_auto_creates_with_derived_name_and_slug(self) -> None:
        org = resolve_or_create_organization(
            GroupRef(keycloak_group_id="gid-1", path="/acme", name="Acme Corp"),
            source="test",
        )

        self.assertEqual(org.name, "Acme Corp")
        self.assertEqual(org.slug, "acme")
        self.assertEqual(org.keycloak_group_id, "gid-1")
        self.assertEqual(org.keycloak_group_path, "/acme")

    def test_same_leaf_different_parent_yields_distinct_slugs(self) -> None:
        a = resolve_or_create_organization(GroupRef(path="/a/team"), source="test")
        b = resolve_or_create_organization(GroupRef(path="/b/team"), source="test")

        self.assertNotEqual(a.slug, b.slug)
        self.assertEqual({a.slug, b.slug}, {"a-team", "b-team"})

    def test_slug_collision_gets_numeric_suffix(self) -> None:
        Organization.objects.create(name="Existing", slug="team")

        org = resolve_or_create_organization(GroupRef(path="/team"), source="test")

        self.assertEqual(org.slug, "team-2")


class SyncOrganizationFromGroupTests(TestCase):
    def test_precreates_organization_without_any_user(self) -> None:
        org = sync_organization_from_group(
            GroupRef(keycloak_group_id="gid-1", path="/acme", name="Acme Corp"),
            source="admin-event",
        )

        self.assertIsNotNone(org)
        self.assertEqual(org.keycloak_group_id, "gid-1")
        self.assertEqual(org.keycloak_group_path, "/acme")
        self.assertEqual(org.name, "Acme Corp")
        self.assertFalse(Membership.objects.exists())

    def test_refreshes_name_and_path_on_rename_keeping_slug(self) -> None:
        original = sync_organization_from_group(
            GroupRef(keycloak_group_id="gid-1", path="/acme", name="Acme"),
            source="admin-event",
        )

        renamed = sync_organization_from_group(
            GroupRef(keycloak_group_id="gid-1", path="/acme-corp", name="Acme Corp"),
            source="admin-event",
        )

        self.assertEqual(renamed.pk, original.pk)
        self.assertEqual(renamed.name, "Acme Corp")
        self.assertEqual(renamed.keycloak_group_path, "/acme-corp")
        # Slug is frozen for URL stability even though the group was renamed.
        self.assertEqual(renamed.slug, original.slug)

    def test_is_idempotent(self) -> None:
        ref = GroupRef(keycloak_group_id="gid-1", path="/acme", name="Acme")
        sync_organization_from_group(ref, source="admin-event")
        sync_organization_from_group(ref, source="admin-event")

        self.assertEqual(
            Organization.objects.filter(keycloak_group_id="gid-1").count(), 1
        )


class ReconcileUserMembershipsTests(TestCase):
    def test_auto_creates_org_active_membership_and_roles(self) -> None:
        user = make_user()

        reconcile_user_memberships(
            user,
            [GroupRef(keycloak_group_id="gid-1", path="/acme")],
            {"P2"},
            prune=True,
            source="test",
        )

        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.origin, Membership.Origin.KEYCLOAK)
        self.assertEqual(membership.status, Membership.Status.ACTIVE)
        self.assertEqual(membership.organization.keycloak_group_id, "gid-1")
        self.assertEqual(role_codes(membership), {"P2"})

    def test_global_roles_applied_to_every_membership(self) -> None:
        user = make_user()

        reconcile_user_memberships(
            user,
            [GroupRef(path="/a"), GroupRef(path="/b")],
            {"P2", "P6"},
            prune=True,
            source="test",
        )

        memberships = Membership.objects.filter(user=user)
        self.assertEqual(memberships.count(), 2)
        for membership in memberships:
            self.assertEqual(role_codes(membership), {"P2", "P6"})

    def test_prune_revokes_kc_membership_absent_from_desired(self) -> None:
        user = make_user()
        # First reconcile puts the user in /acme.
        reconcile_user_memberships(
            user, [GroupRef(path="/acme")], {"P2"}, prune=True, source="test"
        )
        acme = Organization.objects.get(keycloak_group_path="/acme")

        # Second reconcile drops /acme, adds /beta.
        reconcile_user_memberships(
            user, [GroupRef(path="/beta")], {"P2"}, prune=True, source="test"
        )

        acme_membership = Membership.objects.get(user=user, organization=acme)
        self.assertEqual(acme_membership.status, Membership.Status.REVOKED)
        self.assertEqual(role_codes(acme_membership), set())

    def test_prune_drops_role_no_longer_present(self) -> None:
        user = make_user()
        reconcile_user_memberships(
            user, [GroupRef(path="/acme")], {"P2", "P6"}, prune=True, source="test"
        )

        reconcile_user_memberships(
            user, [GroupRef(path="/acme")], {"P2"}, prune=True, source="test"
        )

        membership = Membership.objects.get(user=user)
        self.assertEqual(role_codes(membership), {"P2"})

    def test_manual_membership_referenced_by_group_is_untouched(self) -> None:
        user = make_user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_path="/acme"
        )
        manual = Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.INVITED,
            origin=Membership.Origin.MANUAL,
        )

        reconcile_user_memberships(
            user, [GroupRef(path="/acme")], {"P2"}, prune=True, source="test"
        )

        manual.refresh_from_db()
        self.assertEqual(manual.origin, Membership.Origin.MANUAL)
        self.assertEqual(manual.status, Membership.Status.INVITED)
        self.assertEqual(role_codes(manual), set())

    def test_manual_membership_absent_from_token_not_pruned(self) -> None:
        user = make_user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_path="/acme"
        )
        manual = Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.ACTIVE,
            origin=Membership.Origin.MANUAL,
        )

        # Reconcile references a different org entirely.
        reconcile_user_memberships(
            user, [GroupRef(path="/other")], set(), prune=True, source="test"
        )

        manual.refresh_from_db()
        self.assertEqual(manual.status, Membership.Status.ACTIVE)

    def test_reactivates_previously_revoked_kc_membership(self) -> None:
        user = make_user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_path="/acme"
        )
        Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.REVOKED,
            origin=Membership.Origin.KEYCLOAK,
        )

        reconcile_user_memberships(
            user, [GroupRef(path="/acme")], {"P2"}, prune=True, source="test"
        )

        membership = Membership.objects.get(user=user, organization=org)
        self.assertEqual(membership.status, Membership.Status.ACTIVE)
        self.assertEqual(role_codes(membership), {"P2"})

    def test_inactive_org_skips_roles_without_raising(self) -> None:
        user = make_user()
        Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_path="/acme", is_active=False
        )

        reconcile_user_memberships(
            user, [GroupRef(path="/acme")], {"P2"}, prune=True, source="test"
        )

        membership = Membership.objects.get(user=user)
        self.assertEqual(role_codes(membership), set())

    def test_idempotent_double_run(self) -> None:
        user = make_user()
        args = ([GroupRef(path="/acme")], {"P2"})

        reconcile_user_memberships(user, *args, prune=True, source="test")
        reconcile_user_memberships(user, *args, prune=True, source="test")

        self.assertEqual(Membership.objects.filter(user=user).count(), 1)
        membership = Membership.objects.get(user=user)
        self.assertEqual(
            MembershipRole.objects.filter(membership=membership).count(), 1
        )


class ApplyGroupMembershipChangeTests(TestCase):
    def test_add_creates_kc_membership_without_roles(self) -> None:
        user = make_user()

        apply_group_membership_change(
            user,
            GroupRef(keycloak_group_id="gid-1", path="/acme"),
            added=True,
            source="admin-event",
        )

        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.origin, Membership.Origin.KEYCLOAK)
        self.assertEqual(membership.status, Membership.Status.ACTIVE)
        self.assertEqual(role_codes(membership), set())

    def test_add_leaves_manual_membership_untouched(self) -> None:
        user = make_user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_id="gid-1"
        )
        manual = Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.INVITED,
            origin=Membership.Origin.MANUAL,
        )

        apply_group_membership_change(
            user, GroupRef(keycloak_group_id="gid-1"), added=True, source="admin-event"
        )

        manual.refresh_from_db()
        self.assertEqual(manual.origin, Membership.Origin.MANUAL)
        self.assertEqual(manual.status, Membership.Status.INVITED)

    def test_remove_soft_removes_kc_membership(self) -> None:
        user = make_user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_id="gid-1"
        )
        membership = Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.ACTIVE,
            origin=Membership.Origin.KEYCLOAK,
        )
        MembershipRole.objects.create(
            membership=membership, role=Role.objects.get(code="P2")
        )

        apply_group_membership_change(
            user, GroupRef(keycloak_group_id="gid-1"), added=False, source="admin-event"
        )

        membership.refresh_from_db()
        self.assertEqual(membership.status, Membership.Status.REVOKED)
        self.assertEqual(role_codes(membership), set())

    def test_remove_leaves_manual_membership_untouched(self) -> None:
        user = make_user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_id="gid-1"
        )
        manual = Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.ACTIVE,
            origin=Membership.Origin.MANUAL,
        )

        apply_group_membership_change(
            user, GroupRef(keycloak_group_id="gid-1"), added=False, source="admin-event"
        )

        manual.refresh_from_db()
        self.assertEqual(manual.status, Membership.Status.ACTIVE)

    def test_remove_unknown_org_is_noop(self) -> None:
        user = make_user()

        result = apply_group_membership_change(
            user, GroupRef(keycloak_group_id="gid-x"), added=False, source="admin-event"
        )

        self.assertIsNone(result)
        self.assertFalse(Membership.objects.filter(user=user).exists())


class ReconcileFromClaimsTests(TestCase):
    settings_kwargs = {
        "KEYCLOAK_ORG_REPLICATION": {
            "ENABLED": True,
            "GROUPS_CLAIM": "groups",
            "ROLE_MAP": {"auditor": "P2"},
        }
    }

    def test_creates_memberships_and_roles_from_claims(self) -> None:
        user = make_user()
        claims = {
            "sub": "kc-1",
            "groups": ["/acme"],
            "realm_access": {"roles": ["auditor"]},
        }

        with self.settings(**self.settings_kwargs):
            reconcile_from_claims(user, claims, source="login")

        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.organization.keycloak_group_path, "/acme")
        self.assertEqual(role_codes(membership), {"P2"})

    def test_absent_groups_claim_does_not_prune(self) -> None:
        user = make_user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_path="/acme"
        )
        Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.ACTIVE,
            origin=Membership.Origin.KEYCLOAK,
        )

        with self.settings(**self.settings_kwargs):
            reconcile_from_claims(user, {"sub": "kc-1"}, source="login")

        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.status, Membership.Status.ACTIVE)

    def test_empty_groups_claim_prunes_all_kc_memberships(self) -> None:
        user = make_user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_path="/acme"
        )
        Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.ACTIVE,
            origin=Membership.Origin.KEYCLOAK,
        )

        with self.settings(**self.settings_kwargs):
            reconcile_from_claims(user, {"sub": "kc-1", "groups": []}, source="login")

        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.status, Membership.Status.REVOKED)


class MergeDuplicateGroupOrgsTests(TestCase):
    """Data-migration reconciliation of pre-existing forked orgs."""

    def _run(self) -> None:
        merge_duplicate_group_orgs(global_apps, None)

    def test_merges_id_only_and_path_only_twins(self) -> None:
        user = make_user()
        canon = Organization.objects.create(
            name="Cognitactix", slug="cognitactix", keycloak_group_id="gid-1"
        )
        twin = Organization.objects.create(
            name="Cognitactix",
            slug="cognitactix-2",
            keycloak_group_path="/Cognitactix",
        )
        Membership.objects.create(
            organization=twin,
            user=user,
            status=Membership.Status.ACTIVE,
            origin=Membership.Origin.KEYCLOAK,
        )

        self._run()

        self.assertFalse(Organization.objects.filter(pk=twin.pk).exists())
        canon.refresh_from_db()
        self.assertEqual(canon.keycloak_group_path, "/Cognitactix")
        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.organization_id, canon.pk)

    def test_membership_clash_drops_duplicate_row(self) -> None:
        user = make_user()
        canon = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_id="gid-1"
        )
        twin = Organization.objects.create(
            name="Acme", slug="acme-2", keycloak_group_path="/acme"
        )
        # Both orgs already have a membership for the same user.
        Membership.objects.create(
            organization=canon, user=user, origin=Membership.Origin.KEYCLOAK
        )
        Membership.objects.create(
            organization=twin, user=user, origin=Membership.Origin.KEYCLOAK
        )

        self._run()

        self.assertFalse(Organization.objects.filter(pk=twin.pk).exists())
        self.assertEqual(Membership.objects.filter(user=user).count(), 1)
        self.assertEqual(Membership.objects.get(user=user).organization_id, canon.pk)

    def test_twin_with_audit_history_is_left_untouched(self) -> None:
        user = make_user()
        canon = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_id="gid-1"
        )
        twin = Organization.objects.create(
            name="Acme", slug="acme-2", keycloak_group_path="/acme"
        )
        TrailEntry.objects.create(
            organization=twin,
            actor=user,
            actor_email_snapshot="user@example.com",
            action="created",
            target_entity="thing",
            target_id=twin.pk,
            sequence=1,
            entry_hash="deadbeef",
        )

        self._run()

        # Trail-bearing twin survives; canonical keeps its empty path so the
        # unique path constraint still holds.
        self.assertTrue(Organization.objects.filter(pk=twin.pk).exists())
        canon.refresh_from_db()
        self.assertEqual(canon.keycloak_group_path, "")

    def test_distinct_groups_sharing_a_name_are_not_merged(self) -> None:
        # Two fully-anchored orgs with the same name but different groups must
        # not be collapsed — only an id-only canon adopts a path-only twin.
        a = Organization.objects.create(
            name="Team",
            slug="a-team",
            keycloak_group_id="gid-a",
            keycloak_group_path="/a/team",
        )
        b = Organization.objects.create(
            name="Team",
            slug="b-team",
            keycloak_group_id="gid-b",
            keycloak_group_path="/b/team",
        )

        self._run()

        self.assertTrue(Organization.objects.filter(pk=a.pk).exists())
        self.assertTrue(Organization.objects.filter(pk=b.pk).exists())
