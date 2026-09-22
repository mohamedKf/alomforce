"""Import a manufacturer's profile catalogue from its JSON rows.

Each manufacturer has its own catalogue file in core/data (see
manufacturers.json for the registry: name, attribution, file), all in the
same row shape the Klil import has always used:

    {"profile_number": "05980", "description": "משקוף", "weight_g_per_m": 338,
     "series": "1300", "family": "בלגי", "group": null, "page": 4,
     "section_image": "catalog/sections/05980",
     "equivalent_number": "04573", "equivalent_manufacturer": "klil"}

The Hebrew group header and description are turned into structured role /
glazing range / track count so the apps can filter on them ("show me every
3-track rail that takes 16mm glass"). The last two keys are optional: they
link a compatible extrusion in another maker's catalogue.

    manage.py import_catalog                      # Klil, as before
    manage.py import_catalog --manufacturer extal
    manage.py import_catalog --all                # every registry entry with a file
    manage.py import_catalog --manufacturer alubin --file /tmp/rows.json --dry-run
"""

import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from core.models import (Family, Manufacturer, Profile, ProfileRole, Series,
                         SeriesProfile)

# commands/ -> management/ -> core/
DATA_DIR = Path(__file__).resolve().parents[2] / 'data'
REGISTRY_FILE = DATA_DIR / 'manufacturers.json'
GENERAL = 'כללי'


def load_registry():
    """The manufacturers we know how to import, keyed by slug, in order."""
    entries = json.loads(REGISTRY_FILE.read_text(encoding='utf-8'))
    return {entry['slug']: entry for entry in entries}


def load_catalog_rows(entry, file_override=None):
    """Return (rows, source_label) for one manufacturer.

    An explicit --file wins. Otherwise CATALOG_URL_<SLUG> (or CATALOG_URL for
    the default manufacturer, as Railway has always set it) points at the JSON
    in Cloudinary; if it is unset or unreachable, the bundled file is used.
    """
    from decouple import config

    if file_override:
        path = Path(file_override)
        return json.loads(path.read_text(encoding='utf-8')), str(path)

    slug = entry['slug'].upper()
    url = config(f'CATALOG_URL_{slug}', default=None)
    if url is None and entry.get('is_default'):
        url = config('CATALOG_URL', default=entry.get('default_url'))
    if url:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=30) as resp:
                rows = json.loads(resp.read().decode('utf-8'))
            return rows, url
        except Exception as exc:  # noqa: BLE001 - fall back to the bundled file
            print(f'catalog: could not fetch {url} ({exc}); using local file')

    path = DATA_DIR / entry['file']
    if not path.exists():
        raise CommandError(f'No catalogue file for {entry["slug"]}: {path}')
    return json.loads(path.read_text(encoding='utf-8')), path.name


# --- Role detection ---------------------------------------------------------
#
# Ordered most-specific first: "סרגלי זיגוג לכנף" is a glazing bead, not a sash,
# so the bead rule has to win over the sash rule.

ROLE_PATTERNS = [
    (ProfileRole.GLAZING_BEAD, ['סרגל זיגוג', 'סרגלי זיגוג']),
    (ProfileRole.TRACK, ['מסילה', 'מסילות', 'מסילת']),
    (ProfileRole.SASH, ['כנפיים', 'כנף', 'מג׳יקליל', "מג'יקליל"]),
    (ProfileRole.MULLION, ['חציץ']),
    (ProfileRole.POST, ['עמודים', 'עמוד']),
    (ProfileRole.BEAM, ['קורות', 'קורה']),
    (ProfileRole.FRAME, ['משקוף']),
    (ProfileRole.ADAPTER, ['מעבר לקליל', 'מתאם', 'מעבר']),
    (ProfileRole.TRIM, ['הלבשה', 'הלבשות']),
    (ProfileRole.SHUTTER, ['שלבי גלילה', 'ארגז תריס', 'ארגזי תריס', 'מונובלוק',
                           'תריס', 'שלב']),
    (ProfileRole.SEAL, ['אפי שור', 'אף שור', 'זויות גומי', 'גומי']),
    (ProfileRole.ACCESSORY, ['פרופילי עזר', 'שרוולים', 'שרוול', 'פינות חיבור',
                             'מכסים', 'לחצנים', 'מכסה', 'סגר', 'מוט מוביל',
                             'מרחיק', 'מילוי', 'בורג']),
]


