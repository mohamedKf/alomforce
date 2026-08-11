"""Apply the Cloudinary settings stored in the database at runtime.

Django resolves STORAGES once at import, so switching image storage from the
Settings page means (1) configuring the cloudinary SDK from the DB credentials
and (2) repointing Django's default storage. Calling apply_cloudinary_config()
does both -- at startup and again whenever a manager saves new credentials --
so uploads start (or stop) going to Cloudinary without a redeploy.
"""
from django.conf import settings


def apply_cloudinary_config():
    """Read AppConfig and point the default file storage accordingly.

    Returns the name of the active storage backend, or None if it couldn't run
    (e.g. the table doesn't exist yet during the first migrate).
    """
    try:
        from core.models import AppConfig
        cfg = AppConfig.get()
    except Exception:                                  # noqa: BLE001
        return None

    if cfg.cloudinary_ready:
        # setting() resolves env / .env first (Railway), then the DB field, so
        # this must match how cloudinary_ready decided the creds exist.
        cloud_name = cfg.setting('cloudinary_cloud_name')
        api_key = cfg.setting('cloudinary_api_key')
        api_secret = cfg.setting('cloudinary_api_secret')
        import cloudinary
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True,
        )
        settings.CLOUDINARY_STORAGE = {
            'CLOUD_NAME': cloud_name,
            'API_KEY': api_key,
            'API_SECRET': api_secret,
        }
        backend = 'cloudinary_storage.storage.MediaCloudinaryStorage'
    else:
        backend = 'django.core.files.storage.FileSystemStorage'

    settings.STORAGES['default']['BACKEND'] = backend
    _reset_default_storage()
    return backend


def _reset_default_storage():
    """Drop Django's cached storage instances so the change takes effect."""
    from django.core.files.storage import default_storage, storages
    from django.utils.functional import empty

    # StorageHandler caches instances in ._storages; clear it, then force the
    # default_storage lazy proxy to re-resolve on next use.
    try:
        storages._storages = {}
    except Exception:                                  # noqa: BLE001
        pass
    default_storage._wrapped = empty
