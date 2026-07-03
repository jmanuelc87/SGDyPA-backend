from __future__ import annotations

from enum import StrEnum
from typing import Final
from uuid import UUID

from django.db import transaction

from apps.identity.models import Membership, MembershipRole, Role


class Capability(StrEnum):
    COMMISSION_AUDIT = "commission_audit"
    MANAGE_FINDINGS = "manage_findings"
    APPEAL_FINDING_FACT = "appeal_finding_fact"
    DECIDE_CLOSURE_BRANCH = "decide_closure_branch"
    CREATE_CAPA = "create_capa"
    VERIFY_CAPA_EFFECTIVENESS = "verify_capa_effectiveness"
    REQUEST_DISPOSITION = "request_disposition"
    APPROVE_DISPOSITION = "approve_disposition"
    READ = "read"


ROLE_CAPABILITIES: Final[dict[str, frozenset[Capability]]] = {
    "P1": frozenset(
        {
            Capability.MANAGE_FINDINGS,
            Capability.DECIDE_CLOSURE_BRANCH,
            Capability.VERIFY_CAPA_EFFECTIVENESS,
            Capability.READ,
        }
    ),
    "P2": frozenset({Capability.MANAGE_FINDINGS, Capability.READ}),
    "P3": frozenset(
        {
            Capability.APPEAL_FINDING_FACT,
            Capability.CREATE_CAPA,
            Capability.READ,
        }
    ),
    "P4": frozenset({Capability.REQUEST_DISPOSITION, Capability.READ}),
    "P5": frozenset(
        {
            Capability.COMMISSION_AUDIT,
            Capability.VERIFY_CAPA_EFFECTIVENESS,
            Capability.APPROVE_DISPOSITION,
            Capability.READ,
        }
    ),
    "P6": frozenset({Capability.READ}),
    "P7": frozenset({Capability.READ}),
}


class AuthorizationError(Exception):
    """Raised when server-side authorization denies an action."""


def seed_system_roles() -> None:
    for code, name in Role.SystemRole.choices:
        Role.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "capabilities": sorted(ROLE_CAPABILITIES[code]),
                "is_system": True,
            },
        )


def assign_membership_role(membership: Membership, role: Role) -> MembershipRole:
    if not membership.is_active:
        raise AuthorizationError("Membership must be active to receive roles.")
    return MembershipRole.objects.get_or_create(membership=membership, role=role)[0]


def revoke_membership_role(
    membership: Membership, role: Role
) -> tuple[int, dict[str, int]]:
    return MembershipRole.objects.filter(membership=membership, role=role).delete()


def membership_has_capability(
    membership: Membership,
    capability: Capability | str,
    *,
    object_scope: dict[str, object] | None = None,
) -> bool:
    if not membership.is_active:
        return False

    requested = str(capability)
    assigned_roles = list(membership.roles.all())
    has_role_capability = any(requested in role.capabilities for role in assigned_roles)
    if not has_role_capability:
        return False

    if _is_third_party_read(assigned_roles, requested):
        return _scope_allows(membership.scope, object_scope)

    return True


def user_has_capability(
    user: object,
    organization_id: UUID,
    capability: Capability | str,
    *,
    object_scope: dict[str, object] | None = None,
) -> bool:
    membership = (
        Membership.objects.active()
        .filter(user=user, organization_id=organization_id)
        .prefetch_related("roles")
        .first()
    )
    return bool(
        membership
        and membership_has_capability(
            membership,
            capability,
            object_scope=object_scope,
        )
    )


def require_capability(
    user: object,
    organization_id: UUID,
    capability: Capability | str,
    *,
    object_scope: dict[str, object] | None = None,
) -> None:
    # Enforce at call time, inside the transaction that mutates/reads the object.
    with transaction.atomic():
        if not user_has_capability(
            user,
            organization_id,
            capability,
            object_scope=object_scope,
        ):
            raise AuthorizationError("User lacks the required organization capability.")


def _is_third_party_read(assigned_roles: list[Role], requested: str) -> bool:
    return requested == Capability.READ and any(
        role.code == "P7" for role in assigned_roles
    )


def _scope_allows(
    membership_scope: dict[str, object], object_scope: dict[str, object] | None
) -> bool:
    if not membership_scope:
        return False
    if object_scope is None:
        return True

    for key, requested_value in object_scope.items():
        allowed_value = membership_scope.get(key)
        if isinstance(allowed_value, list):
            if requested_value not in allowed_value:
                return False
        elif allowed_value != requested_value:
            return False
    return True
