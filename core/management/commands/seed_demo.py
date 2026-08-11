"""Fill an empty deployment with a coherent demo dataset.

This exists so the apps can be driven end to end -- log in as a real worker,
open a real client's order, look at last month's payslip -- before any of the
actual business data exists. Everything it writes hangs together: an order's
invoice totals match that order's lines, a payslip is computed by the same
`compute_payroll` the API uses over shifts that really exist in the table.

It never touches the Klil catalog (Family / Series / Profile / SeriesProfile);
those come from the catalog import and are read here, not written.

    python manage.py seed_demo
    python manage.py seed_demo --wipe     # clear previous demo rows first

The generated data is deterministic -- the same seed produces the same names,
numbers and totals on every run, so a bug found on one machine reproduces on
another.
"""

import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import (
    Client,
    Delivery,
    Invoice,
    Location,
    MovementType,
    Order,
    OrderLine,
    PayBasis,
    Payslip,
    PayslipAdjustment,
    PriceTier,
    Profile,
    Role,
    Series,
    Shift,
    Shop,
    StockItem,
    StockMovement,
    User,
    Warehouse,
)
from core.payroll import compute_payroll

# Shared by every generated account. Staff sign in with their ID number and
# clients with their phone, both with this password.
DEMO_PASSWORD = 'Alomforce!2026'

SEED = 20260811


def make_id(prefix8):
    """Build a valid 9-digit Israeli ID by computing its check digit.

    Production runs with RELAXED_AUTH=False, so `validate_israeli_id` rejects
    any ID whose check digit is wrong -- demo accounts have to be as valid as
    real ones.
    """
    total = 0
    for index, char in enumerate(prefix8):
        digit = int(char) * (1 if index % 2 == 0 else 2)
        total += digit if digit < 10 else digit - 9
    return prefix8 + str((10 - total % 10) % 10)


# (first, last, role, pay_basis, rate) -- rate reads against pay_basis.
STAFF = [
    ('אבי', 'לוי', Role.MANAGER, PayBasis.MONTHLY, Decimal('24000')),
    ('נועה', 'בר-און', Role.OFFICE, PayBasis.MONTHLY, Decimal('14500')),
    ('רונית', 'שגב', Role.OFFICE, PayBasis.HOURLY, Decimal('62')),
    ('יוסי', 'דהן', Role.WAREHOUSE, PayBasis.HOURLY, Decimal('48')),
    ('מוחמד', 'זועבי', Role.WAREHOUSE, PayBasis.HOURLY, Decimal('52')),
    ('איתי', 'פרץ', Role.WAREHOUSE, PayBasis.DAILY, Decimal('420')),
    ('סאמר', 'חדאד', Role.DRIVER, PayBasis.DAILY, Decimal('460')),
    ('דוד', 'אזולאי', Role.DRIVER, PayBasis.HOURLY, Decimal('55')),
]

CLIENTS = [
    ('אלומיניום הגליל בע"מ', 'company', 'כרמיאל', 'יעקב מזרחי'),
    ('מסגריית שלום ובניו', 'osek_murshe', 'חיפה', 'שלום כהן'),
    ('חלונות הצפון', 'company', 'עכו', 'רami טאהא'),
    ('א.ב. פרזול ובנייה', 'osek_murshe', 'נצרת', 'אחמד ביאדסה'),
    ('סטודיו זכוכית ומתכת', 'osek_patur', 'תל אביב', 'מיכל רוזן'),
    ('בנייני קסם בע"מ', 'company', 'נהריה', 'אורן שפירא'),
    ('מסגרות הדרום', 'company', 'באר שבע', 'ניסים אלבז'),
    ('ויטרינות פלוס', 'osek_murshe', 'פתח תקווה', 'גיא לרנר'),
    ('קונסטרוקציה ירוקה', 'partnership', 'רעננה', 'תמר גולן'),
    ('שערים ומעקות אלון', 'osek_murshe', 'טבריה', 'אלון בן חיים'),
    ('מרכז הפרופיל', 'company', 'אשדוד', 'ליאור חדד'),
    ('דלתות ומזגנים סלים', 'osek_patur', 'סחנין', 'סלים ותד'),
]

EXPENSE_CATEGORIES = [
    ('חשמל', 'חברת החשמל', Decimal('2400')),
    ('דלק', 'פז חברת נפט', Decimal('1850')),
    ('שכירות מחסן', 'נכסי הצפון בע"מ', Decimal('9500')),
    ('ביטוח', 'הראל ביטוח', Decimal('3200')),
    ('תחזוקת מלגזה', 'שירותי ליפט', Decimal('1450')),
    ('כלי עבודה', 'טולס פרו', Decimal('980')),
]


