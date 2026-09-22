"""Seed every manufacturer's catalogue that is not in the database yet.

The catalogue data ships with the code (core/data/<slug>_catalog_rows.json,
listed in core/data/manufacturers.json), so `migrate` on a fresh database
builds the whole catalogue, and a deploy that adds a maker loads that maker's
rows on the databases already running. A manufacturer that has profiles is
left alone -- re-import by hand with `manage.py import_catalog` when the data
changes.

New series get the starting price 0025 gave the first catalogue, for the
same reason: without a price every order line totals zero.
"""

from decimal import Decimal

from django.core.management import call_command
from django.db import migrations

STARTING_PRICE = Decimal('30.00')


def seed_catalogues(apps, schema_editor):
    import json

    from core.management.commands.import_catalog import DATA_DIR, REGISTRY_FILE

    Profile = apps.get_model('core', 'Profile')
    Series = apps.get_model('core', 'Series')
    for entry in json.loads(REGISTRY_FILE.read_text(encoding='utf-8')):
        if Profile.objects.filter(manufacturer__slug=entry['slug']).exists():
            continue
        if not (DATA_DIR / entry['file']).exists() and not entry.get('is_default'):
            continue
        try:
            call_command('import_catalog', manufacturer=entry['slug'], verbosity=0)
        except Exception as exc:  # noqa: BLE001 - never let seeding break a deploy
            print(f'[seed_catalogues] {entry["slug"]} skipped: {exc}')
    Series.objects.filter(price_per_kg__isnull=True).update(price_per_kg=STARTING_PRICE)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0039_manufacturers'),
    ]

    operations = [
        migrations.RunPython(seed_catalogues, noop),
    ]
