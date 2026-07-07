from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from django.contrib.auth import get_user_model
from django.http import HttpRequest, JsonResponse
from django.test import RequestFactory, TestCase, override_settings

from apps.identity.authentication import (
    BearerAuthenticationError,
    KeycloakOIDCConfig,
    KeycloakTokenValidator,
    resolve_user_from_claims,
)
from apps.identity.middleware import KeycloakBearerAuthenticationMiddleware
from apps.identity.models import Membership, Organization

ISSUER = "http://keycloak.example/realms/sgdypa"
AUDIENCE = "sgdypa-api"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"


def build_private_key() -> RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def encode_token(
    private_key: RSAPrivateKey,
    subject: str,
    **overrides: object,
) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "email": "current@example.com",
        "given_name": "Current",
        "family_name": "User",
        "email_verified": True,
        "name": "Current User",
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


class KeycloakTokenValidatorTests(TestCase):
    def test_validates_rs256_token_against_keycloak_issuer_and_audience(self) -> None:
        private_key = build_private_key()
        token = encode_token(private_key, "kc-sub-1")
        jwks_client = SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(
                key=private_key.public_key()
            )
        )
        validator = KeycloakTokenValidator(
            config=KeycloakOIDCConfig(
                issuer=ISSUER,
                audience=AUDIENCE,
                jwks_url=JWKS_URL,
            ),
            jwks_client=jwks_client,
        )

        claims = validator.validate(token)

        self.assertEqual(claims["sub"], "kc-sub-1")

    def test_rejects_token_with_wrong_issuer(self) -> None:
        private_key = build_private_key()
        token = encode_token(private_key, "kc-sub-1", iss="http://wrong-issuer")
        jwks_client = SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(
                key=private_key.public_key()
            )
        )
        validator = KeycloakTokenValidator(
            config=KeycloakOIDCConfig(
                issuer=ISSUER,
                audience=AUDIENCE,
                jwks_url=JWKS_URL,
            ),
            jwks_client=jwks_client,
        )

        with self.assertRaises(BearerAuthenticationError):
            validator.validate(token)


@override_settings(
    KEYCLOAK_OIDC={
        "ISSUER": ISSUER,
        "AUDIENCE": AUDIENCE,
        "JWKS_URL": JWKS_URL,
        "ALGORITHMS": ("RS256",),
    }
)
class KeycloakBearerAuthenticationMiddlewareTests(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.private_key = build_private_key()
        self.middleware = KeycloakBearerAuthenticationMiddleware(self.response)

    def response(self, request: HttpRequest) -> JsonResponse:
        return JsonResponse(
            {
                "user_id": request.user.id,
                "email": request.user.email,
                "sub": request.keycloak_claims["sub"],
            }
        )

    def test_valid_bearer_token_resolves_user_projection_by_sub(self) -> None:
        User = get_user_model()
        user = User.objects.create_user(
            username="local-user",
            email="stale@example.com",
            keycloak_sub="kc-sub-1",
        )
        token = encode_token(self.private_key, "kc-sub-1")

        response = self._request_with_token(token)

        user.refresh_from_db()
        response_data = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response_data["user_id"], user.id)
        self.assertEqual(response_data["sub"], "kc-sub-1")
        self.assertEqual(user.email, "current@example.com")
        self.assertIs(user.email_verified, True)
        self.assertEqual(user.display_name, "Current User")

    def test_unknown_sub_is_rejected_even_when_email_matches_existing_user(
        self,
    ) -> None:
        User = get_user_model()
        User.objects.create_user(
            username="local-user",
            email="current@example.com",
            keycloak_sub="different-sub",
        )
        token = encode_token(self.private_key, "unknown-sub")

        response = self._request_with_token(token)
        response_data = json.loads(response.content)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response_data["detail"],
            "No local user projection exists for the token subject.",
        )

    def test_invalid_signature_is_rejected(self) -> None:
        token = encode_token(build_private_key(), "kc-sub-1")

        response = self._request_with_token(token)

        self.assertEqual(response.status_code, 401)

    def _request_with_token(self, token: str) -> JsonResponse:
        with patch("apps.identity.authentication.PyJWKClient") as jwks_client:
            jwks_client.return_value.get_signing_key_from_jwt.return_value = (
                SimpleNamespace(key=self.private_key.public_key())
            )
            request = self.factory.get(
                "/api/protected",
                HTTP_AUTHORIZATION=f"Bearer {token}",
            )
            return self.middleware(request)


@override_settings(
    KEYCLOAK_ORG_REPLICATION={
        "ENABLED": True,
        "GROUPS_CLAIM": "groups",
        "ROLE_MAP": {"auditor": "P2"},
    }
)
class LoginOrganizationSyncTests(TestCase):
    """resolve_user_from_claims reconciles org memberships from login claims."""

    def _user(self, sub: str = "kc-sub-1"):
        return get_user_model().objects.create_user(username=sub, keycloak_sub=sub)

    def _claims(self, sub: str = "kc-sub-1", **overrides: object) -> dict:
        claims = {"sub": sub, "email": "u@example.com"}
        claims.update(overrides)
        return claims

    def test_login_creates_memberships_and_roles(self) -> None:
        user = self._user()

        resolve_user_from_claims(
            self._claims(groups=["/acme"], realm_access={"roles": ["auditor"]})
        )

        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.origin, Membership.Origin.KEYCLOAK)
        self.assertEqual(set(membership.roles.values_list("code", flat=True)), {"P2"})

    def test_login_prunes_dropped_group(self) -> None:
        user = self._user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_path="/acme"
        )
        Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.ACTIVE,
            origin=Membership.Origin.KEYCLOAK,
        )

        resolve_user_from_claims(self._claims(groups=["/beta"]))

        dropped = Membership.objects.get(user=user, organization=org)
        self.assertEqual(dropped.status, Membership.Status.REVOKED)

    def test_login_leaves_manual_membership_intact(self) -> None:
        user = self._user()
        org = Organization.objects.create(name="Manual", slug="manual")
        manual = Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.ACTIVE,
            origin=Membership.Origin.MANUAL,
        )

        resolve_user_from_claims(self._claims(groups=[]))

        manual.refresh_from_db()
        self.assertEqual(manual.status, Membership.Status.ACTIVE)

    def test_login_without_groups_claim_does_not_prune(self) -> None:
        user = self._user()
        org = Organization.objects.create(
            name="Acme", slug="acme", keycloak_group_path="/acme"
        )
        Membership.objects.create(
            organization=org,
            user=user,
            status=Membership.Status.ACTIVE,
            origin=Membership.Origin.KEYCLOAK,
        )

        resolve_user_from_claims(self._claims())

        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.status, Membership.Status.ACTIVE)

    def test_reconcile_failure_does_not_break_auth(self) -> None:
        user = self._user()

        with patch(
            "apps.identity.org_replication.reconcile_from_claims",
            side_effect=RuntimeError("boom"),
        ):
            resolved = resolve_user_from_claims(self._claims(groups=["/acme"]))

        self.assertEqual(resolved.pk, user.pk)
