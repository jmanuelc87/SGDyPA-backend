from django.urls import path

from apps.platform import views
from apps.platform.views import HealthCheckView

app_name = "platform"

urlpatterns = [
    path("health-checks", HealthCheckView.as_view(), name="health-checks"),
    path(
        "platform/async-jobs",
        views.async_job_create,
        name="async-job-create",
    ),
    path(
        "platform/async-jobs/<uuid:job_id>",
        views.async_job_detail,
        name="async-job-detail",
    ),
]
