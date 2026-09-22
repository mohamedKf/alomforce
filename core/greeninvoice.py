"""Issue legal invoices through Green Invoice (חשבונית ירוקה, now "morning").

An invoice typed into AlomForce is a record for the accountant; a *legal* tax
invoice has to be produced by licensed bookkeeping software, numbered in its
sequence, signed, and -- above the Israel Invoices threshold -- stamped with a
Tax Authority allocation number. Green Invoice does all of that. This module
asks it to, and brings the PDF back so the accountant's zip still holds every
invoice in one place.

Like invoice_scan.py it is dependency-free: a handful of HTTPS calls built with
urllib rather than an SDK.

API facts this module relies on
-------------------------------
Gathered on 2026-09-22. The official docs have moved twice: the old Apiary
reference (https://greeninvoice.docs.apiary.io/) is retired and returns 404,
https://www.greeninvoice.co.il/api-docs redirects to
https://developers.morning.co/api, which is a JavaScript app that serves no
readable content to a plain fetch. The facts below therefore come from the
community reference that tracks the Morning OpenAPI spec
(https://github.com/skills-il/accounting -- green-invoice/SKILL.md and
green-invoice/references/api-reference.md), cross-checked against working
open-source clients (https://github.com/danielrosehill/GreenInvoice-MCP
src/client.ts and docs/sandbox.md; https://github.com/Jango-AI-com/morning-cli
GREENINVOICE.md) and the Green Invoice help centre
(https://www.greeninvoice.co.il/help-center/webhook-document-created/ for the
document object shape; https://www.greeninvoice.co.il/help-center/developers/
tax-auth-connect/ for allocation numbers).

Base URLs
    production  https://api.greeninvoice.co.il/api/v1
    sandbox     https://sandbox.d.greeninvoice.co.il/api/v1
  The sandbox is a separate tenancy: production keys are rejected there with
  401, so sandbox keys come from an account registered at
  https://app.sandbox.d.greeninvoice.co.il/ (Personal Area > Developer Tools >
  API Keys). Documents made in the sandbox are fake, but email delivery is not.

Token
    POST {base}/account/token   body {"id": <key id>, "secret": <key secret>}
    -> 200 {"token": "<JWT>", "expires": <unix seconds>}
  Tokens last about 30 minutes; `expires` is authoritative. Every other call
  sends `Authorization: Bearer <token>`. A 401 on any call means the token
  died early: refresh once and retry. (Morning now also documents an OAuth 2.0
  client-credentials endpoint at https://api.morning.co/idp/v1/oauth/token;
  /account/token still works and needs no new credentials, so it is used.)

Create a document
    POST {base}/documents
      type         305 tax invoice (חשבונית מס), 320 tax invoice/receipt
                   (חשבונית מס/קבלה), 400 receipt (קבלה). An osek patur cannot
                   issue 305 or 320. 320 and 400 require a `payment` list.
      date         YYYY-MM-DD        lang  "he" | "en"       currency "ILS"
      description  free text shown on the document; remarks; emailContent
      vatType      document level: 0 default, 1 exempt, 2 mixed
      rounding, signed (bool)
      discount     {"amount": n, "type": "sum" | "percentage"}
      client       {name, taxId, emails[], address, city, zip, country,
                    phone, add} -- `add: true` also files the client in
                    Green Invoice's client list. Addresses in `emails`
                    receive the document by email when it is created;
                    `emailContent` is that email's body text.
      income       [{description, quantity, price, currency, vatType,
                     vatRate, catalogNum}] -- `price` is per unit, before
                    VAT. The row-level vatType is a different enum from the
                    document one: 0 default, 1 VAT included in price,
                    2 exempt. vatRate is a fraction (0.18).
      payment      [{type, date, price, currency, ...}] -- type: 1 cash,
                    2 cheque, 3 credit card, 4 bank transfer, 5 PayPal,
                    10 payment app.
    -> 201 {"id": "<uuid>", "number": <int>, "url": {"origin", "he", "en"},
            ...}
  The document number is Green Invoice's own sequence for that type; the
  allocation number (מספר הקצאה) is the field `allocationNumber` on the
  document object, omitted when none was assigned. Green Invoice obtains it
  automatically for tax invoices above the Israel Invoices threshold (NIS
  5,000 net since 2026-06-01) provided the business has authorised the Tax
  Authority connection in its Green Invoice account (Personal Area > Tax
  Authority), an authorisation that expires every three months.

Fetch / download
    GET {base}/documents/{id}                 the full document object
    GET {base}/documents/{id}/download/links  {"he": url, "en": url,
                                               "origin": url} -- signed
                                              links to the PDF; a plain GET
                                              on one returns the file.
Errors
    Non-2xx replies carry {"errorCode": <int>, "errorMessage": "<text>"}.
    Rate limiting is unpublished; 429 is possible under load.
"""

