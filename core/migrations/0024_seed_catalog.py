"""Seed the profile catalog on any fresh database.

The catalog data ships with the code (core/data/klil_catalog_rows.json), so
running `migrate` on a brand-new database (e.g. a fresh Railway Postgres) builds
the whole catalog automatically — no manual import step. It only runs when the
Profile table is empty, so existing databases and re-deploys are untouched.
"""

from django.db import migrations


def seed_catalog(apps, schema_editor):
    """Superseded: 0040_seed_catalogues seeds every manufacturer once the
    catalogue tables have their final shape. Importing here, with the current
    models against the 0024 schema, can no longer work, so this is a no-op."""


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0023_orderline_delivered_length_m'),
    ]

    operations = [
        migrations.RunPython(seed_catalog, noop),
    ]
