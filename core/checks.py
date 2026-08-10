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