# Some sections are a product category in their own right, so the series says
# what the profile is even when the row is only a dimension pair ("62 x 32").
# Applied last, as a fallback after header and description.
SERIES_ROLE = {
    'הלבשות': ProfileRole.TRIM,
    'תריס גלילה': ProfileRole.SHUTTER,
    'תריס הזזה': ProfileRole.SHUTTER,
    'מונובלוקים': ProfileRole.SHUTTER,
    'פירנצה': ProfileRole.SHUTTER,
    'רשתות': ProfileRole.MESH,
    'זויות גומי': ProfileRole.SEAL,
    'פנלים לריצוף': ProfileRole.PANEL,
    'פנלים לחלונות כיס': ProfileRole.PANEL,
    'הצללה': ProfileRole.PANEL,
    'מסתורי כביסה': ProfileRole.PANEL,
    '3100': ProfileRole.RAILING,   # קליל 3100 – מעקים
}


def detect_role(group_header, description, series=None):
    """Work out what a profile does.

    Three signals, strongest first:
      1. group header  -- how the catalog itself organises the page
      2. description   -- the fallback for the 414 rows under no header
      3. series        -- for sections that are a category in themselves
    """
    for source in (group_header or '', description or ''):
        for role, needles in ROLE_PATTERNS:
            if any(needle in source for needle in needles):
                return role

    if series is not None and str(series) in SERIES_ROLE:
        return SERIES_ROLE[str(series)]

    return ProfileRole.OTHER


# --- Glazing range ----------------------------------------------------------

RE_GLASS_RANGE = re.compile(r'זיגוג\s*(\d+(?:\.\d+)?)\s*[÷\-]\s*(\d+(?:\.\d+)?)')
RE_GLASS_UPTO = re.compile(r'זיגוג\s*עד\s*(\d+(?:\.\d+)?)')
RE_GLASS_LIST = re.compile(r'זיגוג\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)')
RE_BARE_RANGE = re.compile(r'(\d+(?:\.\d+)?)\s*÷\s*(\d+(?:\.\d+)?)\s*מ["״]?מ')


def parse_glass(group_header, description):
    """Return (min_mm, max_mm). Either may be None.

    Handles the four notations the catalog uses:
        זיגוג עד 8 מ"מ      -> (None, 8)     a ceiling, no minimum
        זיגוג 11÷16 מ"מ     -> (11, 16)      a range
        זיגוג 14,10 מ"מ     -> (10, 14)      two discrete thicknesses
        6÷11 מ"מ            -> (6, 11)       range without the word זיגוג
    """
    for text in (group_header or '', description or ''):
        if not text:
            continue

        match = RE_GLASS_RANGE.search(text)
        if match:
            return Decimal(match.group(1)), Decimal(match.group(2))

        match = RE_GLASS_LIST.search(text)
        if match:
            values = sorted([Decimal(match.group(1)), Decimal(match.group(2))])
            return values[0], values[1]

        match = RE_GLASS_UPTO.search(text)
        if match:
            return None, Decimal(match.group(1))

        if 'זיגוג' in text:
            match = RE_BARE_RANGE.search(text)
            if match:
                return Decimal(match.group(1)), Decimal(match.group(2))

    return None, None


# --- Track count ------------------------------------------------------------

HEBREW_ONE = ['נתיב אחד', 'נתיב יחיד']
RE_TRACKS = re.compile(r'(\d)\s*נתיבים')


def parse_tracks(group_header, description):
    for text in (group_header or '', description or ''):
        if not text:
            continue
        if any(phrase in text for phrase in HEBREW_ONE):
            return 1
        match = RE_TRACKS.search(text)
        if match:
            return int(match.group(1))
    return None