import json
import logging
import time
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

PRODUCTION_BASE = 'https://api.greeninvoice.co.il/api/v1'
SANDBOX_BASE = 'https://sandbox.d.greeninvoice.co.il/api/v1'

TIMEOUT = 30

# Refresh the token this many seconds before Green Invoice says it expires, so
# a request never goes out on a token that dies in transit.
TOKEN_MARGIN = 60
# When the token reply carries no `expires`, assume the documented ~30 min.
TOKEN_ASSUMED_LIFETIME = 25 * 60

TAX_INVOICE = 305
TAX_INVOICE_RECEIPT = 320
RECEIPT = 400

# What the office asks for -> Green Invoice document type.
KINDS = {
    'invoice': TAX_INVOICE,
    'invoice_receipt': TAX_INVOICE_RECEIPT,
}

# How the client paid, for a tax invoice/receipt -> Green Invoice payment type.
PAYMENT_TYPES = {
    'cash': 1,
    'cheque': 2,
    'card': 3,
    'transfer': 4,
    'paypal': 5,
    'app': 10,
}
DEFAULT_PAYMENT = 'transfer'

CENT = Decimal('0.01')


class GreenInvoiceError(RuntimeError):
    """Green Invoice refused or could not be reached.

    Carries the API's own message so the office sees *why* ("client tax ID
    invalid") rather than a bare failure they cannot act on.
    """

    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


class GreenInvoiceNotConfigured(GreenInvoiceError):
    """No API key pair, so nothing can be issued."""


def is_sandbox():
    """Whether documents go to the sandbox tenancy.

    Sandbox by default: a wrong default here issues real, numbered, legally
    binding invoices, whereas the other way round only produces a fake one
    somebody notices. Switch with GREENINVOICE_SANDBOX=false once the
    production keys are in. An AppConfig field of the same name wins if the
    model ever grows one.
    """
    from decouple import config
    from core.models import AppConfig

    if any(f.name == 'greeninvoice_sandbox' for f in AppConfig._meta.fields):
        value = AppConfig.get().setting('greeninvoice_sandbox')
        if value != '':
            return str(value).strip().lower() in ('1', 'true', 'yes', 'on')
    return config('GREENINVOICE_SANDBOX', default=True, cast=bool)


def _money(value):
    return float(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP))


def _emails(client):
    return [client.email] if client and client.email else []


def build_client(client):
    """The `client` block: who the invoice is made out to.

    The registered legal name is what a tax invoice must carry; the trading
    name is a fallback for clients entered before the office asked for it.
    `add: true` files the client in Green Invoice too, so the office finds them
    there when working in that app directly.
    """
    out = {
        'name': client.legal_name or client.name,
        'taxId': client.tax_id or '',
        'country': 'IL',
        'add': True,
    }
    if client.address:
        out['address'] = client.address
    if client.city:
        out['city'] = client.city
    if client.postal_code:
        out['zip'] = client.postal_code
    if client.phone:
        out['phone'] = client.phone
    return out


def build_income_line(line, vat_rate):
    """One income row for an order line.

    Aluminium is priced by weight, but a customer reads their invoice in the
    units they ordered -- bars or metres -- so that is the quantity, with the
    unit price derived from the line total. When the derived unit price does
    not multiply back to the exact total (a total of 100.00 over 3 bars is
    33.33 x 3 = 99.99) the row is issued as one unit at the full amount, with
    the metres or bars kept in the description: a legal invoice must agree to
    the cent with the order it was raised for, and Green Invoice recomputes
    totals from quantity x price.
    """
    profile = line.profile
    total = line.line_total

    if line.quantity and line.length_mm:
        quantity = Decimal(line.quantity)
        unit = _('%(bars)d bars x %(length)s m') % {
            'bars': line.quantity,
            'length': (Decimal(line.length_mm) / 1000).normalize(),
        }
    else:
        quantity = line.total_length_m
        unit = _('%(metres)s m') % {'metres': line.total_length_m.normalize()}

    weight = line.effective_weight_kg
    pricing = _('%(weight)s kg at %(price)s/kg') % {
        'weight': weight if weight is not None else '?',
        'price': line.price_per_kg,
    }
    description = f'{profile.number} {profile.description}'.strip()
    description = f'{description} — {unit}, {pricing}'
    if line.discount_percent:
        description += ' ' + _('(%(pct)s%% off)') % {
            'pct': line.discount_percent.normalize()}

    price = (total / quantity).quantize(CENT, rounding=ROUND_HALF_UP) if quantity else total
    if quantity <= 0 or (price * quantity).quantize(CENT) != total:
        quantity, price = Decimal(1), total

    return {
        'catalogNum': profile.number,
        'description': description,
        'quantity': float(quantity),
        'price': _money(price),
        'currency': 'ILS',
        'vatType': 0,
        'vatRate': vat_rate,
    }


