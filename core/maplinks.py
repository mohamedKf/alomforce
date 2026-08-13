"""Pull a coordinate out of a shared map link.

Nobody types latitude and longitude. What people actually do is find the place
in whatever map app is on their phone, hit share, and paste the link -- so this
accepts that link from Google Maps, Waze, Apple Maps or OpenStreetMap and
returns the point it refers to.

The point matters more than the text address: a delivery goes to a yard gate or
a side entrance that "רחוב התעשייה 12" will not find, and the driver's Waze
button navigates to the coordinate when there is one.

Short links (maps.app.goo.gl, goo.gl/maps, waze.com/ul/...) carry no coordinate
at all -- they are opaque ids -- so they are resolved by following the redirect
to the long URL first. That is one network call, made when a client is saved.
"""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

# Hosts whose links are opaque ids that must be expanded before they say
# anything about a location.
SHORT_HOSTS = {
    'goo.gl', 'maps.app.goo.gl', 'g.co',
    'waze.com', 'www.waze.com',          # only the /ul/<id> form is short
    'maps.apple', 'apple.co',
    'bit.ly', 'tinyurl.com',
}

# Query parameters different apps use to carry "the place", in the order we
# trust them. Google uses q/ll, Apple ll/q/daddr/coordinate, Waze ll/to/latlng.
COORD_PARAMS = (
    'll', 'q', 'query', 'coordinate', 'daddr', 'latlng', 'sll',
    'to', 'destination', 'center', 'cp',
)

# 32.0853,34.7818 -- the shape a coordinate takes wherever it appears.
_PAIR = re.compile(r'(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)')
# Google's map viewport: /maps/@32.0853,34.7818,17z
_AT = re.compile(r'@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)')
# Google's place data blob: !3d32.0853!4d34.7818 -- the actual pin, which is
# often a few metres from the @ viewport centre, so it is preferred.
_BANG = re.compile(r'!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)')

TIMEOUT = 6


class MapLinkError(ValueError):
    """The link carried no usable coordinate."""


def _clean(value):
    """A coordinate as Decimal, or None when the text is not one."""
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError):
        return None
    return number


def _valid(lat, lng):
    return (
        lat is not None and lng is not None
        and Decimal('-90') <= lat <= Decimal('90')
        and Decimal('-180') <= lng <= Decimal('180')
        # 0,0 is in the Atlantic; it means a parse went wrong, not a delivery.
        and not (lat == 0 and lng == 0)
    )


def _from_pair_text(text):
    if not text:
        return None
    text = unquote(str(text))
    # Waze writes its destination as 'll.32.0853,34.7818'; OSM as '32.08/34.78'.
    text = text.replace('ll.', '').replace('loc:', '')
    match = _PAIR.search(text)
    if not match:
        return None
    lat, lng = _clean(match.group(1)), _clean(match.group(2))
    return (lat, lng) if _valid(lat, lng) else None


def _is_short(url):
    host = (urlparse(url).hostname or '').lower()
    if host in ('waze.com', 'www.waze.com'):
        # waze.com/ul?ll=... already carries the point; waze.com/ul/hsv8v does not.
        return '?' not in url
    return host in SHORT_HOSTS


def expand(url):
    """Follow a short link to whatever it points at. Returns the final URL.

    A failure here is not fatal -- the original URL is returned and parsing is
    tried on it anyway, so a network blip cannot lose a link the user pasted.
    """
    try:
        request = Request(url, method='GET', headers={
            # Google hands short links to a browser-shaped client only.
            'User-Agent': 'Mozilla/5.0 (compatible; AlomForce/1.0)',
        })
        with urlopen(request, timeout=TIMEOUT) as response:
            return response.geturl() or url
    except Exception:  # noqa: BLE001 - any network failure falls back to the raw URL
        return url


def extract_coordinates(url, resolve=True):
    """Return (lat, lng) as Decimals for a map link, or None.

    `resolve=False` skips the redirect lookup, for tests and for callers that
    must not make a network call.
    """
    if not url or not str(url).strip():
        return None

    url = str(url).strip()
    if resolve and _is_short(url):
        url = expand(url)

    parsed = urlparse(url)

    # 1. The pin inside Google's data blob -- the most precise thing on offer.
    found = _BANG.search(url)
    if found:
        lat, lng = _clean(found.group(1)), _clean(found.group(2))
        if _valid(lat, lng):
            return lat, lng

    # 2. Named parameters, in trust order.
    params = parse_qs(parsed.query)
    for key in COORD_PARAMS:
        for raw in params.get(key, []):
            pair = _from_pair_text(raw)
            if pair:
                return pair

    # 3. OpenStreetMap keeps them apart.
    lat, lng = _clean(params.get('mlat', [None])[0]), _clean(params.get('mlon', [None])[0])
    if _valid(lat, lng):
        return lat, lng

    # 4. Google's viewport centre.
    found = _AT.search(url)
    if found:
        lat, lng = _clean(found.group(1)), _clean(found.group(2))
        if _valid(lat, lng):
            return lat, lng

    # 5. geo:32.08,34.78 and any bare pair left in the path or fragment.
    for chunk in (parsed.path, parsed.fragment, parsed.query):
        pair = _from_pair_text(chunk)
        if pair:
            return pair

    return None
