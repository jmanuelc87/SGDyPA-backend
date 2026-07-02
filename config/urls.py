from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("apps.platform.urls")),
    path("admin/", admin.site.urls),
]

handler404 = "apps.platform.api_errors.api_not_found"
handler500 = "apps.platform.api_errors.api_server_error"