def build_payment(order, payment):
    """The `payment` block a tax invoice/receipt must carry.

    A receipt says money changed hands, so it has to say how. The office
    passes {type: cash|cheque|card|transfer|paypal|app, date, cheque_number};
    anything missing defaults to a bank transfer today, the usual case.
    """
    payment = payment or {}
    kind = payment.get('type') or DEFAULT_PAYMENT
    if kind not in PAYMENT_TYPES:
        raise GreenInvoiceError(
            _('Unknown payment type "%(type)s".') % {'type': kind})
    row = {
        'type': PAYMENT_TYPES[kind],
        'date': str(payment.get('date') or date.today().isoformat()),
        'price': _money(order.total),
        'currency': 'ILS',
    }
    if kind == 'cheque' and payment.get('cheque_number'):
        row['chequeNum'] = str(payment['cheque_number'])
    if kind == 'app':
        row['appType'] = 1
    return [row]


def build_document(order, kind='invoice', payment=None, email_client=False):
    """The POST /documents body for an order.

    Line discounts are already inside `line_total`; the order-wide discount is
    passed as a percentage so Green Invoice nets it before VAT exactly as
    Order.net does. VAT is sent as an explicit rate per row rather than left
    to Green Invoice's default so the invoice matches the rate snapshotted on
    the order, even if the statutory rate has moved since.
    """
    if kind not in KINDS:
        raise GreenInvoiceError(_('Unknown invoice kind "%(kind)s".') % {'kind': kind})

    vat_rate = float(order.vat_percent / 100)
    lines = list(order.lines.select_related('profile').all())
    client = build_client(order.client)
    if email_client:
        client['emails'] = _emails(order.client)

    document = {
        'type': KINDS[kind],
        'date': date.today().isoformat(),
        'lang': 'he',
        'currency': 'ILS',
        'vatType': 0,
        'rounding': False,
        'signed': True,
        'description': _('Order %(number)s') % {'number': order.number},
        'client': client,
        'income': [build_income_line(line, vat_rate) for line in lines],
    }
    if order.quoted_as:
        document['remarks'] = _('Quote %(number)s') % {'number': order.quoted_as}
    if order.discount_percent:
        document['discount'] = {'amount': float(order.discount_percent),
                                'type': 'percentage'}
    if KINDS[kind] in (TAX_INVOICE_RECEIPT, RECEIPT):
        document['payment'] = build_payment(order, payment)
    return document


