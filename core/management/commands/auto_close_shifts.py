"""Close every shift a worker forgot to clock out of.

Run periodically (e.g. a Railway cron, hourly or nightly):

    python manage.py auto_close_shifts
    python manage.py auto_close_shifts --hours 12

Each stale shift is set to zero hours, flagged auto_closed, and gets a pending
correction request so the worker can enter the real end time.
"""

from django.core.management.base import BaseCommand

from core.attendance_auto_close import STALE_SHIFT_HOURS, close_all_stale


class Command(BaseCommand):
    help = 'Auto-close shifts left open past the stale threshold.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--hours', type=int, default=STALE_SHIFT_HOURS,
            help=f'Hours after clock-in to treat a shift as forgotten '
                 f'(default {STALE_SHIFT_HOURS}).')

    def handle(self, *args, **options):
        closed = close_all_stale(options['hours'])
        self.stdout.write(self.style.SUCCESS(
            f'Auto-closed {closed} forgotten shift(s).'))
