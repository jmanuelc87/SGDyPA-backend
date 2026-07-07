"""Archive a single tenant's trail partition (ADR-0008) — offboarding / data-residency.

This is the *only* sanctioned removal path for trail data. It operates on a whole tenant
partition via DDL (``DETACH PARTITION`` [+ ``DROP``]), never on individual rows, so the
append-only guarantee for live tenants is untouched. DETACH is DDL and therefore bypasses
the model-level ``TrailEntry.AppendOnlyError`` delete guard by design.

Default is detach-only: the partition survives as a standalone table
(``<partition>_archived``) you can dump to cold storage. Pass ``--drop`` to also drop it.
``--dry-run`` reports the plan.

Postgres-only.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.trail.partitioning import (
    PARENT_TABLE,
    is_partitioned,
    partition_name_for_organization,
)


class Command(BaseCommand):
    help = (
        "Detach (and optionally drop) one organization's trail partition for archival."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--organization",
            required=True,
            help="Organization UUID whose trail partition should be archived.",
        )
        parser.add_argument(
            "--drop",
            action="store_true",
            help="Drop the detached partition after detaching (irreversible).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the plan without executing any DDL.",
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

        try:
            organization_id = UUID(str(options["organization"]))
        except ValueError as exc:
            raise CommandError(
                f"Invalid organization UUID: {options['organization']!r}"
            ) from exc

        partition = partition_name_for_organization(organization_id)
        if not self._partition_exists(partition):
            raise CommandError(
                f"No partition {partition} for organization {organization_id}. Its rows "
                "(if any) are in the DEFAULT partition; run ensure_trail_partitions first."
            )

        archived = f"{partition}_archived"
        drop = options["drop"]
        if options["dry_run"]:
            plan = f"detach {partition} -> {archived}" + (" then DROP" if drop else "")
            self.stdout.write(f"[dry-run] would {plan}")
            return

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE {PARENT_TABLE} DETACH PARTITION "{partition}"')
            if drop:
                cursor.execute(f'DROP TABLE "{partition}"')
            else:
                cursor.execute(f'ALTER TABLE "{partition}" RENAME TO "{archived}"')

        if drop:
            self.stdout.write(
                self.style.SUCCESS(f"Detached and dropped partition {partition}.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Detached partition {partition} as standalone table {archived}. "
                    "Dump it to cold storage, then DROP when safe."
                )
            )

    def _partition_exists(self, name: str) -> bool:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s)", [name])
            return cursor.fetchone()[0] is not None
