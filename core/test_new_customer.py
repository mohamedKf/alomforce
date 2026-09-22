"""Emptying a database for a new customer.

This command deletes nearly everything, so the tests are mostly about what it
must NOT do: lose the catalogues, run without being asked twice, or leave a
database nobody can sign in to.
"""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import (AppConfig, Client, Invoice, Manufacturer, Order,
                         OrderLine, Profile, Role, Series, Shift, Shop,
                         StockItem, StockMovement, User, Warehouse, Location)
from core.tests import make_id, make_user


def a_working_yard():
    """Some of everything, as a live deployment would hold."""
    staff = make_user(id_number=make_id('24680135'), role=Role.WAREHOUSE)
    client = Client.objects.create(name='QA Client 1', phone='050-1112233')
    warehouse = Warehouse.objects.create(name='Main')
    location = Location.objects.create(warehouse=warehouse, code='A-1')
    profile = Profile.objects.filter(weight_g_per_m__isnull=False).first()
    item = StockItem.objects.create(profile=profile, location=location,
                                    length_mm=6000, finish='raw')
    StockMovement.objects.create(stock_item=item, movement_type='receipt',
                                 quantity=5, performed_by=staff)
    order = Order.objects.create(number='ORD-2026-0001', client=client,
                                created_by=staff)
    OrderLine.objects.create(order=order, profile=profile, total_length_m=10)
    Invoice.objects.create(direction='income', number='INV-1', issued_at='2026-01-01',
                           subtotal=100, vat=18, total=118, client=client)
    Shift.objects.create(worker=staff, clock_in=timezone.now())
    return {'staff': staff, 'client': client, 'order': order}


class NewCustomerTests(TestCase):

    def setUp(self):
        self.world = a_working_yard()
        self.catalogue = Profile.objects.count()
        self.series = Series.objects.count()
        self.makers = Manufacturer.objects.count()

    def run_command(self, **kwargs):
        out = StringIO()
        kwargs.setdefault('yes', True)
        call_command('new_customer', stdout=out, **kwargs)
        return out.getvalue()

    # -- the thing it must never do ----------------------------------------

    def test_the_catalogue_survives(self):
        self.run_command()
        self.assertEqual(Profile.objects.count(), self.catalogue)
        self.assertEqual(Series.objects.count(), self.series)
        self.assertEqual(Manufacturer.objects.count(), self.makers)

    def test_it_refuses_a_database_with_no_catalogue(self):
        # A broken install must not be mistaken for a fresh one.
        OrderLine.objects.all().delete()
        StockMovement.objects.all().delete()
        StockItem.objects.all().delete()
        Profile.objects.all().delete()
        with self.assertRaises(CommandError) as caught:
            self.run_command()
        self.assertIn('broken install', str(caught.exception))

    def test_a_dry_run_changes_nothing(self):
        text = self.run_command(dry_run=True)
        self.assertIn('nothing changed', text.lower())
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Client.objects.count(), 1)

    # -- what it clears -----------------------------------------------------

    def test_the_yard_is_emptied(self):
        self.run_command()
        for model in (Order, OrderLine, Invoice, StockItem, StockMovement,
                      Warehouse, Location, Shift, Client):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertFalse(User.objects.filter(is_superuser=False).exists())

    def test_a_superuser_is_kept(self):
        User.objects.create_superuser(
            id_number=make_id('13570246'), password='Str0ng!Passw0rd',
            first_name='Root', last_name='Admin', phone='050-0000000')
        self.run_command()
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)

    def test_keep_staff_leaves_the_people_but_clears_the_work(self):
        self.run_command(keep_staff=True)
        self.assertTrue(User.objects.filter(
            id_number=self.world['staff'].id_number).exists())
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Client.objects.count(), 0)

    # -- what it sets up ----------------------------------------------------

    def test_it_hands_over_a_shop_and_a_manager(self):
        manager_id = make_id('11223300')
        text = self.run_command(
            shop='אלומיניום הגליל', tax_id='514123456',
            manager_id=manager_id, manager_name='יוסי דהן',
            manager_phone='050-9998877')
        shop = Shop.get()
        self.assertEqual(shop.name, 'אלומיניום הגליל')
        self.assertEqual(shop.tax_id, '514123456')
        manager = User.objects.get(id_number=manager_id)
        self.assertEqual(manager.role, Role.MANAGER)
        self.assertTrue(manager.must_change_password)
        self.assertIn('password', text)

    def test_the_manager_can_actually_sign_in(self):
        manager_id = make_id('11223300')
        self.run_command(manager_id=manager_id, manager_name='יוסי דהן',
                         manager_phone='050-9998877',
                         manager_password='Str0ng!Passw0rd', shop='X')
        self.assertTrue(
            User.objects.get(id_number=manager_id).check_password('Str0ng!Passw0rd'))

    @override_settings(RELAXED_AUTH=False)
    def test_a_bad_id_stops_it_before_anything_is_deleted(self):
        # Only when the ID rules are on; a relaxed deployment accepts anything
        # by design, and that is its choice to make.
        with self.assertRaises(CommandError):
            self.run_command(manager_id='123', manager_name='X',
                             manager_phone='050-1111111')
        # The point: it validated first, so the yard is still there.
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Client.objects.count(), 1)

    def test_a_manager_id_without_a_name_is_refused_first(self):
        with self.assertRaises(CommandError):
            self.run_command(manager_id=make_id('11223300'))
        self.assertEqual(Order.objects.count(), 1)

    def test_a_tax_id_without_a_business_name_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_command(tax_id='514123456')
        self.assertEqual(Order.objects.count(), 1)

    def test_running_it_twice_is_harmless(self):
        self.run_command()
        self.run_command()
        self.assertEqual(Profile.objects.count(), self.catalogue)

    def test_it_will_not_delete_without_confirmation(self):
        # No terminal in a test run, so an unconfirmed call must refuse
        # rather than assume yes.
        with self.assertRaises(CommandError) as caught:
            call_command('new_customer', stdout=StringIO())
        self.assertIn('--yes', str(caught.exception))
        self.assertEqual(Order.objects.count(), 1)

    def test_a_manager_without_a_phone_is_refused_before_deleting(self):
        """An account cannot be made without one; better to hear it now."""
        with self.assertRaises(CommandError) as caught:
            self.run_command(manager_id=make_id('11223300'), manager_name='יוסי דהן')
        self.assertIn('--manager-phone', str(caught.exception))
        self.assertEqual(Order.objects.count(), 1)
