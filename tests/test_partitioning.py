"""Tests for the trail scalability changes (ADR-0008): read indexes, no default ordering,
and LIST-by-organization partitioning.

The index/ordering assertions run on any backend. The partitioning assertions are gated to
PostgreSQL, mirroring the existing skipif in ``tests/test_services.py`` — on sqlite the table
is flat and the partition machinery is intentionally a no-op.
"""

from __future__ import annotations

import uuid

import pytest
from apps.identity.models import Organization
from apps.trail.models import TrailEntry
from apps.trail.partitioning import partition_name_for_organization
from apps.trail.services import append_trail_entry
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection

postgres_only = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="LIST-by-organization partitioning is PostgreSQL-only; sqlite is flat.",
)


def _make_actor():
    User = get_user_model()
    return User.objects.create_user(
        username=f"actor-{uuid.uuid4()}",
        email="actor@example.com",
        display_name="Actor",
        keycloak_sub=f"kc-{uuid.uuid4()}",
    )


# --- #1 / #2: engine-agnostic model shape ------------------------------------------


def test_trailentry_has_no_default_ordering() -> None:
    # A default ordering would force an ORDER BY onto every queryset against an
    # unbounded table.
    assert TrailEntry._meta.ordering == []


def test_trailentry_declares_org_prefixed_read_indexes() -> None:
    indexes = {index.name: index for index in TrailEntry._meta.indexes}
    assert set(indexes) == {
        "trail_entry_org_target_idx",
        "trail_entry_org_actor_idx",
        "trail_entry_org_created_idx",
        "trail_entry_org_action_idx",
    }
    # Every read index leads with organization so it prunes to (and stays compact
    # within) one tenant partition.
    for index in indexes.values():
        assert index.fields[0] == "organization"


# --- #5: partitioning runtime (PostgreSQL only) ------------------------------------


@postgres_only
@pytest.mark.django_db
def test_table_is_partitioned_by_organization() -> None:
    from apps.trail.partitioning import is_partitioned

    assert is_partitioned()


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_new_org_signal_creates_partition_and_appends_route_there() -> None:
    org = Organization.objects.create(name="Part", slug=f"part-{uuid.uuid4()}")

    # The Organization post_save signal provisions the tenant partition on commit.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass(%s)", [partition_name_for_organization(org.id)]
        )
        assert cursor.fetchone()[0] is not None

    actor = _make_actor()
    append_trail_entry(
        organization=org,
        actor=actor,
        action="CREAR",
        target_entity="DOC",
        target_id=uuid.uuid4(),
    )

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tableoid::regclass::text FROM trail_trailentry "
            "WHERE organization_id = %s",
            [str(org.id)],
        )
        assert cursor.fetchone()[0] == partition_name_for_organization(org.id)
        cursor.execute(
            "SELECT count(*) FROM trail_trailentry_default WHERE organization_id = %s",
            [str(org.id)],
        )
        assert cursor.fetchone()[0] == 0


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_ensure_trail_partitions_rehomes_default_rows() -> None:
    # bulk_create bypasses the signal, so no partition exists yet and the append lands
    # in DEFAULT.
    org = Organization(name="Bulk", slug=f"bulk-{uuid.uuid4()}")
    Organization.objects.bulk_create([org])
    actor = _make_actor()
    append_trail_entry(
        organization=org,
        actor=actor,
        action="CREAR",
        target_entity="DOC",
        target_id=uuid.uuid4(),
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM trail_trailentry_default WHERE organization_id = %s",
            [str(org.id)],
        )
        assert cursor.fetchone()[0] == 1

    call_command("ensure_trail_partitions")

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass(%s)", [partition_name_for_organization(org.id)]
        )
        assert cursor.fetchone()[0] is not None
        cursor.execute(
            "SELECT count(*) FROM trail_trailentry_default WHERE organization_id = %s",
            [str(org.id)],
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM trail_trailentry WHERE organization_id = %s",
            [str(org.id)],
        )
        assert cursor.fetchone()[0] == 1


@postgres_only
@pytest.mark.django_db(transaction=True)
def test_archive_trail_partition_detaches_tenant() -> None:
    org = Organization.objects.create(name="Gone", slug=f"gone-{uuid.uuid4()}")
    actor = _make_actor()
    append_trail_entry(
        organization=org,
        actor=actor,
        action="CREAR",
        target_entity="DOC",
        target_id=uuid.uuid4(),
    )

    call_command("archive_trail_partition", organization=str(org.id))

    with connection.cursor() as cursor:
        # Detached from the live parent...
        cursor.execute(
            "SELECT count(*) FROM trail_trailentry WHERE organization_id = %s",
            [str(org.id)],
        )
        assert cursor.fetchone()[0] == 0
        # ...but preserved as a standalone table for cold storage.
        cursor.execute(
            "SELECT to_regclass(%s)",
            [f"{partition_name_for_organization(org.id)}_archived"],
        )
        assert cursor.fetchone()[0] is not None
