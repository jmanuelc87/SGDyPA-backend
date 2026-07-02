from django.urls import path

from apps.platform.views import HealthCheckView

app_name = "platform"

urlpatterns = [
    path("health-checks", HealthCheckView.as_view(), name="health-checks"),
]
