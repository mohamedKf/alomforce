"""Seed demo stock so the Stock page has something to show.

Creates two warehouses (with map coordinates), a few locations each, and stock
items across a spread of profiles -- varied finishes, lengths and quantities,
including some low and some out of stock so the status colours are visible.

    python manage.py seed_stock          # add if missing
    python manage.py seed_stock --reset   # wipe stock first
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from core.models import (
    Location, MovementType, Profile, StockItem, StockMovement, User, Warehouse,
)

FINISHES = ['Anodised', 'White RAL 9010', 'Bronze', 'Mill', 'Black RAL 9005']
LENGTHS = [6000, 6500]
# Quantities cycled across items: an out-of-stock, a low one, then healthy ones.
QUANTITIES = [0, 5, 28, 64, 120, 8, 45]


class Command(BaseCommand):
    help = 'Seed demo warehouses, locations and stock items.'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true',
                            help='Delete existing stock and movements first.')

    def handle(self, *args, **options):
        if options['reset']:
            StockMovement.objects.all().delete()
            StockItem.objects.all().delete()
            self.stdout.write('Cleared existing stock.')

        wh_main, _ = Warehouse.objects.get_or_create(
            name='Main Warehouse',
            defaults={'city': 'Netanya', 'address': 'HaMelacha St 5',
                      'latitude': Decimal('32.321500'),
                      'longitude': Decimal('34.859000')})
        wh_south, _ = Warehouse.objects.get_or_create(
            name='South Depot',
            defaults={'city': 'Ashdod', 'address': 'HaMenofim 12',
                      'latitude': Decimal('31.804000'),
                      'longitude': Decimal('34.655000')})

        locations = []
        for warehouse in (wh_main, wh_south):
            for code in ('A-01', 'A-02', 'B-01'):
                loc, _ = Location.objects.get_or_create(warehouse=warehouse, code=code)
                locations.append(loc)

        user = (User.objects.filter(is_staff=True).first()
                or User.objects.filter(role='manager').first())
        if user is None:
            self.stdout.write(self.style.ERROR('No staff user to attribute movements to.'))
            return

        profiles = list(Profile.objects.order_by('number')[:70])
        made = 0
        for i, profile in enumerate(profiles):
            # One or two finishes per profile, so some profiles show in colours.
            finish_set = [FINISHES[i % len(FINISHES)]]
            if i % 3 == 0:
                finish_set.append(FINISHES[(i + 2) % len(FINISHES)])
            for j, finish in enumerate(finish_set):
                loc = locations[(i + j) % len(locations)]
                length = LENGTHS[(i + j) % len(LENGTHS)]
                item, created = StockItem.objects.get_or_create(
                    profile=profile, location=loc, length_mm=length, finish=finish,
                    defaults={'minimum_quantity': 10})
                if created:
                    made += 1
                if not item.movements.exists():
                    qty = QUANTITIES[(i + j) % len(QUANTITIES)]
                    if qty:
                        StockMovement.objects.create(
                            stock_item=item, movement_type=MovementType.RECEIPT,
                            quantity=qty, performed_by=user,
                            note='Opening stock (seed)')

        self.stdout.write(self.style.SUCCESS(
            f'Stock ready: {StockItem.objects.count()} items '
            f'({made} new) across {Warehouse.objects.count()} warehouses.'))
