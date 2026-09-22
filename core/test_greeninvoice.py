"""Issuing legal invoices through Green Invoice, with the API faked.

Every HTTP exchange goes through core.greeninvoice.urlopen, so a fake that
answers by method and URL is enough to check what we send and how we treat
what comes back -- no key, no network, no sandbox account.
"""

import io
import json
import os
import tempfile
import time
from decimal import Decimal
from unittest import mock
from urllib.error import HTTPError

from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from core import greeninvoice
from core.greeninvoice import GreenInvoiceClient, GreenInvoiceError
from core.models import (Client, Invoice, Manufacturer, Order, OrderLine,
                         Profile, Role)
from core.tests import make_id, make_user

KEYS = {'GREENINVOICE_API_KEY': 'key-id', 'GREENINVOICE_API_SECRET': 'key-secret',
        'GREENINVOICE_SANDBOX': 'true'}
NO_KEYS = {'GREENINVOICE_API_KEY': '', 'GREENINVOICE_API_SECRET': ''}

PDF = b'%PDF-1.4 fake invoice'
LINKS = {'origin': 'https://files.test/origin.pdf', 'he': 'https://files.test/he.pdf'}

MEDIA = tempfile.mkdtemp(prefix='alomforce-gi-test-')
LOCAL_FILES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


class FakeResponse:
    def __init__(self, body, content_type='application/json'):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {'Content-Type': content_type}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url, code, payload):
    body = json.dumps(payload).encode()
    return HTTPError(url, code, 'error', {}, io.BytesIO(body))


class FakeAPI:
    """Stands in for urlopen. Records every call; answers like Green Invoice."""

    def __init__(self, created=None, token_ttl=1800, document=None, fail=None):
        self.calls = []
        self.tokens_issued = 0
        self.token_ttl = token_ttl
        self.created = created if created is not None else {
            'id': 'doc-uuid-1', 'number': 20001, 'url': dict(LINKS)}
        self.document = document if document is not None else {
            'id': 'doc-uuid-1', 'number': 20001, 'allocationNumber': '987654321'}
        # (method, url substring) -> callable(url) raising or returning
        self.fail = fail or {}
        self.reject_tokens = set()

    def __call__(self, request, timeout=None):
        method = request.get_method()
        url = request.full_url
        body = json.loads(request.data) if request.data else None
        headers = {k.lower(): v for k, v in request.header_items()}
        self.calls.append((method, url, body, headers))

        for (m, part), effect in self.fail.items():
            if m == method and part in url:
                raise effect

        if url.endswith('/account/token'):
            self.tokens_issued += 1
            return FakeResponse({'token': f'tok-{self.tokens_issued}',
                                 'expires': time.time() + self.token_ttl})
        auth = headers.get('authorization', '')
        if auth.replace('Bearer ', '') in self.reject_tokens:
            raise http_error(url, 401, {'errorCode': 401, 'errorMessage': 'expired'})
        if url.endswith('/documents') and method == 'POST':
            return FakeResponse(self.created)
        if url.endswith('/download/links'):
            return FakeResponse(dict(LINKS))
        if '/documents/' in url and method == 'GET':
            return FakeResponse(self.document)
        if url.startswith('https://files.test/'):
            return FakeResponse(PDF, content_type='application/pdf')
        raise AssertionError(f'unexpected call {method} {url}')

    def sent(self, method, part):
        return [c for c in self.calls if c[0] == method and part in c[1]]


