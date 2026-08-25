"""Deployment safety checks.

Registered in CoreConfig.ready(). These run on every `manage.py check` and
block `check --deploy`, so the testing-phase switches cannot reach production
by being forgotten.
"""

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def relaxed_auth_not_in_production(app_configs, **kwargs):
    """RELAXED_AUTH disables the ID check digit and all password rules."""
    if getattr(settings, 'RELAXED_AUTH', False) and not settings.DEBUG:
        return [
            Error(
                'RELAXED_AUTH is enabled while DEBUG is False.',
                hint=(
                    'RELAXED_AUTH removes ID check-digit validation and every '
                    'password rule, so passwords like "12345678" are accepted. '
                    'It is a testing-phase switch only. Set RELAXED_AUTH=False '
                    'before deploying.'
                ),
                id='core.E001',
            )
        ]
    return []


# The deploy checks below only run under `manage.py check --deploy`, which is
# wired into the Railway start command. Any run of that command IS production,
# so DEBUG must be off and the SECRET_KEY must not be the shipped placeholder.

INSECURE_SECRET_KEY = 'django-insecure-dev-only-change-me'


@register(Tags.security, deploy=True)
def debug_off_in_production(app_configs, **kwargs):
    """A deploy must never run with DEBUG=True (leaks tracebacks/secrets)."""
    if settings.DEBUG:
        return [
            Error(
                'DEBUG is True during a deploy check.',
                hint='Set DEBUG=False in the production environment.',
                id='core.E002',
            )
        ]
    return []


@register(Tags.security, deploy=True)
def secret_key_set_in_production(app_configs, **kwargs):
    """The signing key must be a real secret, not the committed placeholder."""
    if not settings.DEBUG and settings.SECRET_KEY == INSECURE_SECRET_KEY:
        return [
            Error(
                'SECRET_KEY is still the insecure development placeholder.',
                hint='Set a strong, unique SECRET_KEY env var in production.',
                id='core.E003',
            )
        ]
    return []
