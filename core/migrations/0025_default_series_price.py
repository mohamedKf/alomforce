"""Give every unpriced series a starting price per kilo.

Without a price the order maths runs but every line totals zero: weight comes
from the profile, money comes from the series, and the deployed catalog had no
price on any of its 35 series. Orders looked broken -- correct weights, ₪0.00.

30 is a starting figure, not a fact about the metal. The shop edits prices per
series from the desktop afterwards, so this only fills the gap where nothing
was set; a series that already carries a price is left alone.
"""

from decimal import Decimal

from django.db import migrations

STARTING_PRICE = Decimal('30.00')


def set_default_price(apps, schema_editor):
    Series = apps.get_model('core', 'Series')
    Series.objects.filter(price_per_kg__isnull=True).update(
        price_per_kg=STARTING_PRICE)


def unset_default_price(apps, schema_editor):
    """Clear only the prices still sitting at the starting figure.

    A price the shop has since edited is real data and must survive a reverse;
    only the untouched default is safe to undo.
    """
    Series = apps.get_model('core', 'Series')
    Series.objects.filter(price_per_kg=STARTING_PRICE).update(price_per_kg=None)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_seed_catalog'),
    ]

    operations = [
        migrations.RunPython(set_default_price, unset_default_price),
    ]