class Command(BaseCommand):
    help = "Import a manufacturer's profile catalogue into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            '--manufacturer', default=None,
            help='Registry slug (klil, extal, alubin, schremer, profal). '
                 'Defaults to the default manufacturer.',
        )
        parser.add_argument(
            '--file', default=None,
            help='Read rows from this JSON file instead of the registry source.',
        )
        parser.add_argument(
            '--all', action='store_true',
            help='Import every manufacturer whose catalogue file is present.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Parse and report without writing anything.',
        )
        parser.add_argument(
            '--show', type=int, default=0,
            help='Print N sample parsed rows per role.',
        )

    def handle(self, *args, **options):
        registry = load_registry()
        if options['all']:
            slugs = [slug for slug, entry in registry.items()
                     if (DATA_DIR / entry['file']).exists()]
        else:
            slug = options['manufacturer'] or next(
                (s for s, e in registry.items() if e.get('is_default')), 'klil')
            if slug not in registry:
                raise CommandError(
                    f'Unknown manufacturer {slug!r}; known: {", ".join(registry)}')
            slugs = [slug]

        for slug in slugs:
            self.stdout.write(self.style.MIGRATE_HEADING(f'\n== {slug} =='))
            self._import_one(registry[slug], options)

    def _import_one(self, entry, options):
        rows, source = load_catalog_rows(entry, options['file'])
        self.stdout.write(f'Loaded {len(rows)} rows from {source}')

        parsed = []
        for row in rows:
            if row.get('placeholder'):
                continue  # an ERP dummy the maker's site lists but does not sell
            group = row.get('group')
            description = row.get('description') or ''
            glass_min, glass_max = parse_glass(group, description)
            # A catalogue that files some profiles straight under a family, or
            # under nothing at all, still needs a series for the listing: the
            # family stands in, and "כללי" (general) when there is no family.
            family = row.get('family') or GENERAL
            parsed.append({
                **row,
                'family': family,
                'series': row.get('series') or family,
                'profile_number': str(row['profile_number']),
                'role': detect_role(group, description, row.get('series')),
                'glass_min': glass_min,
                'glass_max': glass_max,
                'tracks': parse_tracks(group, description),
            })

        self._report(parsed, options['show'])

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('\nDry run — nothing written.'))
            return

        self._load(entry, parsed)

    # -- reporting ----------------------------------------------------------

    def _report(self, parsed, show):
        roles = Counter(row['role'] for row in parsed)
        self.stdout.write('\nROLE DISTRIBUTION')
        for role, count in roles.most_common():
            label = ProfileRole(role).label
            pct = 100 * count / len(parsed)
            self.stdout.write(f'  {count:5d}  {pct:5.1f}%  {label}')

        with_glass = sum(1 for r in parsed if r['glass_max'] is not None)
        with_tracks = sum(1 for r in parsed if r['tracks'] is not None)
        self.stdout.write(
            f'\n  glazing range parsed : {with_glass:5d} rows'
            f'\n  track count parsed   : {with_tracks:5d} rows'
        )

        unknown = [r for r in parsed if r['role'] == ProfileRole.OTHER]
        if unknown:
            self.stdout.write(
                self.style.WARNING(f'\n{len(unknown)} rows fell through to OTHER:')
            )
            samples = Counter(
                (r['group'], r['description']) for r in unknown
            )
            for (group, description), count in samples.most_common(15):
                self.stdout.write(f'  {count:3d}×  group={group!r}  desc={description!r}')

        if show:
            self.stdout.write('\nSAMPLES')
            by_role = {}
            for row in parsed:
                by_role.setdefault(row['role'], []).append(row)
            for role, items in by_role.items():
                self.stdout.write(f'\n  {ProfileRole(role).label}:')
                for row in items[:show]:
                    glass = ''
                    if row['glass_max'] is not None:
                        low = row['glass_min'] if row['glass_min'] is not None else '≤'
                        glass = f"  glass={low}..{row['glass_max']}"
                    tracks = f"  tracks={row['tracks']}" if row['tracks'] else ''
                    self.stdout.write(
                        f"    {row['profile_number']}  {row['description']!r}"
                        f"{glass}{tracks}"
                    )

    # -- loading ------------------------------------------------------------

    @transaction.atomic
    def _load(self, entry, parsed):
        maker, _ = Manufacturer.objects.update_or_create(
            slug=entry['slug'],
            defaults={
                'name': entry['name'],
                'name_en': entry.get('name_en', ''),
                'website': entry.get('website', ''),
                'attribution': entry.get('attribution', ''),
                'is_default': bool(entry.get('is_default')),
                'position': entry.get('position', 0),
            },
        )
        family_en = entry.get('family_en', {})

        families = {}
        for row in parsed:
            name = row.get('family')
            if name and name not in families:
                families[name], _ = Family.objects.get_or_create(
                    manufacturer=maker,
                    name=name,
                    defaults={
                        'slug': slugify(family_en.get(name, name), allow_unicode=True),
                        'name_en': family_en.get(name, ''),
                    },
                )

        series_cache = {}
        for row in parsed:
            code = str(row['series'])
            if code in series_cache:
                continue
            series_cache[code], _ = Series.objects.get_or_create(
                manufacturer=maker,
                code=code,
                defaults={
                    'name': row.get('series_name')
                    or (f"{entry['name']} {row['family']}" if row.get('family') else code),
                    'family': families.get(row.get('family')),
                    'catalog_page': row.get('page'),
                },
            )

        # One Profile per number. Where the catalog prints conflicting weights
        # for the same number, the first wins on Profile and the differing value
        # is kept per-listing on SeriesProfile.
        profiles = {}
        for row in parsed:
            number = row['profile_number']
            if number not in profiles:
                profiles[number], _ = Profile.objects.get_or_create(
                    manufacturer=maker,
                    number=number,
                    defaults={
                        'description': row.get('description') or '',
                        'weight_g_per_m': row.get('weight_g_per_m'),
                    },
                )
                # The catalog JSON carries the Cloudinary section-image path
                # (catalog/sections/<number>), so a fresh DB shows photos with
                # no local image files. Only set it if the profile has none.
                image = row.get('section_image')
                prof = profiles[number]
                if image and not prof.section_image:
                    prof.section_image = image
                    prof.save(update_fields=['section_image'])

        listings = 0
        for index, row in enumerate(parsed):
            profile = profiles[row['profile_number']]
            description = row.get('description') or ''
            weight = row.get('weight_g_per_m')

            _, created = SeriesProfile.objects.get_or_create(
                series=series_cache[str(row['series'])],
                profile=profile,
                group_header=row.get('group') or '',
                listed_description=(
                    description if description != profile.description else ''
                ),
                defaults={
                    'listed_weight_g_per_m': (
                        weight if weight != profile.weight_g_per_m else None
                    ),
                    'role': row['role'],
                    'glass_min_mm': row['glass_min'],
                    'glass_max_mm': row['glass_max'],
                    'track_count': row['tracks'],
                    'catalog_page': row.get('page'),
                    'position': index,
                },
            )
            listings += created

        linked = self._link_equivalents(maker, parsed, profiles)

        self.stdout.write(self.style.SUCCESS(
            f'\nLoaded {maker.name}: {len(families)} families, {len(series_cache)} series, '
            f'{len(profiles)} profiles, {listings} new listings, '
            f'{linked} equivalents linked.'
        ))

    def _link_equivalents(self, maker, parsed, profiles):
        """Point each row's profile at the compatible extrusion it names.

        `equivalent_number` is matched in `equivalent_manufacturer` (default:
        the default manufacturer); a padded form may be given for catalogues
        that print numbers without the leading zeros the other maker uses.
        """
        linked = 0
        for row in parsed:
            number = row.get('equivalent_number')
            if not number:
                continue
            profile = profiles[row['profile_number']]
            if profile.equivalent_of_id:
                continue
            other_slug = row.get('equivalent_manufacturer')
            other = (Manufacturer.objects.filter(slug=other_slug).first()
                     if other_slug else Manufacturer.get_default())
            if other is None or other == maker:
                continue
            candidates = [str(number)]
            if row.get('equivalent_number_padded'):
                candidates.insert(0, str(row['equivalent_number_padded']))
            target = Profile.objects.filter(
                manufacturer=other, number__in=candidates).first()
            if target is None:
                continue
            profile.equivalent_of = target
            profile.save(update_fields=['equivalent_of'])
            linked += 1
        return linked
