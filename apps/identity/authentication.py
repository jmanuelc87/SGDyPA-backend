from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from jwt import InvalidTokenError, PyJWKClient
from jwt.exceptions import PyJWKClientError


class SigningKey(Protocol):
    key: Any


class JwksClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> SigningKey: ...


class BearerAuthenticationError(Exception):
    status_code = 401
    detail = "Invalid bearer token."

    def __init__(self, detail: str | None = None) -> None:
        if detail is not None:
            self.detail = detail
        super().__init__(self.detail)


class BearerAuthenticationUnavailable(BearerAuthenticationError):
    status_code = 503
    detail = "Bearer authentication is not configured."


class UserProjectionNotFound(BearerAuthenticationError):
    detail = "No local user projection exists for the token subject."


@dataclass(frozen=True)
class KeycloakOIDCConfig:
    issuer: str
    audience: str
    jwks_url: str
    algorithms: tuple[str, ...] = ("RS256",)

    @classmethod
    def from_settings(cls) -> KeycloakOIDCConfig:
        raw_config = getattr(settings, "KEYCLOAK_OIDC", {})
        issuer = raw_config.get("ISSUER")
        audience = raw_config.get("AUDIENCE")
        jwks_url = raw_config.get("JWKS_URL")
        algorithms = tuple(raw_config.get("ALGORITHMS", ("RS256",)))

        if not issuer or not audience or not jwks_url:
            raise BearerAuthenticationUnavailable(
                "KEYCLOAK_OIDC requires ISSUER, AUDIENCE, and JWKS_URL."
            )

        return cls(
            issuer=str(issuer),
            audience=str(audience),
            jwks_url=str(jwks_url),
            algorithms=algorithms,
        )


class KeycloakTokenValidator:
    def __init__(
        self,
        config: KeycloakOIDCConfig | None = None,
        jwks_client: JwksClient | None = None,
    ) -> None:
        self.config = config or KeycloakOIDCConfig.from_settings()
        self.jwks_client = jwks_client or PyJWKClient(self.config.jwks_url)

    def validate(self, token: str) -> dict[str, Any]:
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.config.algorithms),
                audience=self.config.audience,
                issuer=self.config.issuer,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except (InvalidTokenError, PyJWKClientError) as exc:
            raise BearerAuthenticationError("Invalid bearer token.") from exc

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise BearerAuthenticationError("Bearer token is missing sub.")

        return claims


def extract_bearer_token(authorization_header: str) -> str | None:
    if not authorization_header:
        return None

    scheme, separator, value = authorization_header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    if not separator or not value.strip():
        raise BearerAuthenticationError("Bearer token is missing.")

    return value.strip()


def resolve_user_from_claims(claims: dict[str, Any]) -> Any:
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise BearerAuthenticationError("Bearer token is missing sub.")

    UserModel = get_user_model()
    try:
        user = UserModel.objects.get(keycloak_sub=subject)
    except UserModel.DoesNotExist as exc:
        raise UserProjectionNotFound() from exc

    if not user.is_active:
        raise BearerAuthenticationError("Local user projection is inactive.")

    sync_user_projection(user, claims)
    return user


def sync_user_projection(user: Any, claims: dict[str, Any]) -> None:
    with transaction.atomic():
        update_fields = []
        email = claims.get("email")
        if isinstance(email, str) and email and user.email != email:
            user.email = email
            update_fields.append("email")

        first_name = claims.get("given_name")
        if isinstance(first_name, str) and user.first_name != first_name:
            user.first_name = first_name
            update_fields.append("first_name")

        last_name = claims.get("family_name")
        if isinstance(last_name, str) and user.last_name != last_name:
            user.last_name = last_name
            update_fields.append("last_name")

        if update_fields:
            user.save(update_fields=update_fields)


def authenticate_bearer_token(
    token: str,
    validator: KeycloakTokenValidator | None = None,
) -> tuple[Any, dict[str, Any]]:
    validator = validator or KeycloakTokenValidator()
    claims = validator.validate(token)
    user = resolve_user_from_claims(claims)
    return user, claims
