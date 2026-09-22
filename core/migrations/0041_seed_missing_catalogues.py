"""Load any manufacturer that 0040 could not.

0040 imports each catalogue inside a try/except so a bad row cannot stop a
deploy. That is right, but it means a catalogue can be quietly absent: on
MySQL the Schremer import died on a glazing thickness of 66240mm -- an OCR
misread of two catalogue numbers -- and the yard came up with four makers
instead of five, with nothing on screen to say so.

The parser now refuses impossible thicknesses, and this fills in whoever is
still missing. It asks the database rather than a flag, so it repairs an
install that half-loaded, and does nothing on one that is already whole.
"""

from decimal import Decimal

from django.core.management import call_command
from django.db import migrations

STARTING_PRICE = Decimal('30.00')


def seed_missing(apps, schema_editor):
    import json

    from core.management.commands.import_catalog import DATA_DIR, REGISTRY_FILE

    Profile = apps.get_model('core', 'Profile')
    Series = apps.get_model('core', 'Series')
    for entry in json.loads(REGISTRY_FILE.read_text(encoding='utf-8')):
        if Profile.objects.filter(manufacturer__slug=entry['slug']).exists():
            continue
        if not (DATA_DIR / entry['file']).exists():
            continue
        try:
            call_command('import_catalog', manufacturer=entry['slug'], verbosity=0)
        except Exception as exc:  # noqa: BLE001 - never let seeding break a deploy
            print(f'[seed_missing_catalogues] {entry["slug"]} skipped: {exc}')
        else:
            print(f'[seed_missing_catalogues] {entry["slug"]} loaded')
    Series.objects.filter(price_per_kg__isnull=True).update(price_per_kg=STARTING_PRICE)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0040_seed_catalogues'),
    ]

    operations = [
        migrations.RunPython(seed_missing, noop),
    ]
