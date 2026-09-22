"""Hand a fresh database to a new customer.

A deployment starts as a copy of something: the demo data it was shown with,
the QA accounts it was tested with, last customer's orders if a template was
cloned. None of that can go to a yard. This empties everything operational
and leaves the catalogues -- which are the same for every customer and take
a long time to load -- then sets up the business and its first manager.

    manage.py new_customer --dry-run
    manage.py new_customer --shop "אלומיניום הגליל" --tax-id 514123456 \
        --manager-id 123456782 --manager-name "יוסי דהן" \
        --manager-phone 050-1234567

What survives: the catalogues (manufacturers, families, series, profiles) and
whatever integration keys live in the server environment, because those are
the deployment rather than the customer. Everything else goes -- orders,
clients, invoices, payments, deliveries, stock, warehouses, staff, shifts,
payslips, notifications.

It refuses to run without --dry-run first having been offered, asks for the
word DELETE when a terminal is attached, and will not touch a database that
has no catalogue loaded (that is a broken install, not a fresh one).
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import (Client, DeviceToken, Delivery, Document, Invoice,
                         Location, Notification, Order, OrderAttachment,
                         OrderLine, Payment, Payslip, PayslipAdjustment,
                         PriceTier, Profile, Role, Shift,
                         ShiftCorrectionRequest, Shop, StockItem,
                         StockMovement, User, Warehouse, validate_israeli_id)

# Deleted in this order, because the ledger protects the stock item it moved
# and the lines protect the order they belong to. Getting this wrong is a
# ProtectedError rather than silent damage, but the order is load-bearing.
IN_ORDER = [
    PayslipAdjustment, Payslip, ShiftCorrectionRequest, Shift,
    Payment, Document, OrderAttachment, Invoice, Delivery,
    StockMovement, OrderLine, Order, StockItem, Location, Warehouse,
    Notification, DeviceToken, PriceTier,
]

CONFIRM = 'DELETE'


class Command(BaseCommand):
    help = 'Empty a database for a new customer, keeping the catalogues.'

    def add_arguments(self, parser):
        parser.add_argument('--shop', help='The business name.')
        parser.add_argument('--tax-id', help='ח.פ. / ע.מ.')
        parser.add_argument('--manager-id', help="The first manager's ID number.")
        parser.add_argument('--manager-name', help='First and last name.')
        parser.add_argument('--manager-phone', default='')
        parser.add_argument(
            '--manager-password',
            help='Temporary password. Generated and printed when omitted.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Say what would go, change nothing.',
        )
        parser.add_argument(
            '--yes', action='store_true',
            help='Skip the typed confirmation. For scripts only.',
        )
        parser.add_argument(
            '--keep-staff', action='store_true',
            help='Leave the staff accounts alone (a rehearsal, not a handover).',
        )

    # -- what is there ------------------------------------------------------

    def _counts(self, keep_staff):
        rows = [(model.__name__, model.objects.count()) for model in IN_ORDER]
        rows.append(('Client', Client.objects.count()))
        if not keep_staff:
            rows.append(('User (except superusers)',
                         User.objects.filter(is_superuser=False).count()))
        return [(name, n) for name, n in rows if n]

    def handle(self, *args, **options):
        if not Profile.objects.exists():
            raise CommandError(
                'No catalogue in this database. That is a broken install, not '
                'a fresh one -- run `manage.py import_catalog --all` first, so '
                'this does not hand a customer an empty system.'
            )

        keep_staff = options['keep_staff']
        doomed = self._counts(keep_staff)
        catalogue = Profile.objects.count()

        self.stdout.write(self.style.MIGRATE_HEADING('\nThis database holds:'))
        if doomed:
            for name, count in doomed:
                self.stdout.write(f'  {count:6,}  {name}')
        else:
            self.stdout.write('  nothing operational -- it is already clean')
        self.stdout.write(f'\n  {catalogue:6,}  catalogue profiles (kept)')

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('\nDry run — nothing changed.'))
            return

        # Everything needed to hand over, checked before anything is deleted.
        # Half a handover is worse than none: an emptied database nobody can
        # sign in to.
        wanted = self._handover_details(options)

        if doomed and not options['yes']:
            self._confirm(doomed)

        with transaction.atomic():
            removed = self._empty(keep_staff)
            shop = self._set_up_shop(wanted)
            manager = self._create_manager(wanted)

        self.stdout.write(self.style.SUCCESS(
            f'\nCleared {removed:,} rows. {catalogue:,} catalogue profiles kept.'))
        if shop:
            self.stdout.write(f'  business : {shop.name} ({shop.tax_id})')
        if manager:
            self.stdout.write(f'  manager  : {manager.get_full_name()} '
                              f'({manager.id_number})')
            self.stdout.write(self.style.WARNING(
                f'  password : {wanted["password"]}   '
                '(they are asked to change it at first sign-in)'))
        self.stdout.write(
            '\nWorth doing next: `manage.py check_sentry --status` and the '
            'Setup page, which lists what is still unconfigured.\n')

    # -- checks -------------------------------------------------------------

    def _handover_details(self, options):
        """The shop and manager, validated before a single row is deleted."""
        details = {
            'shop': (options.get('shop') or '').strip(),
            'tax_id': (options.get('tax_id') or '').strip(),
            'id_number': (options.get('manager_id') or '').strip(),
            'name': (options.get('manager_name') or '').strip(),
            'phone': (options.get('manager_phone') or '').strip(),
            'password': options.get('manager_password'),
        }
        if details['id_number']:
            try:
                validate_israeli_id(details['id_number'])
            except Exception as exc:                          # noqa: BLE001
                raise CommandError(f'--manager-id: {exc}') from exc
            if not details['name']:
                raise CommandError('--manager-name is needed with --manager-id.')
            # An account cannot be created without one, and finding that out
            # after the database is empty is the worst possible moment.
            if not details['phone']:
                raise CommandError('--manager-phone is needed with --manager-id.')
            if User.objects.filter(id_number=details['id_number'],
                                   is_superuser=True).exists():
                raise CommandError(
                    'That ID belongs to a superuser, which this keeps. Use '
                    'another, or clear the superuser by hand.')
            if not details['password']:
                from django.utils.crypto import get_random_string
                details['password'] = get_random_string(
                    12, 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789')
        if details['tax_id'] and not details['shop']:
            raise CommandError('--tax-id without --shop.')
        return details

    def _confirm(self, doomed):
        import sys

        if not sys.stdin or not sys.stdin.isatty():
            raise CommandError(
                'This deletes data and there is no terminal to confirm at. '
                'Pass --yes if you really mean it.')
        total = sum(count for _, count in doomed)
        self.stdout.write(self.style.ERROR(
            f'\nThis permanently deletes {total:,} rows. There is no undo.'))
        typed = input(f'Type {CONFIRM} to continue: ').strip()
        if typed != CONFIRM:
            raise CommandError('Not confirmed; nothing was changed.')

    # -- doing it -----------------------------------------------------------

    def _empty(self, keep_staff):
        removed = 0
        for model in IN_ORDER:
            count, _ = model.objects.all().delete()
            removed += count
        # Client users point at the Client row, so the people go first.
        if keep_staff:
            count, _ = User.objects.filter(role=Role.CLIENT).delete()
        else:
            count, _ = User.objects.filter(is_superuser=False).delete()
        removed += count
        count, _ = Client.objects.all().delete()
        removed += count
        return removed

    def _set_up_shop(self, wanted):
        if not wanted['shop']:
            return None
        shop = Shop.get()
        shop.name = wanted['shop']
        if wanted['tax_id']:
            shop.tax_id = wanted['tax_id']
        shop.save()
        return shop

    def _create_manager(self, wanted):
        if not wanted['id_number']:
            return None
        first, _, last = wanted['name'].partition(' ')
        user = User.objects.create_user(
            id_number=wanted['id_number'],
            password=wanted['password'],
            first_name=first,
            last_name=last or first,
            phone=wanted['phone'],
            role=Role.MANAGER,
        )
        # They should not keep a password that was typed on somebody else's
        # command line.
        if hasattr(user, 'must_change_password'):
            user.must_change_password = True
            user.save(update_fields=['must_change_password'])
        return user
