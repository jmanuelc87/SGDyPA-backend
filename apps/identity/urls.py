from django.urls import include, path
from rest_framework.routers import DefaultRouter

from apps.identity import views

router = DefaultRouter(trailing_slash=False)
router.register("organizations", views.OrganizationViewSet, basename="organization")
router.register("users", views.UserViewSet, basename="user")
router.register("roles", views.RoleViewSet, basename="role")
router.register("memberships", views.MembershipViewSet, basename="membership")

urlpatterns = [
    path("me", views.me, name="me"),
    path("", include(router.urls)),
]
