from django.urls import path

from apps.platform import views

urlpatterns = [
    path("async-jobs", views.async_job_create, name="platform-async-job-create"),
    path(
        "async-jobs/<uuid:job_id>",
        views.async_job_detail,
        name="platform-async-job-detail",
    ),
]
