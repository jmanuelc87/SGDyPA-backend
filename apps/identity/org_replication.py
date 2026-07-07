"""Replicate Keycloak group membership into local Organization memberships.

This is the counterpart of ``replication.py`` (which projects Keycloak *users*).
Here a user's Keycloak **groups** drive local ``Membership`` rows and their mapped
realm/client **roles** drive ``MembershipRole`` rows (P1-P7).

Two entry points, two reconciliation modes:

* ``reconcile_from_claims`` (login) — the token carries the user's FULL current
  group + role state, so it reconciles authoritatively with ``prune=True``.
* ``apply_group_membership_change`` (GROUP_MEMBERSHIP admin event) — a single
  add/remove that carries no full state, so it is strictly INCREMENTAL.

Both only ever create or prune ``origin=keycloak`` memberships. Rows created by
the API or ``provision_user`` (``origin=manual``) are never touched. Removal is a
soft-remove (``status=REVOKED`` + drop roles); the row is never deleted because
the ``Membership`` FK is ``PROTECT`` and referenced by the audit trail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from apps.identity.authorization import assign_membership_role
from apps.identity.models import Membership, MembershipRole, Organization, Role

logger = logging.getLogger("apps.identity.org_replication")

# Slug is capped at 120 chars; leave headroom for a numeric de-collision suffix.
_MAX_SLUG_BASE = 110
_MAX_SLUG_ATTEMPTS = 50

_ROLE_NAMES = dict(Role.SystemRole.choices)


def _config() -> dict[str, Any]:
    return getattr(settings, "KEYCLOAK_ORG_REPLICATION", {}) or {}


def is_enabled() -> bool:
    return bool(_config().get("ENABLED"))


@dataclass(frozen=True)
class GroupRef:
    """A Keycloak group as seen in a claim or admin event.

    ``keycloak_group_id`` is the strong join key (admin events carry it);
    ``path`` is what the token ``groups`` claim carries; ``name`` is a display
    hint (the last path segment or the event representation's name).
    """

    keycloak_group_id: str | None = None
    path: str | None = None
    name: str | None = None


@dataclass
class ReconcileResult:
    active_org_ids: set[UUID] = field(default_factory=set)
    revoked_org_ids: set[UUID] = field(default_factory=set)


def parse_groups_from_claims(
    claims: dict[str, Any], *, groups_claim: str = "groups"
) -> list[GroupRef] | None:
    """Read group paths from token claims.

    Tri-state return:
    * ``None`` — the claim is ABSENT or malformed. Reconciliation must skip and
      NEVER prune (a missing group mapper must not revoke every membership).
    * ``[]`` — the claim is present but empty: authoritative "no groups", prune.
    * ``list[GroupRef]`` — one ref per non-empty path string.
    """

    if groups_claim not in claims:
        return None
    raw = claims.get(groups_claim)
    if not isinstance(raw, list):
        return None

    refs: list[GroupRef] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        path = item.strip()
        name = path.rstrip("/").split("/")[-1] or path
        refs.append(GroupRef(path=path, name=name))
    return refs


def parse_role_codes_from_claims(
    claims: dict[str, Any], role_map: dict[str, str]
) -> set[str]:
    """Map the token's realm + client role names to local P1-P7 codes.

    Roles are GLOBAL: the returned codes apply to every group-derived membership.
    Unmapped Keycloak role names are dropped.
    """

    if not role_map:
        return set()

    names: set[str] = set()
    realm = claims.get("realm_access")
    if isinstance(realm, dict) and isinstance(realm.get("roles"), list):
        names.update(r for r in realm["roles"] if isinstance(r, str))

    resource = claims.get("resource_access")
    if isinstance(resource, dict):
        for client in resource.values():
            if isinstance(client, dict) and isinstance(client.get("roles"), list):
                names.update(r for r in client["roles"] if isinstance(r, str))

    return {role_map[name] for name in names if name in role_map}


def _normalize_path(path: str | None) -> str:
    """Canonicalize a Keycloak group path so lookups match reliably.

    Keycloak paths are ``/parent/child`` with no trailing slash; a stray trailing
    slash (``/acme/``) or surrounding whitespace must not fork a second org. The
    leading slash and interior structure are preserved; ``/`` maps to ``/``.
    """

    if not path:
        return ""
    normalized = path.strip()
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


def _slug_base(group_ref: GroupRef) -> str:
    if group_ref.path:
        raw = group_ref.path.strip("/").replace("/", "-")
    else:
        raw = group_ref.name or ""
    return (slugify(raw) or "org")[:_MAX_SLUG_BASE]


def _org_name(group_ref: GroupRef) -> str:
    if group_ref.name:
        return group_ref.name
    if group_ref.path:
        leaf = group_ref.path.rstrip("/").split("/")[-1]
        if leaf:
            return leaf
    return group_ref.keycloak_group_id or "Organization"


def find_organization(group_ref: GroupRef) -> Organization | None:
    """Match an existing Organization for a group WITHOUT creating one.

    Matches by ``keycloak_group_id`` first (the strong key), then falls back to
    ``keycloak_group_path`` (what the token claim carries). Backfill is
    bidirectional: an id-anchored row missing its path (a GROUP CREATE event
    whose representation carried no path) gets the path filled in, and a
    path-created row gets its id filled in, so a group can never fork into an
    id-only and a path-only row.
    """

    group_id = group_ref.keycloak_group_id
    path = _normalize_path(group_ref.path)
    org: Organization | None
    if group_id:
        org = Organization.objects.filter(keycloak_group_id=group_id).first()
        if org is not None:
            if path and not org.keycloak_group_path:
                org = _backfill_group_path(org, path)
            return org

    if path:
        org = (
            Organization.objects.filter(keycloak_group_path=path)
            .order_by("created_at")
            .first()
        )
        if org is not None:
            if group_id and not org.keycloak_group_id:
                org = _backfill_group_id(org, group_id)
            return org
    return None


def _backfill_group_id(org: Organization, group_id: str) -> Organization:
    try:
        with transaction.atomic():
            locked: Organization = Organization.objects.select_for_update().get(
                pk=org.pk
            )
            if not locked.keycloak_group_id:
                locked.keycloak_group_id = group_id
                locked.save(update_fields=["keycloak_group_id", "updated_at"])
            return locked
    except IntegrityError:
        # Another writer claimed this group_id concurrently; use their row.
        return cast(
            Organization,
            Organization.objects.filter(keycloak_group_id=group_id).first() or org,
        )


def _backfill_group_path(org: Organization, path: str) -> Organization:
    try:
        with transaction.atomic():
            locked: Organization = Organization.objects.select_for_update().get(
                pk=org.pk
            )
            if not locked.keycloak_group_path:
                locked.keycloak_group_path = path
                locked.save(update_fields=["keycloak_group_path", "updated_at"])
            return locked
    except IntegrityError:
        # Another org already owns this path (unique constraint); use theirs.
        return cast(
            Organization,
            Organization.objects.filter(keycloak_group_path=path).first() or org,
        )


def resolve_or_create_organization(
    group_ref: GroupRef, *, source: str
) -> Organization | None:
    """Return the local Organization for a group, auto-creating it if unseen.

    Slug is derived from the group path (so ``/a/team`` and ``/b/team`` differ)
    and de-collided with a numeric suffix; it is frozen at creation and never
    rewritten on a later Keycloak group rename.
    """

    existing = find_organization(group_ref)
    if existing is not None:
        return existing

    name = _org_name(group_ref)
    path = _normalize_path(group_ref.path)
    base = _slug_base(group_ref)
    for attempt in range(_MAX_SLUG_ATTEMPTS):
        slug = base if attempt == 0 else f"{base}-{attempt + 1}"
        try:
            with transaction.atomic():
                org: Organization = Organization.objects.create(
                    name=name,
                    slug=slug,
                    keycloak_group_id=group_ref.keycloak_group_id or None,
                    keycloak_group_path=path,
                )
            logger.info(
                "keycloak.org.created",
                extra={
                    "organization_id": str(org.id),
                    "slug": org.slug,
                    "keycloak_group_id": group_ref.keycloak_group_id,
                    "keycloak_group_path": path,
                    "source": source,
                },
            )
            return org
        except IntegrityError:
            # A concurrent writer won the race on the group id or path (return
            # their row via the same id/path lookup, backfilling as needed), or
            # only the slug collided (no matching org — retry with a suffix).
            recovered = find_organization(group_ref)
            if recovered is not None:
                return recovered
            continue

    logger.error(
        "keycloak.org.slug_exhausted",
        extra={"slug_base": base, "keycloak_group_path": group_ref.path},
    )
    return None


def sync_organization_from_group(
    group_ref: GroupRef, *, source: str
) -> Organization | None:
    """Pre-create or refresh the local Organization for a Keycloak group.

    Driven by GROUP CREATE/UPDATE admin events (no user involved), so a group
    materializes locally before anyone joins it. Auto-creates when unseen; on an
    existing row it refreshes the mutable projected fields (name, path) on a
    rename/move but keeps the slug frozen for URL stability.
    """

    org = resolve_or_create_organization(group_ref, source=source)
    if org is None:
        return None

    path = _normalize_path(group_ref.path)
    with transaction.atomic():
        locked: Organization = Organization.objects.select_for_update().get(pk=org.pk)
        updates: list[str] = []
        if group_ref.name and locked.name != group_ref.name:
            locked.name = group_ref.name
            updates.append("name")
        if path and locked.keycloak_group_path != path:
            locked.keycloak_group_path = path
            updates.append("keycloak_group_path")
        if updates:
            updates.append("updated_at")
            locked.save(update_fields=updates)
    return locked


def _get_role(code: str) -> Role:
    role, _ = Role.objects.get_or_create(
        code=code, defaults={"name": _ROLE_NAMES.get(code, code)}
    )
    return cast(Role, role)


def _sync_membership_roles(
    membership: Membership, role_codes: set[str], *, prune: bool
) -> None:
    """Add mapped roles to a KC-managed membership; prune the rest when asked.

    Skipped for inactive memberships (inactive/expired membership or inactive
    org): ``assign_membership_role`` requires an active membership, so writing
    roles there would raise rather than silently do nothing.
    """

    if not membership.is_active:
        logger.info(
            "keycloak.org.roles_skipped_inactive",
            extra={"membership_id": str(membership.id)},
        )
        return

    for code in role_codes:
        assign_membership_role(membership, _get_role(code))

    if prune:
        MembershipRole.objects.filter(membership=membership).exclude(
            role__code__in=role_codes
        ).delete()


def reconcile_user_memberships(
    user: Any,
    group_refs: list[GroupRef],
    role_codes: set[str],
    *,
    prune: bool,
    source: str,
) -> ReconcileResult:
    """Reconcile a user's KC-managed memberships against the desired groups.

    ``prune=True`` (login) revokes KC memberships/roles absent from the desired
    set; ``prune=False`` (admin event) only adds. Manual memberships are never
    touched. All writes are ``get_or_create`` / set-difference, so re-running
    with the same inputs is a no-op.
    """

    role_codes = set(role_codes)
    result = ReconcileResult()

    with transaction.atomic():
        locked = list(
            Membership.objects.select_for_update()
            .filter(user=user)
            .select_related("organization")
        )
        by_org_id: dict[UUID, Membership] = {m.organization_id: m for m in locked}

        desired_org_ids: set[UUID] = set()
        for ref in group_refs:
            org = resolve_or_create_organization(ref, source=source)
            if org is None:
                continue
            desired_org_ids.add(org.id)

            membership = by_org_id.get(org.id)
            if membership is None:
                membership, _created = Membership.objects.get_or_create(
                    organization=org,
                    user=user,
                    defaults={
                        "status": Membership.Status.ACTIVE,
                        "origin": Membership.Origin.KEYCLOAK,
                    },
                )
                by_org_id[org.id] = membership

            if membership.origin != Membership.Origin.KEYCLOAK:
                # Manually-managed row: leave status, origin, and roles alone.
                continue

            if membership.status != Membership.Status.ACTIVE:
                membership.status = Membership.Status.ACTIVE
                membership.save(update_fields=["status", "updated_at"])

            _sync_membership_roles(membership, role_codes, prune=prune)
            result.active_org_ids.add(org.id)

        if prune:
            for membership in locked:
                if membership.origin != Membership.Origin.KEYCLOAK:
                    continue
                if membership.organization_id in desired_org_ids:
                    continue
                if membership.status != Membership.Status.REVOKED:
                    membership.status = Membership.Status.REVOKED
                    membership.save(update_fields=["status", "updated_at"])
                MembershipRole.objects.filter(membership=membership).delete()
                result.revoked_org_ids.add(membership.organization_id)

    logger.info(
        "keycloak.org.reconciled",
        extra={
            "user_id": str(getattr(user, "pk", "")),
            "source": source,
            "pruned": prune,
            "active": len(result.active_org_ids),
            "revoked": len(result.revoked_org_ids),
        },
    )
    return result


def reconcile_from_claims(user: Any, claims: dict[str, Any], *, source: str) -> None:
    """Login entry point: authoritative reconcile from full token state.

    No-op (never prunes) when the ``groups`` claim is absent/malformed, so a
    missing group mapper can never revoke a user's memberships.
    """

    config = _config()
    group_refs = parse_groups_from_claims(
        claims, groups_claim=config.get("GROUPS_CLAIM", "groups")
    )
    if group_refs is None:
        return

    role_codes = parse_role_codes_from_claims(claims, config.get("ROLE_MAP", {}))
    reconcile_user_memberships(user, group_refs, role_codes, prune=True, source=source)


def apply_group_membership_change(
    user: Any, group_ref: GroupRef, *, added: bool, source: str
) -> Membership | None:
    """Apply a single GROUP_MEMBERSHIP admin event (incremental, no pruning).

    ``added=True`` (CREATE) auto-creates the org and a KC membership but assigns
    NO roles (the event carries none; roles reconcile at next login).
    ``added=False`` (DELETE) soft-removes the KC membership. Manual memberships
    are skipped in both directions.
    """

    with transaction.atomic():
        if added:
            org = resolve_or_create_organization(group_ref, source=source)
            if org is None:
                return None
            membership: Membership
            membership, _created = Membership.objects.get_or_create(
                organization=org,
                user=user,
                defaults={
                    "status": Membership.Status.ACTIVE,
                    "origin": Membership.Origin.KEYCLOAK,
                },
            )
            if membership.origin != Membership.Origin.KEYCLOAK:
                return membership
            if membership.status != Membership.Status.ACTIVE:
                membership.status = Membership.Status.ACTIVE
                membership.save(update_fields=["status", "updated_at"])
            return membership

        org = find_organization(group_ref)
        if org is None:
            return None
        try:
            membership = Membership.objects.select_for_update().get(
                organization=org, user=user
            )
        except Membership.DoesNotExist:
            return None
        if membership.origin != Membership.Origin.KEYCLOAK:
            return membership
        if membership.status != Membership.Status.REVOKED:
            membership.status = Membership.Status.REVOKED
            membership.save(update_fields=["status", "updated_at"])
        MembershipRole.objects.filter(membership=membership).delete()
        return membership
