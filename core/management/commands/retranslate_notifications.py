"""Re-render stored notifications in each recipient's language.

Notification rows keep the text as it was written, so the ones created before
notifications were translated stay in English forever -- and they are the only
ones anybody has, which makes a working translation look broken.

Every row records the kind of event and the id it came from, so the text can
be rebuilt from the same templates notify.py uses rather than string-patched.
That keeps the wording identical to a freshly sent notification, and means
this needs no updating when a message is reworded.

Rows whose source is gone, or whose kind carries something not stored on the
row (the hours in a clock-out), are left exactly as they are: a wrong
translation would be worse than an English one.
"""

from django.core.management.base import BaseCommand
from django.utils import translation

from core import notify
from core.models import Notification, Order, User


class Command(BaseCommand):
    help = "Re-render stored notifications in each recipient's language."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would change without writing anything.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        rows = list(Notification.objects.select_related('user'))
        # Cached because a single event writes one row per recipient, so the
        # same order is looked up once per person told about it.
        orders, workers = {}, {}
        changed, skipped = [], {}

        for row in rows:
            built = self._rebuild(row, orders, workers)
            if built is None:
                skipped[row.kind] = skipped.get(row.kind, 0) + 1
                continue
            title, body = built
            if (title, body) == (row.title, row.body):
                continue
            self.stdout.write(f'  {row.title!r} -> {title!r}')
            row.title, row.body = title, body
            changed.append(row)

        if changed and not dry_run:
            Notification.objects.bulk_update(changed, ['title', 'body'])

        verb = 'would rewrite' if dry_run else 'rewrote'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {len(changed)} of {len(rows)} notifications'))
        for kind, count in sorted(skipped.items()):
            self.stdout.write(f'  left alone: {count} x {kind or "(no kind)"}')

    # -- rebuilding -------------------------------------------------------

    def _rebuild(self, row, orders, workers):
        """The title and body this row would have if it were sent now."""
        data = row.data or {}
        with translation.override(row.user.language or 'en'):
            if row.kind == 'order_created':
                order = self._order(data.get('order_id'), orders)
                if order is None:
                    return None
                return str(notify._('New order')), str(notify._fmt(
                    notify._('%(number)s for %(client)s.'),
                    number=order.number, client=notify._client_of(order)))

            if row.kind == 'order_step':
                order = self._order(data.get('order_id'), orders)
                step = notify._ORDER_STEPS.get(data.get('status'))
                if order is None or step is None:
                    return None
                title, body = step[1](order)
                return str(title), str(body)

            if row.kind == 'clock_in':
                worker = self._worker(data.get('worker_id'), workers)
                if worker is None:
                    return None
                return str(notify._('Clocked in')), str(notify._fmt(
                    notify._('%(worker)s started work.'),
                    worker=worker.full_name))

        # clock_out (the hours are not on the row), delivery_signed (nor is
        # the name of whoever signed), and anything added later.
        return None

    @staticmethod
    def _order(order_id, cache):
        if order_id is None:
            return None
        key = str(order_id)
        if key not in cache:
            cache[key] = Order.objects.filter(pk=key).first()
        return cache[key]

    @staticmethod
    def _worker(worker_id, cache):
        if worker_id is None:
            return None
        key = str(worker_id)
        if key not in cache:
            cache[key] = User.objects.filter(pk=key).first()
        return cache[key]