def make_order(user, **extra):
    """A two-line order: 10 bars of 6 m (60 kg) and 12.5 m loose (10 kg)."""
    maker = Manufacturer.objects.create(slug='testal', name='Testal')
    frame = Profile.objects.create(manufacturer=maker, number='7101',
                                   description='frame', weight_g_per_m=1000)
    bead = Profile.objects.create(manufacturer=maker, number='7250',
                                  description='glazing bead', weight_g_per_m=800)
    buyer = Client.objects.create(
        name='Vitrina', legal_name='Vitrina Aluminium Ltd', tax_id='515123456',
        email='office@vitrina.test', address='Herzl 12', city='Haifa',
        postal_code='3100000', phone='04-8000000')
    order = Order.objects.create(number='ORD-2026-0100', client=buyer,
                                 created_by=user, vat_percent=Decimal('18.00'),
                                 **extra)
    OrderLine.objects.create(order=order, profile=frame, quantity=10, length_mm=6000,
                             total_length_m=Decimal('60'), price_per_kg=Decimal('30'))
    OrderLine.objects.create(order=order, profile=bead,
                             total_length_m=Decimal('12.5'), price_per_kg=Decimal('32'))
    return order


class TokenTests(TestCase):
    def setUp(self):
        GreenInvoiceClient.forget_tokens()

    def test_token_fetched_once_and_reused(self):
        api = FakeAPI()
        client = GreenInvoiceClient('key-id', 'key-secret', sandbox=True)
        with mock.patch('core.greeninvoice.urlopen', api):
            client.get_document('a')
            client.get_document('b')
        self.assertEqual(api.tokens_issued, 1)
        token_call = api.sent('POST', '/account/token')[0]
        self.assertEqual(token_call[2], {'id': 'key-id', 'secret': 'key-secret'})
        self.assertTrue(token_call[1].startswith(greeninvoice.SANDBOX_BASE))
        for call in api.sent('GET', '/documents/'):
            self.assertEqual(call[3]['authorization'], 'Bearer tok-1')

    def test_expired_token_is_refreshed(self):
        api = FakeAPI(token_ttl=10)          # inside the safety margin: stale at once
        client = GreenInvoiceClient('key-id', 'key-secret', sandbox=True)
        with mock.patch('core.greeninvoice.urlopen', api):
            client.get_document('a')
            client.get_document('b')
        self.assertEqual(api.tokens_issued, 2)

    def test_a_401_gets_one_fresh_token_and_a_retry(self):
        api = FakeAPI()
        api.reject_tokens = {'tok-1'}
        client = GreenInvoiceClient('key-id', 'key-secret', sandbox=True)
        with mock.patch('core.greeninvoice.urlopen', api):
            doc = client.get_document('a')
        self.assertEqual(doc['id'], 'doc-uuid-1')
        self.assertEqual(api.tokens_issued, 2)

    def test_production_base_when_not_sandbox(self):
        client = GreenInvoiceClient('k', 's', sandbox=False)
        self.assertEqual(client.base_url, greeninvoice.PRODUCTION_BASE)

    def test_api_error_carries_the_message(self):
        api = FakeAPI(fail={('POST', '/documents'): http_error(
            'x', 400, {'errorCode': 1111, 'errorMessage': 'Invalid tax ID'})})
        client = GreenInvoiceClient('key-id', 'key-secret', sandbox=True)
        with mock.patch('core.greeninvoice.urlopen', api):
            with self.assertRaises(GreenInvoiceError) as caught:
                client.create_document({'type': 305})
        self.assertIn('Invalid tax ID', str(caught.exception))
        self.assertEqual(caught.exception.code, 1111)

    def test_missing_keys_refuse_to_build(self):
        with self.assertRaises(greeninvoice.GreenInvoiceNotConfigured):
            GreenInvoiceClient('', '', sandbox=True)

    def test_from_config_reads_env(self):
        with mock.patch.dict(os.environ, KEYS):
            client = GreenInvoiceClient.from_config()
        self.assertEqual(client.api_key, 'key-id')
        self.assertTrue(client.sandbox)
        with mock.patch.dict(os.environ, dict(KEYS, GREENINVOICE_SANDBOX='false')):
            self.assertFalse(GreenInvoiceClient.from_config().sandbox)


