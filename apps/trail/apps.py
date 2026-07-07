from django.apps import AppConfig


class TrailConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.trail"
    label = "trail"

    def ready(self) -> None:
        # Register the Organization post_save receiver that provisions per-tenant trail
        # partitions (ADR-0008).
        from apps.trail import signals  # noqa: F401