class GreenInvoiceClient:
    """A thin, authenticated door to the Green Invoice API.

    Tokens are cached per key pair at class level -- a client is built per
    request, and re-authenticating on every one would double the calls and
    invite the rate limit.
    """

    _tokens = {}   # (base_url, key id) -> (token, expires-at unix seconds)

    def __init__(self, api_key, api_secret, sandbox=True):
        if not api_key or not api_secret:
            raise GreenInvoiceNotConfigured(
                _('Green Invoice is not set up: add the API key and secret in Settings.'))
        self.api_key = api_key
        self.api_secret = api_secret
        self.sandbox = sandbox
        self.base_url = SANDBOX_BASE if sandbox else PRODUCTION_BASE

    @classmethod
    def from_config(cls):
        """Build from AppConfig (env vars win over the database, as elsewhere)."""
        from core.models import AppConfig

        config = AppConfig.get()
        return cls(config.setting('greeninvoice_api_key'),
                   config.setting('greeninvoice_api_secret'),
                   sandbox=is_sandbox())

    @classmethod
    def forget_tokens(cls):
        cls._tokens.clear()

    # -- transport ---------------------------------------------------------

    @staticmethod
    def _send(request):
        """One HTTP exchange -> (status, parsed JSON or raw bytes)."""
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                content_type = response.headers.get('Content-Type', '') or ''
        except HTTPError as exc:
            body = exc.read().decode('utf-8', 'replace')
            try:
                payload = json.loads(body)
                message = payload.get('errorMessage') or body[:300]
                code = payload.get('errorCode')
            except (json.JSONDecodeError, AttributeError):
                message, code = body[:300] or exc.reason, None
            raise GreenInvoiceError(
                _('Green Invoice refused the request (%(status)s): %(message)s')
                % {'status': exc.code, 'message': message},
                code=code, status=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GreenInvoiceError(_('Could not reach Green Invoice.')) from exc

        if 'json' in content_type:
            try:
                return json.loads(raw.decode('utf-8')) if raw else {}
            except json.JSONDecodeError as exc:
                raise GreenInvoiceError(
                    _('Green Invoice sent something unreadable.')) from exc
        return raw

    def _token(self):
        key = (self.base_url, self.api_key)
        cached = self._tokens.get(key)
        if cached and cached[1] - TOKEN_MARGIN > time.time():
            return cached[0]

        body = json.dumps({'id': self.api_key, 'secret': self.api_secret}).encode('utf-8')
        request = Request(f'{self.base_url}/account/token', data=body, method='POST',
                          headers={'Content-Type': 'application/json'})
        payload = self._send(request)
        token = payload.get('token') if isinstance(payload, dict) else None
        if not token:
            raise GreenInvoiceError(_('Green Invoice did not return a token.'))
        expires = payload.get('expires')
        try:
            expires = float(expires)
        except (TypeError, ValueError):
            expires = time.time() + TOKEN_ASSUMED_LIFETIME
        self._tokens[key] = (token, expires)
        return token

    def request(self, method, path, body=None, _retry=True):
        """An authenticated call; JSON in, JSON out.

        A 401 means the token expired ahead of its stated time (or was revoked
        server-side), so it is dropped and the call made once more.
        """
        data = json.dumps(body).encode('utf-8') if body is not None else None
        request = Request(f'{self.base_url}{path}', data=data, method=method, headers={
            'Authorization': f'Bearer {self._token()}',
            'Content-Type': 'application/json',
        })
        try:
            return self._send(request)
        except GreenInvoiceError as exc:
            if exc.status == 401 and _retry:
                self._tokens.pop((self.base_url, self.api_key), None)
                return self.request(method, path, body, _retry=False)
            raise

    def download(self, url):
        """Fetch a signed download link. No bearer: the link is its own key,
        and a redirect to a file store would choke on a stray Authorization."""
        raw = self._send(Request(url, method='GET'))
        if isinstance(raw, dict):
            raise GreenInvoiceError(_('Green Invoice did not return a PDF.'))
        return raw

    # -- documents ---------------------------------------------------------

    def create_document(self, document):
        return self.request('POST', '/documents', document)

    def get_document(self, document_id):
        return self.request('GET', f'/documents/{document_id}')

    def download_links(self, document_id):
        return self.request('GET', f'/documents/{document_id}/download/links')

    def issue_tax_invoice(self, order, kind='invoice', payment=None, email_client=False):
        """Issue a tax invoice (or tax invoice/receipt) for an order.

        Returns {external_id, number, allocation_number, pdf_bytes, url}. The
        document exists at Green Invoice the moment POST /documents returns,
        so everything after that is best-effort enrichment: a failure to fetch
        the PDF is logged, not raised, and the caller still gets the number
        and the link to fetch it later. Losing the exception would otherwise
        mean issuing the same legal invoice twice.
        """
        document = build_document(order, kind, payment, email_client)
        created = self.create_document(document)
        external_id = str(created.get('id') or '')
        number = created.get('number')
        if not external_id or number in (None, ''):
            raise GreenInvoiceError(_('Green Invoice did not return a document number.'))

        result = {
            'external_id': external_id,
            'number': str(number),
            'allocation_number': str(created.get('allocationNumber') or ''),
            'pdf_bytes': b'',
            'url': '',
        }

        try:
            links = created.get('url') if isinstance(created.get('url'), dict) else None
            if not links:
                links = self.download_links(external_id)
            result['url'] = links.get('origin') or links.get('he') or links.get('en') or ''
            if not result['allocation_number']:
                # The allocation number is stamped on the stored document,
                # which the creation reply may predate.
                fetched = self.get_document(external_id)
                result['allocation_number'] = str(fetched.get('allocationNumber') or '')
            if result['url']:
                result['pdf_bytes'] = self.download(result['url'])
        except GreenInvoiceError as exc:
            logger.warning('Green Invoice document %s (no. %s) issued but not '
                           'fetched: %s', external_id, number, exc)
        return result


__all__ = [
    'GreenInvoiceClient', 'GreenInvoiceError', 'GreenInvoiceNotConfigured',
    'build_document', 'is_sandbox', 'KINDS', 'PAYMENT_TYPES',
]
