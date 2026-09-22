"""The catalogue with more than one manufacturer in it.

Klil's numbers were unique on their own until Extal, Alubin, Schremer and
Profal came in with their own numbering. These tests pin down what a bare
number means now, how a key (`extal:C90`) names things unambiguously, and
that the old Klil-only calls still behave as they did.
"""

from rest_framework.test import APITestCase

from core.models import (Family, Manufacturer, Profile, Role, Series,
                         SeriesProfile, User)
from core.tests import make_id


def make_maker(slug, name, **extra):
    return Manufacturer.objects.create(slug=slug, name=name, **extra)


class TwoMakersMixin:
    """Klil (seeded by the migration) plus a second maker that reuses a code.

    The second maker is invented rather than one of the real ones, since the
    seeding migration loads every catalogue file it finds and the real slugs
    are taken.
    """

    def setUp(self):
        self.klil = Manufacturer.get_default()
        self.klil_series = Series.objects.filter(manufacturer=self.klil).first()
        self.klil_profile = (SeriesProfile.objects
                             .filter(series=self.klil_series).first().profile)

        self.extal = make_maker('zz-test', 'יצרן בדיקה', name_en='Test maker', position=99)
        family = Family.objects.create(manufacturer=self.extal, name='קלאסי', slug='classic')
        # Same series code and same profile number as Klil's: the collision
        # this whole change exists for.
        self.extal_series = Series.objects.create(
            manufacturer=self.extal, code=self.klil_series.code,
            name='בדיקה קלאסי', family=family, price_per_kg=40)
        self.extal_profile = Profile.objects.create(
            manufacturer=self.extal, number=self.klil_profile.number,
            description='זקף 61x35', weight_g_per_m=965)
        SeriesProfile.objects.create(series=self.extal_series,
                                     profile=self.extal_profile, role='frame')

        self.manager = User.objects.create_user(
            id_number=make_id('66666666'), password='Str0ng!Passw0rd',
            first_name='M', last_name='K', role=Role.MANAGER, phone='050-1112233')
        self.client.force_authenticate(self.manager)


