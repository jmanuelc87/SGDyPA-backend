from __future__ import annotations

from django.db import migrations, models


def merge_duplicate_group_orgs(apps, schema_editor):
    """Collapse orgs that forked into an id-only and a path-only row per group.

    Before path derivation landed, a Keycloak GROUP CREATE event produced an
    org with a ``keycloak_group_id`` but an empty path, and the user's next
    login produced a second org with the ``keycloak_group_path`` but a null id.
    They share nothing but the group ``name``, which is the only bridge back.

    For each id-anchored row missing its path, adopt the path from a same-named
    path-anchored twin, repoint that twin's memberships onto the canonical row,
    and delete the twin. A twin that already carries audit history is left in
    place (its per-org trail hash chain cannot be safely re-parented) for manual
    reconciliation; the canonical row keeps its empty path so the unique
    constraint added next still holds.
    """

    Organization = apps.get_model("identity", "Organization")
    Membership = apps.get_model("identity", "Membership")
    TrailEntry = apps.get_model("trail", "TrailEntry")

    canonical_rows = Organization.objects.filter(
        keycloak_group_id__isnull=False, keycloak_group_path=""
    )
    for canon in canonical_rows:
        twins = (
            Organization.objects.filter(keycloak_group_id__isnull=True, name=canon.name)
            .exclude(pk=canon.pk)
            .exclude(keycloak_group_path="")
            .order_by("created_at")
        )
        adopted_path = ""
        for twin in twins:
            if TrailEntry.objects.filter(organization=twin).exists():
                # Merging would break the twin's audit hash chain; leave it.
                continue
            for membership in Membership.objects.filter(organization=twin):
                clashes = (
                    Membership.objects.filter(
                        organization=canon, user_id=membership.user_id
                    )
                    .exclude(pk=membership.pk)
                    .exists()
                )
                if clashes:
                    # Canonical already has this user; drop the duplicate row
                    # (its MembershipRole rows cascade away with it).
                    membership.delete()
                else:
                    membership.organization = canon
                    membership.save(update_fields=["organization"])
            if not adopted_path:
                adopted_path = twin.keycloak_group_path
            twin.delete()
        # Only adopt the path if no surviving row still holds it (e.g. a
        # trail-bearing twin we deliberately skipped), so the unique constraint
        # added next cannot fail.
        if (
            adopted_path
            and not canon.keycloak_group_path
            and not Organization.objects.filter(keycloak_group_path=adopted_path)
            .exclude(pk=canon.pk)
            .exists()
        ):
            canon.keycloak_group_path = adopted_path
            canon.save(update_fields=["keycloak_group_path"])


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0005_membership_origin_organization_keycloak_group_id_and_more"),
        ("trail", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(merge_duplicate_group_orgs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.UniqueConstraint(
                fields=["keycloak_group_path"],
                condition=~models.Q(keycloak_group_path=""),
                name="uniq_organization_keycloak_group_path",
            ),
        ),
    ]
