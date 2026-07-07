from __future__ import annotations

import threading
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction

from apps.identity.models import Organization
from apps.trail.models import LedgerHead, TrailEntry
from apps.trail.services import append_trail_entry, next_sequence_for_organization


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Acme", slug=f"acme-{uuid.uuid4()}")


@pytest.fixture
def actor():
    User = get_user_model()
    return User.objects.create_user(
        username=f"actor-{uuid.uuid4()}",
        email="actor@example.com",
        display_name="Actor Original",
        keycloak_sub=f"kc-{uuid.uuid4()}",
    )


@pytest.mark.django_db
def test_append_trail_entry_allocates_tenant_sequence_and_identity_snapshot(
    organization: Organization, actor
) -> None:
    target_id = uuid.uuid4()

    first = append_trail_entry(
        organization=organization,
        actor=actor,
        action="CREAR",
        target_entity="DOCUMENT",
        target_id=target_id,
        payload={"name": "manual.pdf"},
    )
    actor.email = "renamed@example.com"
    actor.display_name = "Actor Renamed"
    actor.save(update_fields=["email", "display_name"])
    second = append_trail_entry(
        organization=organization,
        actor=actor,
        action="LEER_CONTROLADO",
        target_entity="DOCUMENT",
        target_id=target_id,
    )

    assert first.sequence == 1
    assert first.actor_email_snapshot == "actor@example.com"
    assert first.actor_display_name_snapshot == "Actor Original"
    assert second.sequence == 2
    assert second.previous_hash == first.entry_hash
    assert second.actor_email_snapshot == "renamed@example.com"
    assert second.actor_display_name_snapshot == "Actor Renamed"
    assert next_sequence_for_organization(organization) == 3


@pytest.mark.django_db
def test_append_advances_materialized_ledger_head(
    organization: Organization, actor
) -> None:
    append_trail_entry(
        organization=organization,
        actor=actor,
        action="CREAR",
        target_entity="DOCUMENT",
        target_id=uuid.uuid4(),
    )
    second = append_trail_entry(
        organization=organization,
        actor=actor,
        action="LEER_CONTROLADO",
        target_entity="DOCUMENT",
        target_id=uuid.uuid4(),
    )

    head = LedgerHead.objects.get(organization=organization)
    assert head.ultima_secuencia == 2
    assert head.ultimo_hash == second.entry_hash


@pytest.mark.django_db
def test_first_append_lazily_seeds_single_ledger_head(
    organization: Organization, actor
) -> None:
    assert not LedgerHead.objects.filter(organization=organization).exists()

    first = append_trail_entry(
        organization=organization,
        actor=actor,
        action="CREAR",
        target_entity="DOCUMENT",
        target_id=uuid.uuid4(),
    )

    heads = LedgerHead.objects.filter(organization=organization)
    assert heads.count() == 1
    head = heads.get()
    assert head.ultima_secuencia == 1
    assert head.ultimo_hash == first.entry_hash


@pytest.mark.django_db
def test_trail_entry_rolls_back_with_domain_transaction(
    organization: Organization, actor
) -> None:
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            append_trail_entry(
                organization=organization,
                actor=actor,
                action="CREAR",
                target_entity="DOCUMENT",
                target_id=uuid.uuid4(),
            )
            raise IntegrityError("domain write failed")

    assert TrailEntry.objects.count() == 0


@pytest.mark.django_db
def test_sequence_is_scoped_per_tenant(actor) -> None:
    first_org = Organization.objects.create(name="One", slug=f"one-{uuid.uuid4()}")
    second_org = Organization.objects.create(name="Two", slug=f"two-{uuid.uuid4()}")

    first = append_trail_entry(
        organization=first_org,
        actor=actor,
        action="CREAR",
        target_entity="DOCUMENT",
        target_id=uuid.uuid4(),
    )
    second = append_trail_entry(
        organization=second_org,
        actor=actor,
        action="CREAR",
        target_entity="DOCUMENT",
        target_id=uuid.uuid4(),
    )

    assert first.sequence == 1
    assert second.sequence == 1


@pytest.mark.django_db
def test_trail_entry_rejects_updates_and_deletes(
    organization: Organization, actor
) -> None:
    entry = append_trail_entry(
        organization=organization,
        actor=actor,
        action="CREAR",
        target_entity="DOCUMENT",
        target_id=uuid.uuid4(),
    )

    entry.action = "EDITAR"
    with pytest.raises(TrailEntry.AppendOnlyError):
        entry.save()
    with pytest.raises(TrailEntry.AppendOnlyError):
        entry.delete()
    with pytest.raises(TrailEntry.AppendOnlyError):
        TrailEntry.objects.filter(pk=entry.pk).update(action="EDITAR")
    with pytest.raises(TrailEntry.AppendOnlyError):
        TrailEntry.objects.filter(pk=entry.pk).delete()


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="Real row locking (SELECT FOR UPDATE) requires PostgreSQL; sqlite is a no-op.",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_appends_keep_chain_dense_and_verifiable() -> None:
    """ADR-0008 mandatory concurrency invariant: N simultaneous same-tenant writers.

    Asserts (a) a dense sequence with no gaps/dupes, (b) a verifiable hash chain, and
    (c) the UNIQUE(organization, sequence) backstop firing on a forced double writer.
    """

    User = get_user_model()
    organization = Organization.objects.create(
        name="Concurrent", slug=f"concurrent-{uuid.uuid4()}"
    )
    actor = User.objects.create_user(
        username=f"actor-{uuid.uuid4()}",
        email="actor@example.com",
        display_name="Actor",
        keycloak_sub=f"kc-{uuid.uuid4()}",
    )

    writers = 8
    start = threading.Barrier(writers)
    errors: list[Exception] = []

    def worker() -> None:
        start.wait()
        try:
            append_trail_entry(
                organization=organization,
                actor=actor,
                action="CREAR",
                target_entity="DOCUMENT",
                target_id=uuid.uuid4(),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced via the errors list below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []

    entries = list(
        TrailEntry.objects.filter(organization=organization).order_by("sequence")
    )
    # (a) dense sequence 1..N, no gaps or duplicates.
    assert [entry.sequence for entry in entries] == list(range(1, writers + 1))
    # (b) chain verifies link by link.
    previous_hash = ""
    for entry in entries:
        assert entry.previous_hash == previous_hash
        previous_hash = entry.entry_hash
    # ...and the materialized head matches the tip.
    head = LedgerHead.objects.get(organization=organization)
    assert head.ultima_secuencia == writers
    assert head.ultimo_hash == entries[-1].entry_hash

    # (c) forcing a duplicate (organization, sequence) trips the UNIQUE backstop.
    with pytest.raises(IntegrityError):
        TrailEntry.objects.create(
            organization=organization,
            actor=actor,
            actor_email_snapshot=actor.email,
            actor_display_name_snapshot=actor.display_name,
            action="CREAR",
            target_entity="DOCUMENT",
            target_id=uuid.uuid4(),
            payload={},
            sequence=entries[-1].sequence,
            previous_hash=entries[-1].entry_hash,
            entry_hash="deadbeef",
        )
