from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from rest_framework import serializers

from apps.identity.models import Membership, MembershipRole, Organization, Role


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "slug", "is_active"]
        read_only_fields = fields


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        fields = [
            "id",
            "email",
            "email_verified",
            "display_name",
            "first_name",
            "last_name",
        ]
        read_only_fields = fields


class RoleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Role
        fields = ["id", "code", "name", "capabilities", "is_system"]
        read_only_fields = ["id", "capabilities", "is_system"]


class MembershipRoleSerializer(serializers.ModelSerializer):
    role = RoleSerializer(read_only=True)
    role_id = serializers.PrimaryKeyRelatedField(
        source="role", queryset=Role.objects.all(), write_only=True
    )

    class Meta:
        model = MembershipRole
        fields = ["id", "role", "role_id", "assigned_at"]
        read_only_fields = ["id", "role", "assigned_at"]


class MembershipSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    user_id = serializers.PrimaryKeyRelatedField(
        source="user", queryset=get_user_model().objects.all(), write_only=True
    )
    organization = OrganizationSerializer(read_only=True)
    roles = RoleSerializer(many=True, read_only=True)
    es_invitada = serializers.BooleanField(source="is_invited", read_only=True)
    alcance = serializers.JSONField(source="scope", required=False)
    expira_en = serializers.DateTimeField(
        source="expires_at", required=False, allow_null=True
    )

    class Meta:
        model = Membership
        fields = [
            "id",
            "organization",
            "user",
            "user_id",
            "status",
            "es_invitada",
            "alcance",
            "expira_en",
            "roles",
            "invited_at",
            "accepted_at",
        ]
        read_only_fields = [
            "id",
            "organization",
            "user",
            "es_invitada",
            "roles",
            "invited_at",
            "accepted_at",
        ]

    def create(self, validated_data):
        request = self.context["request"]
        organization_id = request.headers["X-Organization-Id"]
        validated_data["organization_id"] = organization_id
        validated_data.setdefault("status", Membership.Status.INVITED)
        if Membership.objects.filter(
            organization_id=organization_id, user=validated_data["user"]
        ).exists():
            raise serializers.ValidationError(
                {"user_id": "This user already has a membership in the organization."}
            )
        try:
            # Savepoint so a lost check-then-insert race against the
            # (organization, user) unique constraint rolls back cleanly inside
            # the request's outer transaction instead of poisoning it.
            with transaction.atomic():
                return super().create(validated_data)
        except IntegrityError as exc:
            raise serializers.ValidationError(
                {"user_id": "This user already has a membership in the organization."}
            ) from exc


class MeSerializer(serializers.Serializer):
    profile = UserSerializer(source="*")
    memberships = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    orgs = serializers.SerializerMethodField()

    def get_memberships(self, user):
        memberships = user.organization_memberships.select_related(
            "organization", "user"
        ).prefetch_related("roles")
        return MembershipSerializer(memberships, many=True).data

    def get_roles(self, user):
        roles = Role.objects.filter(memberships__user=user).distinct()
        return RoleSerializer(roles, many=True).data

    def get_orgs(self, user):
        orgs = Organization.objects.filter(
            memberships__user=user,
            memberships__status__in=[
                Membership.Status.ACTIVE,
                Membership.Status.INVITED,
            ],
        ).distinct()
        return OrganizationSerializer(orgs, many=True).data
