from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("api/v1/platform/", include("apps.platform.urls")),
    path("admin/", admin.site.urls),
]