class Command(BaseCommand):
    help = 'Populate the database with a realistic demo dataset.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--wipe',
            action='store_true',
            help='Delete existing operational rows before seeding. Never '
                 'touches the catalog or superusers.',
        )
        parser.add_argument(
            '--orders', type=int, default=45,
            help='How many orders to generate (default 45).',
        )
        parser.add_argument(
            '--months', type=int, default=3,
            help='How many months of shifts and payslips (default 3).',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        self.rng = random.Random(SEED)

        if not Profile.objects.exists():
            raise CommandError(
                'The Klil catalog is empty -- run `manage.py import_catalog` '
                'first, since orders are built from real profiles.'
            )

        if options['wipe']:
            self._wipe()
        elif User.objects.filter(is_superuser=False).exists():
            raise CommandError(
                'This database already holds non-superuser accounts. Re-run '
                'with --wipe if you really want to replace them.'
            )

        shop = self._shop()
        tiers = self._price_tiers()
        staff = self._staff()
        warehouse, locations = self._warehouse()
        # Stock after staff: every movement records who performed it.
        self._stock(locations, [u for u in staff if u.role == Role.WAREHOUSE])
        clients = self._clients(tiers)
        self._client_users(clients)
        orders = self._orders(clients, staff, options['orders'])
        self._deliveries(orders, staff)
        self._invoices(orders, staff)
        workers = [u for u in staff if u.role != Role.MANAGER]
        shifts = self._shifts(workers, options['months'])
        self._payslips(workers, staff[0], options['months'])

        self._report(shop, warehouse, staff, clients, orders, shifts)

    # -- teardown ---------------------------------------------------------

    def _wipe(self):
        """Delete operational data, leaving the catalog and superusers alone."""
        PayslipAdjustment.objects.all().delete()
        Payslip.objects.all().delete()
        Shift.objects.all().delete()
        Delivery.objects.all().delete()
        Invoice.objects.all().delete()
        # Movements PROTECT their stock item and reference order lines, so the
        # ledger has to be cleared before either of those.
        StockMovement.objects.all().delete()
        OrderLine.objects.all().delete()
        Order.objects.all().delete()
        StockItem.objects.all().delete()
        Location.objects.all().delete()
        Warehouse.objects.all().delete()
        # Client users point at Client, so users go first.
        User.objects.filter(is_superuser=False).delete()
        Client.objects.all().delete()
        PriceTier.objects.all().delete()
        self.stdout.write(self.style.WARNING('  wiped previous demo data'))

    # -- reference data ---------------------------------------------------

    def _shop(self):
        shop = Shop.objects.first() or Shop()
        shop.name = 'אלום פורס'
        shop.legal_name = 'אלום פורס בע"מ'
        shop.tax_id = '515482930'
        shop.address = 'האומן 12, אזור תעשייה'
        shop.city = 'כרמיאל'
        shop.phone = '04-9881230'
        shop.email = 'office@alomforce.co.il'
        shop.latitude = Decimal('32.9186')
        shop.longitude = Decimal('35.2951')
        shop.save()
        return shop

    def _price_tiers(self):
        specs = [('רגיל', '0'), ('קבוע', '4.5'), ('סיטונאי', '9')]
        return [
            PriceTier.objects.create(name=name, discount_percent=Decimal(pct))
            for name, pct in specs
        ]

    def _warehouse(self):
        warehouse = Warehouse.objects.create(
            name='מחסן ראשי — כרמיאל',
            address='האומן 12, אזור תעשייה',
            city='כרמיאל',
            latitude=Decimal('32.9186'),
            longitude=Decimal('35.2951'),
        )
        locations = [
            Location.objects.create(
                warehouse=warehouse,
                code=f'{aisle}-{bay:02d}',
                barcode=f'LOC{aisle}{bay:02d}',
                description=f'מדף {aisle} עמדה {bay}',
            )
            for aisle in 'ABCD'
            for bay in range(1, 6)
        ]
        return warehouse, locations

    def _stock(self, locations, warehouse_staff):
        """Put a spread of real catalog profiles on the shelves.

        Quantity is not a field -- it is the sum of the movement ledger -- so
        each item gets a goods-received movement, and some get a later count
        adjustment so the stock screens have something non-trivial to show.
        """
        profiles = list(Profile.objects.order_by('number')[:120])
        finishes = ['גלם', 'לבן RAL 9010', 'שחור RAL 9005', 'אנודייז טבעי']
        items = StockItem.objects.bulk_create([
            StockItem(
                profile=profile,
                location=locations[index % len(locations)],
                length_mm=self.rng.choice([5000, 6000, 6500]),
                finish=self.rng.choice(finishes),
                minimum_quantity=self.rng.choice([5, 10, 20]),
            )
            for index, profile in enumerate(profiles)
        ])

        movements = []
        for item in items:
            movements.append(StockMovement(
                stock_item=item,
                movement_type=MovementType.RECEIPT,
                quantity=self.rng.choice([8, 12, 20, 40, 60, 120]),
                performed_by=self.rng.choice(warehouse_staff),
                note='מלאי פתיחה',
            ))
            if self.rng.random() < 0.15:
                movements.append(StockMovement(
                    stock_item=item,
                    movement_type=MovementType.ADJUSTMENT,
                    quantity=self.rng.choice([-3, -2, -1, 1, 2]),
                    performed_by=self.rng.choice(warehouse_staff),
                    note='ספירת מלאי',
                ))
        StockMovement.objects.bulk_create(movements)

    # -- people -----------------------------------------------------------

    def _staff(self):
        users = []
        for index, (first, last, role, basis, rate) in enumerate(STAFF):
            id_number = make_id(f'3{index:07d}')
            fields = {
                'first_name': first,
                'last_name': last,
                'role': role,
                'phone': f'050-{7100000 + index:07d}'[:12],
                'email': f'staff{index + 1}@alomforce.co.il',
                'language': 'he',
                'hired_on': date(2023, 1, 15) + timedelta(days=index * 47),
                'pay_basis': basis,
                'overtime_enabled': basis != PayBasis.MONTHLY,
                'daily_regular_hours': Decimal('8'),
                'is_active': True,
                # Managers and office staff reach the Django admin too.
                'is_staff': role in (Role.MANAGER, Role.OFFICE),
            }
            if basis == PayBasis.HOURLY:
                fields['hourly_rate'] = rate
            elif basis == PayBasis.DAILY:
                fields['daily_rate'] = rate
            else:
                fields['monthly_salary'] = rate

            users.append(User.objects.create_user(
                id_number=id_number, password=DEMO_PASSWORD, **fields))
        return users

    def _clients(self, tiers):
        clients = []
        for index, (name, business_type, city, contact) in enumerate(CLIENTS):
            clients.append(Client.objects.create(
                name=name,
                legal_name=name,
                business_type=business_type,
                tax_id=str(510000000 + index * 7919),
                contact_name=contact,
                phone=f'04-{9200000 + index * 137:07d}'[:12],
                email=f'client{index + 1}@example.co.il',
                address=f'רחוב התעשייה {10 + index}',
                city=city,
                delivery_address=f'רחוב התעשייה {10 + index}, {city}',
                price_tier=tiers[index % len(tiers)],
                credit_limit=Decimal(self.rng.choice([20000, 50000, 80000])),
                is_active=True,
            ))
        return clients

    def _client_users(self, clients):
        """One login per client company, keyed on phone rather than an ID."""
        for index, client in enumerate(clients):
            User.objects.create_client_user(
                phone=f'052-{6100000 + index:07d}'[:12],
                password=DEMO_PASSWORD,
                first_name=client.contact_name.split(' ')[0],
                last_name=' '.join(client.contact_name.split(' ')[1:]) or 'לקוח',
                email=client.email,
                language='he',
                client=client,
            )

    # -- orders -----------------------------------------------------------

    def _orders(self, clients, staff, count):
        series = list(Series.objects.filter(is_active=True)[:20])
        creators = [u for u in staff if u.role in (Role.MANAGER, Role.OFFICE)]
        # Weighted so most orders are finished business and only a few are
        # still moving -- that is what a real quarter looks like.
        statuses = (
            ['delivered'] * 22 + ['ready'] * 5 + ['out_for_delivery'] * 4
            + ['picking'] * 4 + ['confirmed'] * 5 + ['submitted'] * 3
            + ['draft'] * 2 + ['cancelled'] * 2
        )
        now = timezone.now()
        orders = []

        for index in range(count):
            client = self.rng.choice(clients)
            status = statuses[index % len(statuses)]
            ordered_at = now - timedelta(
                days=self.rng.randint(1, 120), hours=self.rng.randint(0, 9))

            order = Order.objects.create(
                number=f'ORD-{ordered_at.year}-{index + 1:04d}',
                client=client,
                status=status,
                required_by=(ordered_at + timedelta(days=self.rng.randint(3, 21))).date(),
                notes=self.rng.choice(['', '', 'לאסוף עם מלגזה', 'דחוף — פרויקט']),
                discount_percent=(client.price_tier.discount_percent
                                  if client.price_tier else Decimal('0')),
                vat_percent=Decimal('18'),
                created_by=self.rng.choice(creators),
            )
            # ordered_at is auto_now_add, so it can only be backdated after
            # the INSERT -- otherwise every order looks like it landed today.
            Order.objects.filter(pk=order.pk).update(ordered_at=ordered_at)
            order.ordered_at = ordered_at

            self._order_lines(order, series)
            orders.append(order)
        return orders

    def _order_lines(self, order, series_pool):
        for _ in range(self.rng.randint(2, 6)):
            chosen = self.rng.choice(series_pool)
            profile = (Profile.objects.filter(series=chosen)
                       .order_by('?').first())
            if profile is None:
                continue
            length_mm = self.rng.choice([5000, 6000, 6500])
            quantity = self.rng.randint(2, 40)
            OrderLine.objects.create(
                order=order,
                profile=profile,
                series=chosen,
                length_mm=length_mm,
                quantity=quantity,
                # Bars x bar length, in metres -- the field the pricing reads.
                total_length_m=(Decimal(length_mm) * quantity / Decimal('1000')),
                price_per_kg=chosen.price_per_kg or Decimal('32.50'),
                prepared=order.status in ('ready', 'out_for_delivery', 'delivered'),
            )

    def _deliveries(self, orders, staff):
        drivers = [u for u in staff if u.role == Role.DRIVER]
        status_map = {
            'delivered': 'delivered',
            'out_for_delivery': 'en_route',
            'ready': 'loaded',
        }
        for order in orders:
            mapped = status_map.get(order.status)
            if mapped is None:
                continue
            delivered_at = (order.ordered_at + timedelta(days=self.rng.randint(2, 14))
                            if mapped == 'delivered' else None)
            Delivery.objects.create(
                order=order,
                driver=self.rng.choice(drivers),
                status=mapped,
                scheduled_for=(order.ordered_at + timedelta(days=2)).date(),
                delivered_at=delivered_at,
                address=order.client.delivery_address or order.client.address,
                recipient_name=order.client.contact_name if delivered_at else '',
            )

    # -- money ------------------------------------------------------------

    def _invoices(self, orders, staff):
        """An income invoice per delivered order, plus recurring expenses."""
        creator = next(u for u in staff if u.role == Role.OFFICE)
        seq = 0

        for order in orders:
            if order.status != 'delivered':
                continue
            subtotal = sum(
                (line.effective_weight_kg or Decimal('0')) * line.price_per_kg
                for line in order.lines.all()
            ).quantize(Decimal('0.01'))
            if order.discount_percent:
                subtotal = (subtotal * (Decimal('100') - order.discount_percent)
                            / Decimal('100')).quantize(Decimal('0.01'))
            vat = (subtotal * order.vat_percent / Decimal('100')).quantize(Decimal('0.01'))
            total = subtotal + vat
            paid = self.rng.random() < 0.75
            seq += 1

            Invoice.objects.create(
                direction='income',
                number=f'INV-{order.ordered_at.year}-{seq:04d}',
                client=order.client,
                order=order,
                party_name=order.client.name,
                party_tax_id=order.client.tax_id,
                issued_at=(order.ordered_at + timedelta(days=1)).date(),
                category='מכירות',
                subtotal=subtotal,
                vat=vat,
                total=total,
                amount_paid=total if paid else Decimal('0.00'),
                source='generated',
                status='paid' if paid else 'unpaid',
                created_by=creator,
            )

        today = timezone.localdate()
        for month_back in range(3):
            issued = (today.replace(day=1) - timedelta(days=month_back * 30)).replace(day=5)
            for category, party, base in EXPENSE_CATEGORIES:
                subtotal = (base * Decimal(self.rng.uniform(0.85, 1.15))
                            ).quantize(Decimal('0.01'))
                vat = (subtotal * Decimal('18') / Decimal('100')).quantize(Decimal('0.01'))
                Invoice.objects.create(
                    direction='expense',
                    number=f'EXP-{issued:%Y%m}-{category[:3]}',
                    party_name=party,
                    issued_at=issued,
                    category=category,
                    subtotal=subtotal,
                    vat=vat,
                    total=subtotal + vat,
                    amount_paid=subtotal + vat,
                    source='manual',
                    status='paid',
                    created_by=creator,
                )

    # -- attendance and pay -----------------------------------------------

    def _shifts(self, workers, months):
        """Weekday shifts over the recent past, Sunday-Thursday."""
        today = timezone.localdate()
        start = today - timedelta(days=months * 31)
        tz = timezone.get_current_timezone()
        created = []

        for worker in workers:
            day = start
            while day < today:
                # Israeli working week: Sunday(6) through Thursday(3).
                if day.weekday() in (4, 5) or self.rng.random() < 0.08:
                    day += timedelta(days=1)
                    continue
                start_hour = self.rng.choice([6, 7, 7, 8])
                length = self.rng.choice([8, 8, 8.5, 9, 9.5, 10])
                clock_in = timezone.make_aware(
                    datetime.combine(day, time(start_hour, self.rng.choice([0, 15, 30]))), tz)
                created.append(Shift(
                    worker=worker,
                    clock_in=clock_in,
                    clock_out=clock_in + timedelta(hours=length),
                    note='',
                ))
                day += timedelta(days=1)

        Shift.objects.bulk_create(created)
        return created

    def _payslips(self, workers, manager, months):
        """Compute finalised payslips from the shifts just created.

        Uses `compute_payroll` rather than re-deriving the numbers, so a
        payslip here matches exactly what the API would produce for the same
        worker and period.
        """
        today = timezone.localdate()
        periods = []
        cursor = today.replace(day=1)
        for _ in range(months):
            cursor = (cursor - timedelta(days=1)).replace(day=1)
            periods.append((cursor.year, cursor.month))

        for worker in workers:
            for year, month in periods:
                shifts = Shift.objects.filter(
                    worker=worker,
                    clock_in__year=year,
                    clock_in__month=month,
                    clock_out__isnull=False,
                )
                if not shifts.exists():
                    continue
                data = compute_payroll(worker, shifts)
                payslip = Payslip.objects.create(
                    worker=worker,
                    year=year,
                    month=month,
                    status='final',
                    source='generated',
                    pay_basis=data['pay_basis'],
                    overtime_enabled=data['overtime_enabled'],
                    days_worked=data['days_worked'],
                    regular_hours=Decimal(str(data['regular_hours'])),
                    overtime_125_hours=Decimal(str(data['overtime_125_hours'])),
                    overtime_150_hours=Decimal(str(data['overtime_150_hours'])),
                    hourly_rate=Decimal(str(data['hourly_rate'])),
                    base_pay=Decimal(str(data['base_pay'])),
                    overtime_pay=Decimal(str(data['overtime_pay'])),
                    created_by=manager,
                    finalised_at=timezone.now(),
                )
                PayslipAdjustment.objects.create(
                    payslip=payslip, label='נסיעות', amount=Decimal('250.00'))
                if self.rng.random() < 0.4:
                    PayslipAdjustment.objects.create(
                        payslip=payslip, label='בונוס', amount=Decimal('500.00'))
                PayslipAdjustment.objects.create(
                    payslip=payslip, label='ביטוח לאומי',
                    amount=-(Decimal(str(data['base_pay'])) * Decimal('0.07')
                             ).quantize(Decimal('0.01')))

    # -- output -----------------------------------------------------------

    def _report(self, shop, warehouse, staff, clients, orders, shifts):
        w = self.stdout.write
        w('')
        w(self.style.SUCCESS('Demo data created'))
        w(f'  shop        : {shop.name}')
        w(f'  warehouse   : {warehouse.name} '
          f'({Location.objects.count()} locations, {StockItem.objects.count()} stock items)')
        w(f'  staff       : {len(staff)}')
        w(f'  clients     : {len(clients)} (+{User.objects.filter(role=Role.CLIENT).count()} logins)')
        w(f'  orders      : {len(orders)} ({OrderLine.objects.count()} lines)')
        w(f'  deliveries  : {Delivery.objects.count()}')
        w(f'  invoices    : {Invoice.objects.filter(direction="income").count()} income, '
          f'{Invoice.objects.filter(direction="expense").count()} expense')
        w(f'  shifts      : {len(shifts)}')
        w(f'  payslips    : {Payslip.objects.count()}')
        w('')
        w(self.style.WARNING(f'  every account password: {DEMO_PASSWORD}'))
        w('')
        w('  staff sign in with the ID number, clients with the phone:')
        for user in staff:
            w(f'    {user.id_number}  {user.role:<10} {user.full_name}')
        first_client_user = User.objects.filter(role=Role.CLIENT).first()
        if first_client_user:
            w(f'    {first_client_user.phone}  client     '
              f'{first_client_user.full_name} — {first_client_user.client.name}')
        w('')
