"""Import the Klil profile catalog from core/data/klil_catalog_rows.json.

The source rows carry a Hebrew group header and description; this command turns
those into structured role / glazing range / track count so the apps can filter
on them ("show me every 3-track rail that takes 16mm glass").

Run `--dry-run` to see what the parser makes of the data without writing.
"""

import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from core.models import Family, Profile, ProfileRole, Series, SeriesProfile

# commands/ -> management/ -> core/
DATA_FILE = Path(__file__).resolve().parents[2] / 'data' / 'klil_catalog_rows.json'

# The catalog data lives outside the code so any deployment imports from the
# same source. CATALOG_URL (Railway/.env) points at the JSON in Cloudinary; if
# it isn't set or can't be fetched, we fall back to the bundled DATA_FILE.
DEFAULT_CATALOG_URL = (
    'https://res.cloudinary.com/dvhuyctib/raw/upload/catalog/klil_catalog_rows.json'
)


def load_catalog_rows():
    """Return (rows, source_label). Cloudinary first, local file as fallback."""
    from decouple import config
    url = config('CATALOG_URL', default=DEFAULT_CATALOG_URL)
    if url:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=30) as resp:
                rows = json.loads(resp.read().decode('utf-8'))
            return rows, url
        except Exception as exc:  # noqa: BLE001 - fall back to the bundled file
            print(f'catalog: could not fetch {url} ({exc}); using local file')
    return json.loads(DATA_FILE.read_text(encoding='utf-8')), str(DATA_FILE.name)


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


# --- Family ----------------------------------------------------------------

FAMILY_EN = {
    'קלאסי': 'Classic',
    'אופיס': 'Office',
    'בלגי': 'Belgian',
    'מנהטן': 'Manhattan',
    'נוף': 'Nof',
}


class Command(BaseCommand):
    help = 'Import the Klil profile catalog into the database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Parse and report without writing anything.',
        )
        parser.add_argument(
            '--show', type=int, default=0,
            help='Print N sample parsed rows per role.',
        )

    def handle(self, *args, **options):
        rows, source = load_catalog_rows()
        self.stdout.write(f'Loaded {len(rows)} rows from {source}')

        parsed = []
        for row in rows:
            group = row.get('group')
            description = row.get('description') or ''
            glass_min, glass_max = parse_glass(group, description)
            parsed.append({
                **row,
                'role': detect_role(group, description, row.get('series')),
                'glass_min': glass_min,
                'glass_max': glass_max,
                'tracks': parse_tracks(group, description),
            })

        self._report(parsed, options['show'])

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('\nDry run — nothing written.'))
            return

        self._load(parsed)

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
    def _load(self, parsed):
        families = {}
        for row in parsed:
            name = row.get('family')
            if name and name not in families:
                families[name], _ = Family.objects.get_or_create(
                    name=name,
                    defaults={
                        'slug': slugify(FAMILY_EN.get(name, name), allow_unicode=True),
                        'name_en': FAMILY_EN.get(name, ''),
                    },
                )

        series_cache = {}
        for row in parsed:
            code = str(row['series'])
            if code in series_cache:
                continue
            series_cache[code], _ = Series.objects.get_or_create(
                code=code,
                defaults={
                    'name': f"קליל {row['family']}" if row.get('family') else code,
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
            listings += int(created)

        self.stdout.write(self.style.SUCCESS(
            f'\nLoaded {len(families)} families, {len(series_cache)} series, '
            f'{len(profiles)} profiles, {listings} listings.'
        ))