class PayloadTests(TestCase):
    def setUp(self):
        self.office = make_user(make_id('31313131'), role=Role.OFFICE)
        self.order = make_order(self.office)

    def test_two_line_order(self):
        doc = greeninvoice.build_document(self.order, 'invoice')
        self.assertEqual(doc['type'], 305)
        self.assertEqual(doc['currency'], 'ILS')
        self.assertEqual(doc['lang'], 'he')
        self.assertEqual(doc['vatType'], 0)
        self.assertIn('ORD-2026-0100', doc['description'])
        self.assertNotIn('payment', doc)
        self.assertNotIn('discount', doc)

        client = doc['client']
        self.assertEqual(client['name'], 'Vitrina Aluminium Ltd')
        self.assertEqual(client['taxId'], '515123456')
        self.assertEqual(client['address'], 'Herzl 12')
        self.assertEqual(client['city'], 'Haifa')
        self.assertEqual(client['zip'], '3100000')
        self.assertTrue(client['add'])
        # Nobody is emailed unless the office asks.
        self.assertNotIn('emails', client)

        bars, metres = doc['income']
        self.assertEqual(bars['catalogNum'], '7101')
        self.assertIn('7101', bars['description'])
        self.assertIn('frame', bars['description'])
        self.assertEqual(bars['quantity'], 10)          # bars, as ordered
        self.assertEqual(bars['price'], 180.0)          # 60 kg x 30 / 10 bars
        self.assertEqual(bars['vatRate'], 0.18)
        self.assertEqual(bars['vatType'], 0)
        self.assertEqual(metres['quantity'], 12.5)      # metres, as ordered
        self.assertEqual(metres['price'], 25.6)         # 10 kg x 32 / 12.5 m
        # quantity x price agrees with line_total to the cent
        for row, line in zip(doc['income'], self.order.lines.order_by('id')):
            self.assertEqual(Decimal(str(row['quantity'])) * Decimal(str(row['price'])),
                             line.line_total)

    def test_email_client_when_asked(self):
        doc = greeninvoice.build_document(self.order, 'invoice', email_client=True)
        self.assertEqual(doc['client']['emails'], ['office@vitrina.test'])

    def test_invoice_receipt_carries_the_payment(self):
        doc = greeninvoice.build_document(
            self.order, 'invoice_receipt',
            payment={'type': 'cheque', 'date': '2026-09-20', 'cheque_number': '77'})
        self.assertEqual(doc['type'], 320)
        [payment] = doc['payment']
        self.assertEqual(payment['type'], 2)
        self.assertEqual(payment['date'], '2026-09-20')
        self.assertEqual(payment['chequeNum'], '77')
        self.assertEqual(Decimal(str(payment['price'])), self.order.total)

    def test_invoice_receipt_defaults_to_a_transfer_today(self):
        doc = greeninvoice.build_document(self.order, 'invoice_receipt')
        self.assertEqual(doc['payment'][0]['type'], 4)

    def test_unknown_payment_type_is_refused(self):
        with self.assertRaises(GreenInvoiceError):
            greeninvoice.build_document(self.order, 'invoice_receipt',
                                        payment={'type': 'gold'})

    def test_order_discount_becomes_a_percentage_discount(self):
        self.order.discount_percent = Decimal('5.00')
        doc = greeninvoice.build_document(self.order, 'invoice')
        self.assertEqual(doc['discount'], {'amount': 5.0, 'type': 'percentage'})

    def test_unit_price_that_does_not_divide_falls_back_to_one_unit(self):
        """100.00 over 3 bars is 33.33 x 3 = 99.99; the invoice must say 100.00."""
        line = self.order.lines.order_by('id').first()
        line.quantity, line.length_mm = 3, 6000
        line.weight_kg_override = Decimal('4')
        line.price_per_kg = Decimal('25')          # 100.00 for the line
        row = greeninvoice.build_income_line(line, 0.18)
        self.assertEqual(row['quantity'], 1)
        self.assertEqual(row['price'], 100.0)
        self.assertIn('3 bars', row['description'])


