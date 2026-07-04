from __future__ import annotations

import uuid

from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.identity.authorization import (
    AuthorizationError,
    Capability,
    assign_membership_role,
    require_capability,
    revoke_membership_role,
)
from apps.identity.models import Membership, Organization, Role
from apps.identity.serializers import (
    MembershipRoleSerializer,
    MembershipSerializer,
    MeSerializer,
    OrganizationSerializer,
    RoleSerializer,
    UserSerializer,
)
from apps.platform.error_codes import ErrorCode
from apps.platform.exceptions import StableAPIException


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    return Response(MeSerializer(request.user).data)


class OrganizationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = OrganizationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Organization.objects.filter(
            memberships__user=self.request.user
        ).distinct()


class UserViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        org_id = self.request.headers["X-Organization-Id"]
        return (
            type(self.request.user)
            .objects.filter(organization_memberships__organization_id=org_id)
            .distinct()
        )


def require_org_capability(request, capability: Capability) -> None:
    # Server-side RBAC gate for tenant mutations. The middleware has already
    # verified the caller is a member of X-Organization-Id; this enforces that
    # the member actually holds the capability, translating the domain
    # AuthorizationError into a stable 403 envelope.
    org_id = request.headers["X-Organization-Id"]
    try:
        require_capability(request.user, org_id, capability)
    except AuthorizationError as exc:
        raise PermissionDenied(str(exc)) from exc


def require_idempotency_header(request) -> None:
    raw_key = request.headers.get("Idempotency-Key", "").strip()
    if not raw_key:
        raise StableAPIException(
            code=ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
            message="Idempotency-Key header is required for this operation.",
        )
    try:
        uuid.UUID(raw_key)
    except ValueError as exc:
        raise StableAPIException(
            code=ErrorCode.IDEMPOTENCY_KEY_INVALID,
            message="Idempotency-Key header must be a UUID.",
        ) from exc


class RoleViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    # Roles are the fixed P1-P7 system definitions with server-enforced,
    # read-only capabilities; there is no tenant-scoped role authoring, so this
    # viewset is read-only.
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated]
    queryset = Role.objects.all()


class MembershipViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = MembershipSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        org_id = self.request.headers["X-Organization-Id"]
        return (
            Membership.objects.filter(organization_id=org_id)
            .select_related("organization", "user")
            .prefetch_related("roles")
        )

    def create(self, request, *args, **kwargs):
        require_org_capability(request, Capability.MANAGE_MEMBERSHIPS)
        require_idempotency_header(request)
        return super().create(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="roles")
    def add_role(self, request, pk=None):
        require_org_capability(request, Capability.MANAGE_MEMBERSHIPS)
        require_idempotency_header(request)
        membership = self.get_object()
        serializer = MembershipRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            assignment = assign_membership_role(
                membership, serializer.validated_data["role"]
            )
        except AuthorizationError as exc:
            raise PermissionDenied(str(exc)) from exc
        return Response(
            MembershipRoleSerializer(assignment).data, status=status.HTTP_201_CREATED
        )

    @action(
        detail=True,
        methods=["delete"],
        url_path=(
            r"roles/(?P<role_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
        ),
    )
    def remove_role(self, request, pk=None, role_id=None):
        require_org_capability(request, Capability.MANAGE_MEMBERSHIPS)
        membership = self.get_object()
        role = get_object_or_404(Role, pk=role_id)
        revoke_membership_role(membership, role)
        return Response(status=status.HTTP_204_NO_CONTENT)
