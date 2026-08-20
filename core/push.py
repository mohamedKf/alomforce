"""Send a push notification through Firebase Cloud Messaging.

Dormant until FIREBASE_CREDENTIALS is set, so the events below can be wired
into the app's flow now and start arriving the day the Firebase project
exists -- no code change, no second deploy.

Notifications are deliberately fire-and-forget. A driver's phone being off
must never fail the delivery that was just signed, so every failure here is
logged and swallowed: the record in the database is the truth, and the
notification is only a nudge towards it.

FCM's HTTP v1 API is the only one left -- the legacy server-key endpoint was
switched off in 2024 -- and it wants an OAuth2 token from a service account,
which is what google-auth handles.
"""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger('core.push')

SCOPE = 'https://www.googleapis.com/auth/firebase.messaging'
TIMEOUT = 10

# Firebase says a token is dead with one of these; the row is then removed
# rather than retried forever.
DEAD_TOKEN_CODES = {'UNREGISTERED', 'INVALID_ARGUMENT', 'NOT_FOUND'}


def _credentials():
    """Service account details from settings, or None when unconfigured."""
    from django.conf import settings

    raw = getattr(settings, 'FIREBASE_CREDENTIALS', '')
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error('FIREBASE_CREDENTIALS is not valid JSON; push is off.')
        return None


def is_configured():
    return _credentials() is not None


def _access_token(info):
    """An OAuth2 token for the messaging scope.

    google-auth caches and refreshes internally, so this is cheap to call per
    send and there is no token lifetime to manage here.
    """
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(
        info, scopes=[SCOPE])
    creds.refresh(GoogleRequest())
    return creds.token


def _send_one(project_id, access_token, token, title, body, data):
    url = f'https://fcm.googleapis.com/v1/projects/{project_id}/messages:send'
    payload = {
        'message': {
            'token': token,
            'notification': {'title': title, 'body': body},
            # Strings only: FCM rejects a data payload with non-string values,
            # and an int slipped in here is an easy mistake to make.
            'data': {k: str(v) for k, v in (data or {}).items()},
            'android': {'priority': 'high'},
            'apns': {'headers': {'apns-priority': '10'}},
        }
    }
    request = Request(
        url, data=json.dumps(payload).encode('utf-8'), method='POST',
        headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        })
    with urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode('utf-8'))


def send_to_users(users, title, body, data=None):
    """Notify every device belonging to `users`. Returns how many were sent.

    Never raises. A push that fails is a nudge that did not arrive, and the
    delivery, order or approval it referred to has already been recorded.
    """
    from core.models import DeviceToken

    info = _credentials()
    if info is None:
        return 0

    user_ids = [u.id if hasattr(u, 'id') else u for u in users]
    tokens = list(DeviceToken.objects.filter(user_id__in=user_ids)
                  .values_list('token', flat=True))
    if not tokens:
        return 0

    try:
        access_token = _access_token(info)
    except Exception:                                     # noqa: BLE001
        logger.exception('Could not get a Firebase access token; push skipped.')
        return 0

    project_id = info.get('project_id')
    sent, dead = 0, []
    for token in tokens:
        try:
            _send_one(project_id, access_token, token, title, body, data)
            sent += 1
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')
            if any(code in detail for code in DEAD_TOKEN_CODES):
                # The app was reinstalled or removed; stop addressing it.
                dead.append(token)
            else:
                logger.warning('Push to a device failed (%s): %s',
                               exc.code, detail[:200])
        except (URLError, TimeoutError):
            logger.warning('Push could not reach Firebase; skipped one device.')
        except Exception:                                 # noqa: BLE001
            logger.exception('Unexpected failure sending a push.')

    if dead:
        DeviceToken.objects.filter(token__in=dead).delete()
        logger.info('Removed %d dead push tokens.', len(dead))
    return sent


def send_to_roles(roles, title, body, data=None, exclude=None):
    """Notify everyone holding one of `roles` -- 'the drivers', 'the office'."""
    from core.models import User

    users = User.objects.filter(role__in=roles, is_active=True)
    if exclude is not None:
        users = users.exclude(pk=getattr(exclude, 'pk', exclude))
    return send_to_users(list(users), title, body, data)
