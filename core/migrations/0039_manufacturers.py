"""Give the catalogue a manufacturer axis.

Until now there was one catalogue, Klil's, and family names, series codes and
profile numbers were unique on their own. Extal, Alubin, Schremer and Profal
each print their own numbering, and some of it collides with Klil's, so
uniqueness moves to (manufacturer, code). Everything already in the database
is Klil's and is filed under a Klil row created here.
"""

import django.db.models.deletion
from django.db import migrations, models

import core.models

KLIL = {
    'name': 'קליל',
    'name_en': 'Klil',
    'website': 'https://www.klil.co.il',
    'attribution': '© קליל תעשיות בע"מ',
    'is_default': True,
    'position': 1,
}


def file_under_klil(apps, schema_editor):
    Manufacturer = apps.get_model('core', 'Manufacturer')
    klil, _ = Manufacturer.objects.get_or_create(slug='klil', defaults=KLIL)
    for name in ('Family', 'Series', 'Profile'):
        model = apps.get_model('core', name)
        model.objects.filter(manufacturer__isnull=True).update(manufacturer=klil)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0038_drop_cutting_list_kind'),
    ]

    operations = [
        migrations.CreateModel(
            name='Manufacturer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slug', models.SlugField(max_length=40, unique=True, verbose_name='slug')),
                ('name', models.CharField(max_length=100, unique=True, verbose_name='name')),
                ('name_en', models.CharField(blank=True, max_length=100, verbose_name='name (English)')),
                ('website', models.URLField(blank=True, verbose_name='website')),
                ('attribution', models.CharField(blank=True, help_text='Copyright line for the catalogue data, shown wherever it is shown.', max_length=255, verbose_name='attribution')),
                ('is_default', models.BooleanField(default=False, help_text='The manufacturer a bare profile number refers to.', verbose_name='default')),
                ('is_active', models.BooleanField(default=True, verbose_name='active')),
                ('position', models.PositiveSmallIntegerField(default=0, verbose_name='position')),
            ],
            options={
                'verbose_name': 'manufacturer',
                'verbose_name_plural': 'manufacturers',
                'ordering': ['position', 'name'],
            },
        ),
        # The free-text column on Series goes; every row said "Klil".
        migrations.RemoveField(model_name='series', name='manufacturer'),
        migrations.AddField(
            model_name='family', name='manufacturer',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='families', to='core.manufacturer', verbose_name='manufacturer'),
        ),
        migrations.AddField(
            model_name='series', name='manufacturer',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='series', to='core.manufacturer', verbose_name='manufacturer'),
        ),
        migrations.AddField(
            model_name='profile', name='manufacturer',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='profiles', to='core.manufacturer', verbose_name='manufacturer'),
        ),
        migrations.RunPython(file_under_klil, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='family', name='manufacturer',
            field=models.ForeignKey(default=core.models.default_manufacturer_pk, on_delete=django.db.models.deletion.PROTECT, related_name='families', to='core.manufacturer', verbose_name='manufacturer'),
        ),
        migrations.AlterField(
            model_name='series', name='manufacturer',
            field=models.ForeignKey(default=core.models.default_manufacturer_pk, on_delete=django.db.models.deletion.PROTECT, related_name='series', to='core.manufacturer', verbose_name='manufacturer'),
        ),
        migrations.AlterField(
            model_name='profile', name='manufacturer',
            field=models.ForeignKey(default=core.models.default_manufacturer_pk, on_delete=django.db.models.deletion.PROTECT, related_name='profiles', to='core.manufacturer', verbose_name='manufacturer'),
        ),
        migrations.AddField(
            model_name='profile', name='equivalent_of',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='equivalents', to='core.profile', verbose_name='equivalent of'),
        ),
        # Uniqueness moves from the bare code to (manufacturer, code).
        migrations.AlterField(
            model_name='family', name='name',
            field=models.CharField(max_length=100, verbose_name='name'),
        ),
        migrations.AlterField(
            model_name='family', name='slug',
            field=models.SlugField(allow_unicode=True, max_length=100, verbose_name='slug'),
        ),
        migrations.AlterField(
            model_name='series', name='code',
            field=models.CharField(db_index=True, max_length=20, verbose_name='code'),
        ),
        migrations.AlterField(
            model_name='profile', name='number',
            field=models.CharField(db_index=True, max_length=20, verbose_name='profile number'),
        ),
        migrations.AddConstraint(
            model_name='family',
            constraint=models.UniqueConstraint(fields=('manufacturer', 'name'), name='unique_family_name_per_manufacturer'),
        ),
        migrations.AddConstraint(
            model_name='family',
            constraint=models.UniqueConstraint(fields=('manufacturer', 'slug'), name='unique_family_slug_per_manufacturer'),
        ),
        migrations.AddConstraint(
            model_name='series',
            constraint=models.UniqueConstraint(fields=('manufacturer', 'code'), name='unique_series_code_per_manufacturer'),
        ),
        migrations.AddConstraint(
            model_name='profile',
            constraint=models.UniqueConstraint(fields=('manufacturer', 'number'), name='unique_profile_number_per_manufacturer'),
        ),
    ]