class KeysAndBareNumbersTests(TwoMakersMixin, APITestCase):

    def test_same_number_can_exist_for_two_makers(self):
        self.assertEqual(
            Profile.objects.filter(number=self.klil_profile.number).count(), 2)
        self.assertEqual(self.klil_profile.key, f'klil:{self.klil_profile.number}')
        self.assertEqual(self.extal_profile.key, f'zz-test:{self.extal_profile.number}')

    def test_bare_number_means_the_default_maker(self):
        r = self.client.get(f'/api/catalog/profiles/{self.klil_profile.number}/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['manufacturer_slug'], 'klil')
        self.assertEqual(r.data['key'], self.klil_profile.key)

    def test_key_reaches_the_other_maker(self):
        r = self.client.get(f'/api/catalog/profiles/{self.extal_profile.key}/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['manufacturer_name'], 'יצרן בדיקה')
        self.assertEqual(r.data['description'], 'זקף 61x35')

    def test_manufacturer_param_narrows_a_bare_number(self):
        r = self.client.get(
            f'/api/catalog/profiles/{self.klil_profile.number}/?manufacturer=zz-test')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['manufacturer_slug'], 'zz-test')

    def test_ambiguous_number_with_no_default_is_refused_with_candidates(self):
        Manufacturer.objects.filter(pk=self.klil.pk).update(is_default=False)
        r = self.client.get(f'/api/catalog/profiles/{self.klil_profile.number}/')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertCountEqual(r.data['candidates'],
                              [self.klil_profile.key, self.extal_profile.key])

    def test_unknown_number_is_404(self):
        r = self.client.get('/api/catalog/profiles/999999/')
        self.assertEqual(r.status_code, 404)

    def test_series_key_addresses_the_price(self):
        r = self.client.patch(f'/api/catalog/series/{self.extal_series.key}/price/',
                              {'price_per_kg': '55'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.extal_series.refresh_from_db()
        self.klil_series.refresh_from_db()
        self.assertEqual(self.extal_series.price_per_kg, 55)
        self.assertNotEqual(self.klil_series.price_per_kg, 55)

    def test_bare_series_code_still_prices_klil(self):
        r = self.client.patch(f'/api/catalog/series/{self.klil_series.code}/price/',
                              {'price_per_kg': '31'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.klil_series.refresh_from_db()
        self.assertEqual(self.klil_series.price_per_kg, 31)


class FilteringTests(TwoMakersMixin, APITestCase):

    def test_manufacturers_endpoint_lists_them_in_order_with_counts(self):
        r = self.client.get('/api/catalog/manufacturers/')
        self.assertEqual(r.status_code, 200, r.data)
        # Other makers may be seeded from core/data too; only the order of
        # these two and Extal's counts are this test's business.
        slugs = [m['slug'] for m in r.data]
        self.assertEqual(slugs[0], 'klil')
        self.assertIn('zz-test', slugs)
        self.assertLess(slugs.index('klil'), slugs.index('zz-test'))
        extal = next(m for m in r.data if m['slug'] == 'zz-test')
        self.assertEqual(extal['series_count'], 1)
        self.assertEqual(extal['profile_count'], 1)
        self.assertTrue(r.data[0]['is_default'])

    def test_listings_filter_by_manufacturer(self):
        r = self.client.get('/api/catalog/listings/?manufacturer=zz-test')
        self.assertEqual(r.status_code, 200)
        rows = r.data['results'] if isinstance(r.data, dict) else r.data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['key'], self.extal_profile.key)
        self.assertEqual(rows[0]['series_key'], self.extal_series.key)
        self.assertEqual(rows[0]['manufacturer_name'], 'יצרן בדיקה')

    def test_listings_by_bare_series_code_are_klil_only(self):
        r = self.client.get(f'/api/catalog/listings/?series={self.klil_series.code}')
        rows = r.data['results'] if isinstance(r.data, dict) else r.data
        self.assertTrue(rows)
        self.assertTrue(all(row['manufacturer_slug'] == 'klil' for row in rows))

    def test_listings_by_series_key_reach_the_other_maker(self):
        r = self.client.get(f'/api/catalog/listings/?series={self.extal_series.key}')
        rows = r.data['results'] if isinstance(r.data, dict) else r.data
        self.assertEqual([row['manufacturer_slug'] for row in rows], ['zz-test'])

    def test_series_and_families_carry_their_maker(self):
        r = self.client.get('/api/catalog/series/?manufacturer=zz-test')
        self.assertEqual([s['key'] for s in r.data], [self.extal_series.key])
        r = self.client.get('/api/catalog/families/?manufacturer=zz-test')
        self.assertEqual([f['name'] for f in r.data], ['קלאסי'])
        self.assertEqual(r.data[0]['manufacturer_slug'], 'zz-test')

    def test_inactive_maker_is_hidden_from_pickers(self):
        Manufacturer.objects.filter(pk=self.extal.pk).update(is_active=False)
        r = self.client.get('/api/catalog/manufacturers/')
        self.assertNotIn('zz-test', [m['slug'] for m in r.data])
        r = self.client.get('/api/catalog/manufacturers/?active=all')
        self.assertIn('zz-test', [m['slug'] for m in r.data])


class StockAndOrdersTests(TwoMakersMixin, APITestCase):

    def setUp(self):
        super().setUp()
        from core.models import Location, Warehouse
        warehouse = Warehouse.objects.create(name='Test WH')
        self.location = Location.objects.create(warehouse=warehouse, code='T-1')

    def test_stock_is_booked_in_by_key(self):
        r = self.client.post('/api/stock/', {
            'profile': self.extal_profile.key, 'location': self.location.id,
            'length_mm': 6000, 'finish': 'raw', 'initial_quantity': 4,
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        r = self.client.get('/api/stock/?manufacturer=zz-test')
        rows = r.data['results'] if isinstance(r.data, dict) else r.data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['key'], self.extal_profile.key)
        self.assertEqual(rows[0]['manufacturer_name'], 'יצרן בדיקה')

    def test_stock_by_bare_number_is_klil(self):
        r = self.client.post('/api/stock/', {
            'profile': self.klil_profile.number, 'location': self.location.id,
            'length_mm': 6000, 'finish': 'raw',
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        rows = self.client.get('/api/stock/').data
        rows = rows['results'] if isinstance(rows, dict) else rows
        self.assertEqual(rows[0]['manufacturer_slug'], 'klil')

    def test_stock_options_name_the_maker_once_two_are_on_the_shelves(self):
        for profile in (self.klil_profile, self.extal_profile):
            self.client.post('/api/stock/', {
                'profile': profile.key, 'location': self.location.id,
                'length_mm': 6000, 'finish': 'raw'}, format='json')
        options = self.client.get('/api/stock/options/').data
        keys = {s['key'] for s in options['series']}
        self.assertIn(self.extal_series.key, keys)
        names = [s['name'] for s in options['series'] if s['key'] == self.extal_series.key]
        self.assertTrue(names[0].startswith('יצרן בדיקה'))

    def test_unknown_key_on_stock_is_a_clear_error(self):
        r = self.client.post('/api/stock/', {
            'profile': 'zz-test:000000', 'location': self.location.id,
            'length_mm': 6000, 'finish': 'raw'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('profile', r.data)

    def test_order_line_takes_keys_and_prices_from_that_series(self):
        from core.models import Client
        client = Client.objects.create(name='Bina Ltd', phone='050-9998877')
        r = self.client.post('/api/orders/', {
            'client': client.id,
            'lines': [{'profile': self.extal_profile.key,
                       'series': self.extal_series.key, 'total_length_m': '10'}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        line = r.data['lines'][0]
        self.assertEqual(line['profile_key'], self.extal_profile.key)
        self.assertEqual(line['number'], self.extal_profile.number)
        self.assertEqual(str(line['price_per_kg']), '40.00')


class EquivalentsTests(TwoMakersMixin, APITestCase):

    def test_a_compatible_profile_names_its_twin_by_key(self):
        schremer = make_maker('zz-twin', 'יצרן תאום', position=98)
        twin = Profile.objects.create(
            manufacturer=schremer, number='4536', description='משקוף',
            weight_g_per_m=683, equivalent_of=self.klil_profile)
        r = self.client.get(f'/api/catalog/profiles/{twin.key}/')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['equivalent_of'], self.klil_profile.key)