@override_settings(MEDIA_ROOT=MEDIA, STORAGES=LOCAL_FILES)
class IssueInvoiceViewTests(APITestCase):
    def setUp(self):
        GreenInvoiceClient.forget_tokens()
        self.office = make_user(make_id('32323232'), role=Role.OFFICE)
        self.order = make_order(self.office)
        self.url = f'/api/orders/{self.order.id}/issue_invoice/'
        self.client.force_authenticate(self.office)

    def _issue(self, api, body=None, env=KEYS):
        with mock.patch.dict(os.environ, env), \
                mock.patch('core.greeninvoice.urlopen', api):
            return self.client.post(self.url, body or {}, format='json')

    def test_issues_records_and_stores_the_pdf(self):
        api = FakeAPI()
        r = self._issue(api, {'kind': 'invoice'})
        self.assertEqual(r.status_code, 201, r.data)

        invoice = Invoice.objects.get()
        self.assertEqual(invoice.direction, Invoice.Direction.INCOME)
        self.assertEqual(invoice.source, Invoice.Source.GENERATED)
        self.assertEqual(invoice.number, '20001')
        self.assertEqual(invoice.external_id, 'doc-uuid-1')
        self.assertEqual(invoice.allocation_number, '987654321')
        self.assertEqual(invoice.client, self.order.client)
        self.assertEqual(invoice.order, self.order)
        self.assertEqual(invoice.party_name, 'Vitrina Aluminium Ltd')
        self.assertEqual(invoice.party_tax_id, '515123456')
        self.assertEqual(invoice.subtotal, Decimal('2120.00'))
        self.assertEqual(invoice.vat, Decimal('381.60'))
        self.assertEqual(invoice.total, Decimal('2501.60'))
        self.assertEqual(invoice.status, Invoice.Status.UNPAID)
        self.assertEqual(invoice.created_by, self.office)
        self.assertEqual(invoice.notes, LINKS['origin'])
        with invoice.file.open('rb') as fh:
            self.assertEqual(fh.read(), PDF)

        # The reply is the ordinary invoice shape the apps already render.
        self.assertEqual(r.data['number'], '20001')
        self.assertEqual(r.data['allocation_number'], '987654321')
        self.assertTrue(r.data['file_url'])

        # What went over the wire: a 305 for this order, then the PDF.
        [create] = api.sent('POST', '/documents')
        self.assertEqual(create[2]['type'], 305)
        self.assertEqual(len(create[2]['income']), 2)
        self.assertEqual(len(api.sent('GET', 'files.test')), 1)
        # The signed link is fetched bare -- no bearer to trip a file store.
        self.assertNotIn('authorization', api.sent('GET', 'files.test')[0][3])

    def test_invoice_receipt_is_paid(self):
        api = FakeAPI()
        r = self._issue(api, {'kind': 'invoice_receipt', 'payment': {'type': 'cash'}})
        self.assertEqual(r.status_code, 201, r.data)
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.status, Invoice.Status.PAID)
        self.assertEqual(invoice.amount_paid, invoice.total)
        [create] = api.sent('POST', '/documents')
        self.assertEqual(create[2]['type'], 320)
        self.assertEqual(create[2]['payment'][0]['type'], 1)

    def test_links_fetched_when_creation_reply_has_none(self):
        api = FakeAPI(created={'id': 'doc-uuid-1', 'number': 20002})
        r = self._issue(api)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(len(api.sent('GET', '/download/links')), 1)
        with Invoice.objects.get().file.open('rb') as fh:
            self.assertEqual(fh.read(), PDF)

    def test_pdf_failure_still_records_the_issued_invoice(self):
        api = FakeAPI(fail={('GET', 'files.test'): http_error('x', 500, {})})
        r = self._issue(api)
        self.assertEqual(r.status_code, 201, r.data)
        invoice = Invoice.objects.get()
        self.assertEqual(invoice.number, '20001')
        self.assertFalse(invoice.file)
        self.assertEqual(invoice.notes, LINKS['origin'])

    def test_refuses_without_keys(self):
        api = FakeAPI()
        r = self._issue(api, env=NO_KEYS)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(api.calls, [])
        self.assertEqual(Invoice.objects.count(), 0)

    def test_refuses_an_order_with_no_lines(self):
        self.order.lines.all().delete()
        r = self._issue(FakeAPI())
        self.assertEqual(r.status_code, 400)
        self.assertIn('no lines', r.data['detail'])

    def test_refuses_an_unpriced_line(self):
        line = self.order.lines.order_by('id').last()
        line.price_per_kg = Decimal('0')
        line.save()
        api = FakeAPI()
        r = self._issue(api)
        self.assertEqual(r.status_code, 400)
        self.assertIn('7250', r.data['detail'])
        self.assertEqual(api.calls, [])

    def test_refuses_a_second_generated_invoice(self):
        api = FakeAPI()
        self.assertEqual(self._issue(api).status_code, 201)
        r = self._issue(api)
        self.assertEqual(r.status_code, 400)
        self.assertIn('20001', r.data['detail'])
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(len(api.sent('POST', '/documents')), 1)

    def test_a_manual_invoice_does_not_block_issuing(self):
        Invoice.objects.create(direction='income', source='manual', number='INV-1',
                               order=self.order, issued_at='2026-09-01')
        r = self._issue(FakeAPI())
        self.assertEqual(r.status_code, 201, r.data)

    def test_unknown_kind_is_refused(self):
        r = self._issue(FakeAPI(), {'kind': 'credit_note'})
        self.assertEqual(r.status_code, 400)

    def test_api_refusal_is_a_502_with_the_reason(self):
        api = FakeAPI(fail={('POST', '/documents'): http_error(
            'x', 400, {'errorCode': 1111, 'errorMessage': 'Invalid tax ID'})})
        r = self._issue(api)
        self.assertEqual(r.status_code, 502)
        self.assertIn('Invalid tax ID', r.data['detail'])
        self.assertEqual(Invoice.objects.count(), 0)

    def test_warehouse_cannot_issue(self):
        worker = make_user(make_id('33333333'), role=Role.WAREHOUSE)
        self.client.force_authenticate(worker)
        r = self._issue(FakeAPI())
        self.assertEqual(r.status_code, 403)

    def test_missing_order_is_404(self):
        with mock.patch.dict(os.environ, KEYS):
            r = self.client.post('/api/orders/999999/issue_invoice/', {}, format='json')
        self.assertEqual(r.status_code, 404)


