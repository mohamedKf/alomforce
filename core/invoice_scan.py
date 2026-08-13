"""Read an invoice photo with GPT vision and hand back the fields.

The office photographs a supplier invoice and the numbers appear in the form,
already filled in. What this returns is a *suggestion*: every value goes into
a form the person confirms before anything is saved, because a model misreading
a total by one digit is a plausible failure and an unreviewed one would land
straight in the books.

Deliberately dependency-free -- one HTTPS call built with urllib rather than
the OpenAI SDK, which would add a package (and its transitive httpx) to every
deploy for a single request.

When no API key is configured this module is never reached: the endpoint says
so, and the app falls back to typing the invoice in by hand.
"""

import base64
import json
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = 'https://api.openai.com/v1/chat/completions'

# Vision-capable and cheap enough to run on every invoice the office snaps.
MODEL = 'gpt-4o-mini'

TIMEOUT = 60

# What we ask for, and the only keys we read back. Anything else the model
# volunteers is ignored rather than trusted into the form.
FIELDS = (
    'number', 'issued_at', 'party_name', 'party_tax_id',
    'subtotal', 'vat', 'total', 'category',
)

PROMPT = """You are reading a supplier invoice for an Israeli aluminium
business. Extract these fields and return ONLY a JSON object, no prose:

  number       - the invoice number as printed
  issued_at    - the invoice date as YYYY-MM-DD
  party_name   - the supplier's business name, as printed
  party_tax_id - the supplier's tax/company number (ח.פ / ע.מ), digits only
  subtotal     - amount before VAT, digits and a decimal point only
  vat          - the VAT amount
  total        - the amount payable including VAT
  category     - a short category in English, e.g. electricity, fuel, rent,
                 tools, insurance, materials

Rules:
- Hebrew invoices are normal; read them as printed.
- Use null for anything you cannot read. Never guess a number.
- Amounts: no currency symbols, no thousands separators.
- If the document shows a total and VAT but no subtotal, compute
  subtotal = total - vat.
"""


class ScanUnavailable(RuntimeError):
    """No API key, so scanning is switched off."""


class ScanFailed(RuntimeError):
    """The call was made and did not produce anything usable."""


def _decimal_or_none(value):
    if value in (None, '', 'null'):
        return None
    try:
        return Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _clean(raw):
    """Keep only the fields we asked for, in the shapes the form expects."""
    out = {}
    for key in FIELDS:
        value = raw.get(key)
        if value in (None, '', 'null'):
            out[key] = None
        elif key in ('subtotal', 'vat', 'total'):
            number = _decimal_or_none(value)
            out[key] = str(number) if number is not None else None
        else:
            out[key] = str(value).strip()

    # A total with no subtotal is common on small invoices; deriving it here
    # saves the office arithmetic they would otherwise do by hand.
    total, vat, subtotal = (_decimal_or_none(out.get(k))
                            for k in ('total', 'vat', 'subtotal'))
    if subtotal is None and total is not None and vat is not None:
        out['subtotal'] = str(total - vat)
    elif total is None and subtotal is not None and vat is not None:
        out['total'] = str(subtotal + vat)
    return out


def extract(file_bytes, content_type, api_key):
    """Return a dict of suggested invoice fields.

    Raises ScanUnavailable when there is no key, ScanFailed when the call or
    the reply is unusable -- both of which the endpoint turns into "type it in
    yourself" rather than a dead end.
    """
    if not api_key:
        raise ScanUnavailable('No OpenAI API key configured.')

    if content_type == 'application/pdf':
        # The vision endpoint takes images; a PDF has to be rasterised first,
        # which this deployment has no library for. Better to say so than to
        # send bytes the model will reject.
        raise ScanFailed('PDF scanning is not supported yet — photograph the '
                         'invoice, or enter it by hand.')

    encoded = base64.b64encode(file_bytes).decode('ascii')
    body = json.dumps({
        'model': MODEL,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'text', 'text': PROMPT},
                {'type': 'image_url',
                 'image_url': {'url': f'data:{content_type};base64,{encoded}'}},
            ],
        }],
        # Forces valid JSON back, so a chatty reply cannot break the parse.
        'response_format': {'type': 'json_object'},
        'max_tokens': 700,
        # The numbers on the page are the answer; there is nothing to be
        # creative about.
        'temperature': 0,
    }).encode('utf-8')

    request = Request(API_URL, data=body, method='POST', headers={
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    })

    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:300]
        raise ScanFailed(f'The scanning service refused the request ({exc.code}). '
                         f'{detail}') from exc
    except (URLError, TimeoutError) as exc:
        raise ScanFailed('Could not reach the scanning service.') from exc
    except json.JSONDecodeError as exc:
        raise ScanFailed('The scanning service sent something unreadable.') from exc

    try:
        content = payload['choices'][0]['message']['content']
        return _clean(json.loads(content))
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ScanFailed('Could not read the invoice from that image.') from exc
