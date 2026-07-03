from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from django.db import connection
from django.db.models import Manager, QuerySet

_current_organization: ContextVar[UUID | None] = ContextVar(
    "current_organization",
    default=None,
)

# Sentinel scope for requests without a tenant context (tenant-agnostic
# endpoints). Stored instead of NULL so tenant-scoped unique constraints enforce
# idempotency identically on every backend — a NULL organization_id would make
# each row distinct and defeat deduplication/replay.
NO_ORGANIZATION: UUID = UUID(int=0)


def get_current_organization_id() -> UUID | None:
    return _current_organization.get()


def get_current_organization_scope() -> UUID:
    """Active organization id, or the tenant-agnostic sentinel when unset."""

    return _current_organization.get() or NO_ORGANIZATION


@contextmanager
def organization_context(organization_id: UUID) -> Iterator[None]:
    token = _current_organization.set(organization_id)
    try:
        yield
    finally:
        _current_organization.reset(token)


def set_current_organization_for_transaction(organization_id: UUID) -> None:
    if connection.vendor != "postgresql":
        return

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_org', %s, true)",
            [str(organization_id)],
        )


class TenantScopedQuerySet(QuerySet):
    def for_current_organization(self) -> QuerySet:
        organization_id = get_current_organization_id()
        if organization_id is None:
            return self.none()
        return self.filter(organization_id=organization_id)


class TenantScopedManager(Manager.from_queryset(TenantScopedQuerySet)):  # type: ignore[misc]
    def get_queryset(self) -> QuerySet:
        return super().get_queryset().for_current_organization()


def user_has_organization_membership(user: Any, organization_id: UUID) -> bool:
    checker = getattr(user, "has_organization_membership", None)
    if callable(checker):
        return bool(checker(organization_id))

    for value in _iter_direct_organization_ids(user):
        if _same_organization(value, organization_id):
            return True

    for membership in _iter_memberships(user):
        value = _membership_organization_id(membership)
        if _same_organization(value, organization_id):
            return True

    return False


def _iter_direct_organization_ids(user: Any) -> Iterator[Any]:
    for attr_name in ("organization_ids", "organizations"):
        value = getattr(user, attr_name, None)
        if value is None:
            continue

        if _is_iterable(value):
            yield from value


def _iter_memberships(user: Any) -> Iterator[Any]:
    for attr_name in ("memberships", "organization_memberships"):
        source = getattr(user, attr_name, None)
        if source is None:
            continue

        if hasattr(source, "all") and callable(source.all):
            yield from source.all()
        elif _is_iterable(source):
            yield from source


def _membership_organization_id(membership: Any) -> Any:
    if isinstance(membership, dict):
        return membership.get("organization_id") or membership.get("organization")

    organization_id = getattr(membership, "organization_id", None)
    if organization_id is not None:
        return organization_id

    organization = getattr(membership, "organization", None)
    if organization is not None:
        return getattr(organization, "id", organization)

    return membership


def _same_organization(value: Any, organization_id: UUID) -> bool:
    if value is None:
        return False

    try:
        return UUID(str(value)) == organization_id
    except (TypeError, ValueError):
        return False


def _is_iterable(value: Any) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, str | bytes)
