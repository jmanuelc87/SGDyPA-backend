from __future__ import annotations

from typing import Any

import jwt
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.identity.models import Membership, MembershipRole, Organization, Role


class Command(BaseCommand):
    help = (
        "Create or update a local User projection keyed by its Keycloak sub so a "
        "bearer token can authenticate against the API. Optionally attach the user "
        "to an organization with a system role. Intended for local development and "
        "testing only."
    )

    def add_arguments(self, parser: Any) -> None:
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument(
            "--sub",
            help="Keycloak subject (the token 'sub' claim) to anchor the user on.",
        )
        source.add_argument(
            "--token",
            help=(
                "Raw access token. Decoded WITHOUT signature verification purely to "
                "read sub/email/name claims for provisioning."
            ),
        )

        parser.add_argument("--username", help="Username (defaults to email or sub).")
        parser.add_argument("--email", help="Email address.")
        parser.add_argument("--first-name", help="First name.")
        parser.add_argument("--last-name", help="Last name.")
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Grant Django staff+superuser flags.",
        )

        parser.add_argument(
            "--org-slug",
            help="Attach the user to this organization (created if missing).",
        )
        parser.add_argument(
            "--org-name",
            help="Organization display name (defaults to the slug).",
        )
        parser.add_argument(
            "--role",
            choices=[code for code, _ in Role.SystemRole.choices],
            help="System role code (P1-P7) to assign in the organization.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        claims: dict[str, Any] = {}
        if options["token"]:
            try:
                claims = jwt.decode(
                    options["token"], options={"verify_signature": False}
                )
            except jwt.InvalidTokenError as exc:
                raise CommandError(f"Could not decode --token: {exc}") from exc

        sub = options["sub"] or claims.get("sub")
        if not sub:
            raise CommandError("Token has no 'sub' claim; pass --sub explicitly.")

        email = options["email"] or claims.get("email") or ""
        username = options["username"] or email or sub
        first_name = options["first_name"] or claims.get("given_name") or ""
        last_name = options["last_name"] or claims.get("family_name") or ""

        org_slug = options["org_slug"]
        role_code = options["role"]
        if role_code and not org_slug:
            raise CommandError("--role requires --org-slug.")

        UserModel = get_user_model()

        with transaction.atomic():
            user, created = UserModel.objects.get_or_create(
                keycloak_sub=sub,
                defaults={"username": username},
            )
            user.email = email
            user.first_name = first_name
            user.last_name = last_name
            user.is_active = True
            if options["superuser"]:
                user.is_staff = True
                user.is_superuser = True
            if claims.get("name"):
                user.display_name = claims["name"]
            if isinstance(claims.get("email_verified"), bool):
                user.email_verified = claims["email_verified"]
            user.save()

            self.stdout.write(
                self.style.SUCCESS(
                    f"{'Created' if created else 'Updated'} user "
                    f"'{user.username}' (sub={sub})."
                )
            )

            if org_slug:
                org, org_created = Organization.objects.get_or_create(
                    slug=org_slug,
                    defaults={"name": options["org_name"] or org_slug},
                )
                membership, m_created = Membership.objects.get_or_create(
                    organization=org,
                    user=user,
                    defaults={"status": Membership.Status.ACTIVE},
                )
                if not m_created and membership.status != Membership.Status.ACTIVE:
                    membership.status = Membership.Status.ACTIVE
                    membership.save(update_fields=["status"])
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{'Created' if org_created else 'Reused'} org '{org.slug}' "
                        f"(id={org.id}); membership active."
                    )
                )

                if role_code:
                    role_name = dict(Role.SystemRole.choices)[role_code]
                    role, _ = Role.objects.get_or_create(
                        code=role_code, defaults={"name": role_name}
                    )
                    MembershipRole.objects.get_or_create(
                        membership=membership, role=role
                    )
                    self.stdout.write(
                        self.style.SUCCESS(f"Assigned role {role_code} · {role_name}.")
                    )

                self.stdout.write(
                    self.style.WARNING(
                        f"Send header  X-Organization-Id: {org.id}  "
                        "on org-scoped requests."
                    )
                )
