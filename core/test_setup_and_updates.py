"""Telling the office what is not finished, and the apps that they are behind."""

from unittest import mock

from rest_framework.test import APITestCase

from core.models import AppConfig, Role, Shop, User
from core.tests import make_id
from core.views_setup import BLOCKING, build_checklist


def configure(**fields):
    """Set AppConfig fields. get() first: the row is created on demand, and
    a queryset update against an empty table quietly changes nothing."""
    cfg = AppConfig.get()
    for key, value in fields.items():
        setattr(cfg, key, value)
    cfg.save()
    return cfg


def configure_shop(**fields):
    shop = Shop.get()
    for key, value in fields.items():
        setattr(shop, key, value)
    shop.save()
    return shop


def item(items, key):
    return next(i for i in items if i['key'] == key)


class SetupChecklistTests(APITestCase):

    def setUp(self):
        self.manager = User.objects.create_user(
            id_number=make_id('55555555'), password='Str0ng!Passw0rd',
            first_name='M', last_name='K', role=Role.MANAGER, phone='050-1234567')
        self.client.force_authenticate(self.manager)

    def test_a_bare_deployment_is_not_ready_and_says_why(self):
        r = self.client.get('/api/setup/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(r.data['ready'])
        email = item(r.data['items'], 'email')
        self.assertFalse(email['ok'])
        self.assertEqual(email['level'], BLOCKING)
        # The point of the screen: what stops working, not just what is blank.
        self.assertIn('accountant', email['consequence'])

    def test_unfinished_things_are_listed_before_finished_ones(self):
        r = self.client.get('/api/setup/')
        oks = [i['ok'] for i in r.data['items']]
        self.assertEqual(oks, sorted(oks), 'outstanding items must come first')

    def test_a_tick_carries_no_scolding(self):
        configure_shop(name='Alum Force', tax_id='514999999')
        r = self.client.get('/api/setup/')
        shop = item(r.data['items'], 'shop')
        self.assertTrue(shop['ok'])
        self.assertEqual(shop['detail'], '')
        self.assertEqual(shop['consequence'], '')

    def test_configuring_everything_blocking_makes_it_ready(self):
        configure_shop(name='Alum Force', tax_id='514999999')
        configure(cloudinary_cloud_name='c', cloudinary_api_key='k',
                  cloudinary_api_secret='s', smtp_host='smtp.example.com',
                  smtp_from='office@example.com')
        r = self.client.get('/api/setup/')
        self.assertTrue(r.data['ready'], [i for i in r.data['items'] if not i['ok']])
        self.assertEqual(r.data['blocking'], 0)

    def test_it_never_reveals_a_secret(self):
        configure(smtp_password='hunter2', openai_api_key='sk-secret')
        body = str(self.client.get('/api/setup/').data)
        self.assertNotIn('hunter2', body)
        self.assertNotIn('sk-secret', body)

    def test_the_catalogues_are_named_with_their_size(self):
        r = self.client.get('/api/setup/')
        slugs = {c['slug']: c['profiles'] for c in r.data['catalogues']}
        self.assertIn('klil', slugs)
        self.assertGreater(slugs['klil'], 0)

    def test_a_driver_cannot_read_it(self):
        driver = User.objects.create_user(
            id_number=make_id('99999999'), password='Str0ng!Passw0rd',
            first_name='D', last_name='K', role=Role.DRIVER, phone='050-7654321')
        self.client.force_authenticate(driver)
        self.assertIn(self.client.get('/api/setup/').status_code, (403, 404))

    def test_the_checklist_reads_without_a_request(self):
        self.assertTrue(any(i['key'] == 'storage' for i in build_checklist()))


class UpdateNoticeTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            id_number=make_id('12121212'), password='Str0ng!Passw0rd',
            first_name='W', last_name='K', role=Role.WAREHOUSE, phone='050-2223344')
        self.client.force_authenticate(self.user)

    def test_nothing_published_means_nothing_claimed(self):
        r = self.client.get('/api/config/')
        self.assertIsNone(r.data['update']['desktop'])
        self.assertIsNone(r.data['update']['android'])

    def test_a_published_desktop_release_reaches_the_app(self):
        configure(latest_desktop_version='1.2.0',
                  latest_desktop_url='https://example.com/AlomForce-Setup-1.2.0.exe',
                  release_notes='Catalogues for every maker.')
        r = self.client.get('/api/config/')
        desktop = r.data['update']['desktop']
        self.assertEqual(desktop['version'], '1.2.0')
        self.assertIn('AlomForce-Setup', desktop['url'])
        self.assertEqual(desktop['notes'], 'Catalogues for every maker.')

    def test_the_phone_is_compared_by_build_number(self):
        configure(latest_android_version='1.1.0', latest_android_build=13)
        android = self.client.get('/api/config/').data['update']['android']
        self.assertEqual(android['build'], 13)
        self.assertEqual(android['version'], '1.1.0')

    def test_a_build_number_that_is_not_a_number_is_ignored(self):
        configure(latest_android_version='1.1.0', latest_android_build=None)
        with mock.patch.object(AppConfig, 'setting',
                               side_effect=lambda f: 'nonsense' if f == 'latest_android_build'
                               else ('1.1.0' if f == 'latest_android_version' else '')):
            android = AppConfig.get().release_for('android')
        self.assertIsNone(android['build'])

    def test_the_environment_can_publish_the_release_instead(self):
        import os
        with mock.patch.dict(os.environ, {'LATEST_DESKTOP_VERSION': '2.0.0',
                                          'LATEST_DESKTOP_URL': 'https://example.com/x.exe'}):
            desktop = self.client.get('/api/config/').data['update']['desktop']
        self.assertEqual(desktop['version'], '2.0.0')
