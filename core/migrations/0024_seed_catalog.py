"""Seed the profile catalog on any fresh database.

The catalog data ships with the code (core/data/klil_catalog_rows.json), so
running `migrate` on a brand-new database (e.g. a fresh Railway Postgres) builds
the whole catalog automatically — no manual import step. It only runs when the
Profile table is empty, so existing databases and re-deploys are untouched.
"""

from django.core.management import call_command
from django.db import migrations


def seed_catalog(apps, schema_editor):
    Profile = apps.get_model('core', 'Profile')
    if Profile.objects.exists():
        return  # already populated — nothing to do
    try:
        call_command('import_catalog', verbosity=0)
    except Exception as exc:  # noqa: BLE001 — never let seeding break a deploy
        print(f'[seed_catalog] catalog import skipped: {exc}')


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_orderline_delivered_length_m'),
    ]

    operations = [
        migrations.RunPython(seed_catalog, noop),
    ]
