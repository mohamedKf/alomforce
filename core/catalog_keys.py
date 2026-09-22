"""Naming a series or a profile across manufacturers.

A catalogue number only means something together with who printed it: Klil's
7000 and Schremer's 7000-compatible line are different series, and a Profal
five-digit number can coincide with a Klil one. So the apps and the API name
things by *key*: ``<manufacturer slug>:<code>``, as in ``klil:7000`` or
``extal:060093``.

A bare code is still accepted everywhere a key is, because that is what the
QR labels already on the racks carry and what a fitter types. It resolves
when only one manufacturer has it; when several do, the default manufacturer
wins, and ``?manufacturer=`` narrows it explicitly.
"""

from django.db.models import Q
from django.http import Http404

from .models import Manufacturer, Profile, Series

SEP = ':'


def split_key(value):
    """`'extal:C90'` -> `('extal', 'C90')`; `'7000'` -> `(None, '7000')`."""
    value = (value or '').strip()
    if SEP in value:
        slug, _, code = value.partition(SEP)
        return (slug.strip().lower() or None), code.strip()
    return None, value


def manufacturer_q(value, prefix=''):
    """A filter for a manufacturer given by slug or id, at an optional prefix
    such as ``series__`` or ``profile__``."""
    value = (value or '').strip()
    if not value:
        return Q()
    if value.isdigit():
        return Q(**{f'{prefix}manufacturer_id': int(value)})
    return Q(**{f'{prefix}manufacturer__slug': value.lower()})


def code_q(value, field, manufacturer=None, prefix=''):
    """A filter for a key or bare code on `field` (``code`` or ``number``).

    ``code_q('extal:C90', 'code', prefix='series__')`` matches listings whose
    series is Extal's C90; ``code_q('7000', 'code')`` matches every 7000.
    The explicit ``manufacturer`` applies only when the value carries none.
    """
    slug, code = split_key(value)
    q = Q(**{f'{prefix}{field}': code})
    if slug:
        q &= Q(**{f'{prefix}manufacturer__slug': slug})
    elif manufacturer:
        q &= manufacturer_q(manufacturer, prefix)
    else:
        # A bare code from an older client or a rack label: the default
        # maker's, when it has one; otherwise whoever prints it.
        model = Series if field == 'code' else Profile
        if model.objects.filter(**{field: code, 'manufacturer__is_default': True}).exists():
            q &= Q(**{f'{prefix}manufacturer__is_default': True})
    return q


def _resolve(queryset, field, value, manufacturer=None):
    matches = list(
        queryset.filter(code_q(value, field, manufacturer))
        .select_related('manufacturer')
    )
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise Http404
    preferred = [m for m in matches if m.manufacturer.is_default]
    if len(preferred) == 1:
        return preferred[0]
    raise Ambiguous(value, matches)


class Ambiguous(Http404):
    """A bare code that several manufacturers use, with no way to choose."""

    def __init__(self, value, matches):
        self.value = value
        self.matches = matches
        keys = ', '.join(m.key for m in matches)
        super().__init__(f'{value!r} is ambiguous; use one of: {keys}')


def resolve_series(value, manufacturer=None, queryset=None):
    return _resolve(queryset if queryset is not None else Series.objects.all(),
                    'code', value, manufacturer)


def resolve_profile(value, manufacturer=None, queryset=None):
    return _resolve(queryset if queryset is not None else Profile.objects.all(),
                    'number', value, manufacturer)


def label_for(obj):
    """What to print for a code when the manufacturer matters: the bare code
    for the default manufacturer, the full key for anyone else, so existing
    Klil labels and screens read as they always did."""
    code = obj.number if isinstance(obj, Profile) else obj.code
    return code if obj.manufacturer.is_default else obj.key


__all__ = [
    'Ambiguous', 'Manufacturer', 'code_q', 'label_for', 'manufacturer_q',
    'resolve_profile', 'resolve_series', 'split_key',
]
