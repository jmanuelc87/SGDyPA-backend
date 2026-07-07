"""Reconcile per-tenant ``trail_trailentry`` partitions (ADR-0008).

Backstop for the ``Organization`` ``post_save`` signal: creates a partition for every org
that lacks one (e.g. orgs imported in bulk, or created before partitioning was enabled) and,
unless ``--no-rehome`` is passed, moves any rows that landed in the ``DEFAULT`` partition into
their proper tenant partition. Idempotent; safe to run on every deploy or on a schedule.

Postgres-only — prints a clear no-op notice on other backends.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.identity.models import Organization
from apps.trail.partitioning import (
    DEFAULT_PARTITION,
    PARENT_TABLE,
    create_partition_for_organization,
    is_partitioned,
    partition_name_for_organization,
)


class Command(BaseCommand):
    help = "Create missing per-organization trail partitions and re-home DEFAULT rows."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--no-rehome",
            action="store_true",
            help="Only create missing partitions; leave DEFAULT-partition rows in place.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without executing any DDL/DML.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if connection.vendor != "postgresql":
            self.stdout.write(
                "Not PostgreSQL; trail_trailentry is a flat table. Nothing to do."
            )
            return
        if not is_partitioned():
            raise CommandError(
                "trail_trailentry is not partitioned. Apply migration trail.0004 first."
            )

        dry_run = options["dry_run"]
        no_rehome = options["no_rehome"]

        # Orgs whose rows are still in DEFAULT (created before their partition existed). A
        # partition FOR VALUES IN (org) cannot be created while DEFAULT holds matching rows,
        # so these must be re-homed first (which lifts the rows out, then creates the
        # partition).
        default_org_ids = self._default_org_ids()

        rehomed = 0
        if not no_rehome:
            rehomed = self._rehome_default_rows(default_org_ids, dry_run=dry_run)

        # Create partitions for the remaining orgs — those with no partition and no DEFAULT
        # rows. Orgs still stuck in DEFAULT (only possible under --no-rehome) are skipped with
        # a warning, since creating their partition would fail until their rows are re-homed.
        created = 0
        for organization_id in Organization.objects.values_list("id", flat=True):
            name = partition_name_for_organization(organization_id)
            if self._partition_exists(name):
                continue
            if organization_id in default_org_ids and no_rehome:
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping {organization_id}: rows in DEFAULT; "
                        "rerun without --no-rehome."
                    )
                )
                continue
            if organization_id in default_org_ids:
                continue  # already created during re-homing above
            created += 1
            if dry_run:
                self.stdout.write(
                    f"[dry-run] would create partition {name} for {organization_id}"
                )
            else:
                create_partition_for_organization(organization_id)
                self.stdout.write(f"Created partition {name} for {organization_id}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. partitions_created={created} rows_rehomed={rehomed}"
                + (" (dry-run)" if dry_run else "")
            )
        )

    def _partition_exists(self, name: str) -> bool:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", [name])
            return cursor.fetchone()[0] is not None

    def _default_org_ids(self) -> list[UUID]:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT DISTINCT organization_id FROM {DEFAULT_PARTITION}")
            return [row[0] for row in cursor.fetchall()]

    def _rehome_default_rows(self, org_ids: list[UUID], *, dry_run: bool) -> int:
        """Move rows sitting in the DEFAULT partition into their tenant partitions.

        Postgres refuses to create a partition ``FOR VALUES IN (org)`` while DEFAULT still
        holds matching rows, so per org the order must be: lift the rows out to a temp table,
        delete them from DEFAULT, create the tenant partition, then reinsert through the
        parent so they route into it. Each org is done in its own transaction.
        """

        rehomed = 0
        for organization_id in org_ids:
            if dry_run:
                self.stdout.write(
                    f"[dry-run] would re-home DEFAULT rows for org {organization_id}"
                )
                continue
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "CREATE TEMP TABLE _trail_rehome ON COMMIT DROP AS "
                    f"SELECT * FROM {DEFAULT_PARTITION} WHERE organization_id = %s",
                    [organization_id],
                )
                cursor.execute(
                    f"DELETE FROM {DEFAULT_PARTITION} WHERE organization_id = %s",
                    [organization_id],
                )
                create_partition_for_organization(organization_id)
                cursor.execute(
                    f"INSERT INTO {PARENT_TABLE} SELECT * FROM _trail_rehome"
                )
                rehomed += cursor.rowcount
        return rehomed