class InvoicingStatusTests(APITestCase):
    def setUp(self):
        self.office = make_user(make_id('34343434'), role=Role.OFFICE)
        self.client.force_authenticate(self.office)

    def test_ready_with_keys(self):
        with mock.patch.dict(os.environ, KEYS):
            r = self.client.get('/api/invoicing/status/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, {'ready': True, 'sandbox': True})

    def test_not_ready_without_keys(self):
        with mock.patch.dict(os.environ, NO_KEYS):
            r = self.client.get('/api/invoicing/status/')
        self.assertEqual(r.data['ready'], False)

    def test_production_flag(self):
        with mock.patch.dict(os.environ, dict(KEYS, GREENINVOICE_SANDBOX='false')):
            r = self.client.get('/api/invoicing/status/')
        self.assertEqual(r.data, {'ready': True, 'sandbox': False})

    def test_keys_from_settings_count_too(self):
        from core.models import AppConfig
        config = AppConfig.get()
        config.greeninvoice_api_key = 'db-key'
        config.greeninvoice_api_secret = 'db-secret'
        config.save()
        with mock.patch.dict(os.environ, NO_KEYS):
            r = self.client.get('/api/invoicing/status/')
        self.assertTrue(r.data['ready'])

    def test_workers_do_not_see_it(self):
        self.client.force_authenticate(make_user(make_id('35353535'), role=Role.WAREHOUSE))
        self.assertEqual(self.client.get('/api/invoicing/status/').status_code, 403)
