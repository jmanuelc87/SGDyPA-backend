from django.urls import include, path

urlpatterns = [
    path("api/v1/", include("apps.identity.urls")),
    path("api/v1/", include("apps.platform.urls")),
]

handler404 = "apps.platform.api_errors.api_not_found"
handler500 = "apps.platform.api_errors.api_server_error"
