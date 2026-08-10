from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'
    label = 'core'
    verbose_name = 'AlomForce'

    def ready(self):
        import sys

        from core import checks  # noqa: F401  (registers deployment checks)

        # Apply DB-stored Cloudinary settings when actually serving. Skipped for
        # schema commands, which run before the table exists (and where querying
        # at startup is both pointless and discouraged).
        skip = {'makemigrations', 'migrate', 'collectstatic', 'shell', 'test'}
        if not (set(sys.argv) & skip):
            from core.storage_config import apply_cloudinary_config
            apply_cloudinary_config()
