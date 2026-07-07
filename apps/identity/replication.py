from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction

logger = logging.getLogger("apps.identity.replication")


def _str_or_none(value: Any) -> str | None:
    """Return the value only when it is a string, else ``None``.

    ``None`` means "the source did not carry this attribute; leave the local
    value untouched". An empty string IS a value (Keycloak lets you clear a
    name), so it is preserved rather than dropped.
    """

    return value if isinstance(value, str) else None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


@dataclass(frozen=True)
class ProjectionAttributes:
    """Normalized snapshot of a Keycloak user's replicable attributes.

    Every field is optional. A ``None`` field means the source (login token
    claims vs. admin-event representation) did not provide that attribute, so
    the existing local value is left untouched. Identity is keyed on the
    Keycloak ``sub`` only; email here is a projected snapshot and is NEVER used
    as a join key (ADR-0002).
    """

    username: str | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    email_verified: bool | None = None
    enabled: bool | None = None

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> ProjectionAttributes:
        """Build attributes from OIDC token claims (login-time sync)."""

        return cls(
            username=_str_or_none(claims.get("preferred_username")),
            email=_str_or_none(claims.get("email")),
            first_name=_str_or_none(claims.get("given_name")),
            last_name=_str_or_none(claims.get("family_name")),
            display_name=_str_or_none(claims.get("name")),
            email_verified=_bool_or_none(claims.get("email_verified")),
            # A token is never issued for a disabled user, so login never
            # carries `enabled`; leaving it None avoids ever reactivating a
            # user the admin-event path deactivated.
        )

    @classmethod
    def from_representation(
        cls, representation: dict[str, Any]
    ) -> ProjectionAttributes:
        """Build attributes from a Keycloak admin-event user representation.

        Keycloak has no top-level display name, so it is composed from
        ``firstName``/``lastName`` when either is present.
        """

        first_name = _str_or_none(representation.get("firstName"))
        last_name = _str_or_none(representation.get("lastName"))
        composed = " ".join(part for part in (first_name, last_name) if part).strip()

        return cls(
            username=_str_or_none(representation.get("username")),
            email=_str_or_none(representation.get("email")),
            first_name=first_name,
            last_name=last_name,
            display_name=composed or None,
            email_verified=_bool_or_none(representation.get("emailVerified")),
            enabled=_bool_or_none(representation.get("enabled")),
        )


def apply_projection(user: Any, attrs: ProjectionAttributes) -> list[str]:
    """Copy provided attributes onto ``user`` in memory; return changed fields.

    Does not save. Callers persist the returned ``update_fields``.
    """

    update_fields: list[str] = []

    # Username is a non-empty, unique column; an empty/absent source value must
    # never clear it. This also heals legacy rows created before username was
    # projected, where it was seeded with the immutable `sub` (a UUID).
    if (
        attrs.username is not None
        and attrs.username
        and user.username != attrs.username
    ):
        user.username = attrs.username
        update_fields.append("username")

    # Email is a value that must be non-empty to overwrite; an empty email in a
    # source is treated as "absent" rather than clearing a real address.
    if attrs.email is not None and attrs.email and user.email != attrs.email:
        user.email = attrs.email
        update_fields.append("email")

    if attrs.first_name is not None and user.first_name != attrs.first_name:
        user.first_name = attrs.first_name
        update_fields.append("first_name")

    if attrs.last_name is not None and user.last_name != attrs.last_name:
        user.last_name = attrs.last_name
        update_fields.append("last_name")

    if attrs.display_name is not None and user.display_name != attrs.display_name:
        user.display_name = attrs.display_name
        update_fields.append("display_name")

    if attrs.email_verified is not None and user.email_verified != attrs.email_verified:
        user.email_verified = attrs.email_verified
        update_fields.append("email_verified")

    if attrs.enabled is not None and user.is_active != attrs.enabled:
        user.is_active = attrs.enabled
        update_fields.append("is_active")

    return update_fields


def upsert_user_projection(
    sub: str,
    attrs: ProjectionAttributes,
    *,
    source: str,
) -> tuple[Any, bool, list[str]]:
    """Create or update the local User projection keyed on ``keycloak_sub``.

    ``source`` is a short label (e.g. ``"admin-event"``) recorded in logs so the
    write path is auditable. Returns ``(user, created, changed_fields)``.
    """

    if not sub:
        raise ValueError("upsert_user_projection requires a non-empty sub")

    UserModel = get_user_model()
    with transaction.atomic():
        user, created = UserModel.objects.select_for_update().get_or_create(
            keycloak_sub=sub,
            # Seed with the real Keycloak username; fall back to email, then to
            # the immutable sub as a last resort (username is NOT NULL and must
            # be unique, and email may be absent). apply_projection heals the
            # username on a later event/login once one is available.
            defaults={"username": attrs.username or attrs.email or sub},
        )
        changed = apply_projection(user, attrs)
        if changed:
            user.save(update_fields=changed)

    logger.info(
        "keycloak.projection.upserted",
        extra={
            "keycloak_sub": sub,
            "source": source,
            # NOT "created": it is a reserved LogRecord attribute and raises
            # KeyError when logging is enabled at INFO.
            "was_created": created,
            "changed_fields": changed,
        },
    )
    return user, created, changed


def deactivate_user_projection(sub: str, *, source: str) -> bool:
    """Reflect a Keycloak delete/disable by deactivating the local projection.

    Never deletes the local row (it is referenced by memberships and the audit
    trail with PROTECT). Returns ``True`` if a row was deactivated.
    """

    if not sub:
        raise ValueError("deactivate_user_projection requires a non-empty sub")

    UserModel = get_user_model()
    with transaction.atomic():
        try:
            user = UserModel.objects.select_for_update().get(keycloak_sub=sub)
        except UserModel.DoesNotExist:
            logger.info(
                "keycloak.projection.deactivate_missing",
                extra={"keycloak_sub": sub, "source": source},
            )
            return False

        if not user.is_active:
            return False

        user.is_active = False
        user.save(update_fields=["is_active"])

    logger.info(
        "keycloak.projection.deactivated",
        extra={"keycloak_sub": sub, "source": source},
    )
    return True
