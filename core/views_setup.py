"""Is this deployment actually finished?

Most of what can be wrong with a fresh install is not an error. It is a blank
field, and the thing it powers simply never happens: no SMTP host and the
accountant's monthly package is never sent, no Cloudinary and every photo the
drivers take is written to a disk the next deploy throws away. Nothing raises,
nobody is told, and it surfaces weeks later as "we never got the invoices".

So this says it out loud. Each item carries what is missing, what stops
working because of it, and where to fix it -- ordered worst first, because a
list that opens with a Mapbox token teaches people to ignore the list.

    GET /api/setup/   managers and office, since it names what is configured
                      (never a secret's value, only whether one is set)
"""

from django.utils.translation import gettext_lazy as _
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import AppConfig, Manufacturer, Profile, Shop, User
from core.views import BASE, IsOffice

# How much it matters. 'blocking' means something the business was sold on
# does not work at all; 'advised' is a real loss the yard can live with for a
# while; 'optional' is a nicety and is reported only so the list is complete.
BLOCKING, ADVISED, OPTIONAL = 'blocking', 'advised', 'optional'


def _item(key, label, ok, level, detail, consequence, where):
    return {
        'key': key,
        'label': str(label),
        'ok': bool(ok),
        'level': level,
        # Only said when it is not ok; a tick needs no explanation.
        'detail': '' if ok else str(detail),
        'consequence': '' if ok else str(consequence),
        'where': where,
    }


def build_checklist():
    """Every item, worst first. Pure, so a test can read it without a request."""
    cfg = AppConfig.get()
    shop = Shop.get()
    items = []

    items.append(_item(
        'shop', _('Business details'),
        bool(shop.name and shop.tax_id),
        BLOCKING,
        _('The business name or tax ID is missing.'),
        _('Invoices and delivery notes print without the details that make '
          'them valid paperwork.'),
        'settings',
    ))

    items.append(_item(
        'storage', _('File storage'),
        cfg.cloudinary_ready,
        BLOCKING,
        _('Cloudinary is not configured.'),
        _('Signatures, delivery photos, scanned invoices and generated PDFs '
          'are written to the server disk, which is wiped on every deploy.'),
        'settings',
    ))

    items.append(_item(
        'email', _('Email'),
        cfg.smtp_ready,
        BLOCKING,
        _('No mail server is configured.'),
        _('Invoices cannot be emailed and the accountant never receives the '
          'monthly package. Nothing warns anyone; the mail simply is not sent.'),
        'settings',
    ))

    items.append(_item(
        'accountant', _('Accountant'),
        bool(cfg.setting('accountant_email')),
        ADVISED,
        _('No accountant address.'),
        _('There is nobody to send the monthly book-keeping package to.'),
        'settings',
    ))

    items.append(_item(
        'catalog', _('Catalogue'),
        Profile.objects.exists(),
        BLOCKING,
        _('No profiles are loaded.'),
        _('Orders are built from the catalogue, so nothing can be ordered.'),
        'catalog',
    ))

    items.append(_item(
        'staff', _('Staff'),
        User.objects.filter(is_active=True).exclude(role='client').exists(),
        BLOCKING,
        _('Nobody can sign in except the administrator.'),
        _('The yard cannot clock in, pick orders or drive deliveries.'),
        'users',
    ))

    items.append(_item(
        'updates', _('Update notices'),
        bool(cfg.release_for('desktop') or cfg.release_for('android')),
        ADVISED,
        _('No current version is published.'),
        _('The apps cannot tell anyone an update exists, so a yard stays on '
          'an old build until somebody telephones.'),
        'settings',
    ))

    items.append(_item(
        'push', _('Push notifications'),
        bool(_firebase_configured()),
        ADVISED,
        _('Firebase credentials are not set.'),
        _('Phones are not woken for a new order or a delivery; the bell only '
          'fills in when the app is opened.'),
        'environment',
    ))

    items.append(_item(
        'scanning', _('Invoice scanning'),
        cfg.openai_ready,
        OPTIONAL,
        _('No OpenAI key.'),
        _('A supplier invoice has to be typed in rather than photographed.'),
        'settings',
    ))

    items.append(_item(
        'map', _('Map'),
        bool(cfg.setting('mapbox_token')),
        OPTIONAL,
        _('No Mapbox token.'),
        _('The delivery map falls back to a plainer one.'),
        'settings',
    ))

    items.append(_item(
        'invoicing', _('Legal invoicing'),
        cfg.greeninvoice_ready,
        OPTIONAL,
        _('Green Invoice is not connected.'),
        _('Invoices are records for the accountant, not legally issued tax '
          'invoices with an allocation number.'),
        'settings',
    ))

    items.append(_item(
        'registration', _('Manager sign-up code'),
        bool(cfg.setting('register_code')),
        OPTIONAL,
        _('No sign-up code.'),
        _('A new manager cannot register themselves; an administrator has to '
          'create the account.'),
        'settings',
    ))

    order = {BLOCKING: 0, ADVISED: 1, OPTIONAL: 2}
    items.sort(key=lambda i: (i['ok'], order[i['level']]))
    return items


def _firebase_configured():
    from decouple import config
    return bool(config('FIREBASE_CREDENTIALS', default=''))


class SetupView(APIView):
    """GET /api/setup/ — what is configured, and what breaks where it is not."""

    permission_classes = BASE + [IsOffice]

    def get(self, request):
        items = build_checklist()
        unresolved = [i for i in items if not i['ok']]
        return Response({
            'items': items,
            # "Ready" means nothing blocking is outstanding -- the yard can be
            # handed the keys. Advised and optional gaps do not hold that up.
            'ready': not any(i['level'] == BLOCKING for i in unresolved),
            'blocking': sum(1 for i in unresolved if i['level'] == BLOCKING),
            'advised': sum(1 for i in unresolved if i['level'] == ADVISED),
            'optional': sum(1 for i in unresolved if i['level'] == OPTIONAL),
            'catalogues': [
                {'slug': m.slug, 'name': m.name, 'profiles': m.profiles.count()}
                for m in Manufacturer.objects.filter(is_active=True)
                .order_by('position')
            ],
        })
