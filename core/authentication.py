"""Authentication with presence tracking.

Bumping last_seen from middleware does not work with JWT: DRF resolves the
token user on its own request wrapper during view dispatch, while the Django
request.user stays anonymous, so post-view middleware never sees who it was.
The reliable place is the authentication class, which runs exactly when the
token has been verified and the user is known.
"""

from django.utils import timezone
from rest_framework_simplejwt.authentication import JWTAuthentication

from core.models import LAST_SEEN_THROTTLE


class PresenceJWTAuthentication(JWTAuthentication):
    """JWTAuthentication that records the user as recently active.

    Cheap by construction: last_seen is rewritten only when it is older than the
    throttle, and with update_fields so it touches one column and fires no
    auto_now churn. Presence only -- not shift tracking.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            user, _token = result
            now = timezone.now()
            if user.last_seen is None or (now - user.last_seen) >= LAST_SEEN_THROTTLE:
                user.last_seen = now
                user.save(update_fields=['last_seen'])
        return result
