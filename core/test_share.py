"""Sending a quote or a delivery note by WhatsApp, and the app crash-report config."""

from unittest import mock

from rest_framework.test import APITestCase

from core.models import Client, Order, OrderStatus, Profile, Role, User
from core.tests import make_id


class ShareTests(APITestCase):

    def setUp(self):
        self.manager = User.objects.create_user(
            id_number=make_id('44444444'), password='Str0ng!Passw0rd',
            first_name='M', last_name='K', role=Role.MANAGER, phone='050-1112233')
        self.client.force_authenticate(self.manager)
        self.customer = Client.objects.create(name='Bina Ltd', phone='052-123 4567')
        profile = Profile.objects.filter(weight_g_per_m__isnull=False).first()
        r = self.client.post('/api/orders/', {
            'client': self.customer.id, 'status': OrderStatus.QUOTE,
            'lines': [{'profile': profile.key, 'total_length_m': '12'}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.order = Order.objects.get(pk=r.data['id'])

    def test_quote_share_gives_a_wa_me_link_to_the_client(self):
        r = self.client.get(f'/api/orders/{self.order.id}/share/?kind=quote')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['phone'], '972521234567')
        self.assertTrue(r.data['whatsapp_url'].startswith('https://wa.me/972521234567?text='))
        self.assertIn(self.order.number, r.data['message'])
        self.assertIn(r.data['public_url'], r.data['message'])
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.public_token)
        self.assertIn(f'/q/{self.order.public_token}/', r.data['public_url'])

    def test_the_public_quote_link_needs_no_login(self):
        link = self.client.get(f'/api/orders/{self.order.id}/share/?kind=quote').data['public_url']
        self.client.force_authenticate(None)
        r = self.client.get(link)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_the_link_survives_acceptance_and_serves_the_order_note(self):
        link = self.client.get(f'/api/orders/{self.order.id}/share/?kind=quote').data['public_url']
        self.assertEqual(self.client.post(f'/api/orders/{self.order.id}/accept_quote/').status_code, 200)
        self.client.force_authenticate(None)
        r = self.client.get(link)
        self.assertEqual(r.status_code, 200)
        self.assertIn('_order.pdf', r['Content-Disposition'])

    def test_no_delivery_note_share_for_a_quote(self):
        r = self.client.get(f'/api/orders/{self.order.id}/share/?kind=delivery_note')
        self.assertEqual(r.status_code, 400)

    def test_delivery_note_share_once_ready(self):
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.READY)
        r = self.client.get(f'/api/orders/{self.order.id}/share/?kind=delivery_note')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('/d/', r.data['public_url'])
        self.assertIn('Bina Ltd', r.data['message'])

    def test_a_client_without_a_phone_still_gets_a_link(self):
        Client.objects.filter(pk=self.customer.pk).update(phone='')
        r = self.client.get(f'/api/orders/{self.order.id}/share/?kind=quote')
        self.assertEqual(r.data['phone'], '')
        self.assertTrue(r.data['whatsapp_url'].startswith('https://wa.me/?text='))

    def test_warehouse_staff_cannot_share(self):
        worker = User.objects.create_user(
            id_number=make_id('33333333'), password='Str0ng!Passw0rd',
            first_name='W', last_name='K', role=Role.WAREHOUSE, phone='050-9998888')
        self.client.force_authenticate(worker)
        r = self.client.get(f'/api/orders/{self.order.id}/share/?kind=quote')
        self.assertIn(r.status_code, (403, 404))


class ClientSentryConfigTests(APITestCase):

    def test_config_carries_the_apps_dsn_when_set(self):
        user = User.objects.create_user(
            id_number=make_id('22222222'), password='Str0ng!Passw0rd',
            first_name='D', last_name='K', role=Role.DRIVER, phone='050-7776666')
        self.client.force_authenticate(user)
        with mock.patch.dict('os.environ', {'SENTRY_DSN_CLIENTS': 'https://k@o.ingest.sentry.io/1',
                                            'RAILWAY_ENVIRONMENT_NAME': 'production'}):
            r = self.client.get('/api/config/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['sentry_dsn'], 'https://k@o.ingest.sentry.io/1')
        self.assertEqual(r.data['sentry_environment'], 'production')

    def test_config_is_quiet_without_a_dsn(self):
        user = User.objects.create_user(
            id_number=make_id('22222223'), password='Str0ng!Passw0rd',
            first_name='D', last_name='K', role=Role.DRIVER, phone='050-7776667')
        self.client.force_authenticate(user)
        with mock.patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('SENTRY_DSN_CLIENTS', None)
            r = self.client.get('/api/config/')
        self.assertEqual(r.data['sentry_dsn'], '')
