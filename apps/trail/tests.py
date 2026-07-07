from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from apps.identity.models import Organization
from apps.trail.models import TrailEntry
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
