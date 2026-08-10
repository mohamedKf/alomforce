"""Build the accountant's monthly package.

For a chosen month it produces a single ZIP containing:
  - salary_YYYY_MM.xlsx        — the salary sheet (days/hours/rate/pay)
  - income_YYYY_MM.pdf         — every income invoice, combined into one PDF
  - expense_YYYY_MM.pdf        — every expense invoice, combined into one PDF
  - invoices/…                 — each invoice's original file (PDF or photo)

Invoice files that are PDFs are appended as-is; image files are wrapped onto a
page. Invoices with no attached file get a small generated summary page so the
combined PDF still accounts for them.
"""

import zipfile
from io import BytesIO

from django.utils import timezone

from core.models import Invoice
from core.salary_excel import build_salary_excel, salary_filename


def _month_bounds(year, month):
    now = timezone.localtime()
    start = now.replace(year=year, month=month, day=1, hour=0, minute=0,
                        second=0, microsecond=0)
    end = start.replace(year=year + (month == 12),
                        month=1 if month == 12 else month + 1)
    return start, end


def _summary_page(invoice):
    """A one-page PDF standing in for an invoice that has no attached file."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont('Helvetica-Bold', 16)
    c.drawString(50, h - 70, f'{invoice.get_direction_display()} invoice'
                             f'  {invoice.number or invoice.id}')
    c.setFont('Helvetica', 12)
    y = h - 110
    party = (invoice.client.name if invoice.client else invoice.party_name) or ''
    for label, value in [
        ('Party', party),
        ('Tax ID', invoice.party_tax_id or ''),
        ('Date', invoice.issued_at.isoformat() if invoice.issued_at else ''),
        ('Category', invoice.category or ''),
        ('Subtotal', f'{invoice.subtotal}'),
        ('VAT', f'{invoice.vat}'),
        ('Total', f'{invoice.total}'),
        ('Paid', f'{invoice.amount_paid}'),
    ]:
        c.drawString(50, y, f'{label}: {value}')
        y -= 22
    c.drawString(50, y - 10, '(no file attached)')
    c.showPage()
    c.save()
    return buf.getvalue()


def _image_page(data):
    """Wrap an image (bytes) onto a single A4 PDF page, or None on failure."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    try:
        img = ImageReader(BytesIO(data))
        iw, ih = img.getSize()
    except Exception:                                     # noqa: BLE001
        return None
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    margin = 40
    scale = min((w - 2 * margin) / iw, (h - 2 * margin) / ih)
    dw, dh = iw * scale, ih * scale
    c.drawImage(img, (w - dw) / 2, (h - dh) / 2, dw, dh,
                preserveAspectRatio=True, mask='auto')
    c.showPage()
    c.save()
    return buf.getvalue()


def _invoice_file_bytes(invoice):
    if not invoice.file:
        return None, None
    try:
        invoice.file.open('rb')
        data = invoice.file.read()
        invoice.file.close()
    except Exception:                                     # noqa: BLE001
        return None, None
    name = invoice.file.name.lower()
    kind = 'pdf' if name.endswith('.pdf') else 'image'
    return data, kind


def _combined_pdf(invoices):
    """Merge a set of invoices into one PDF (bytes)."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for inv in invoices:
        data, kind = _invoice_file_bytes(inv)
        page_pdf = None
        if data and kind == 'pdf':
            page_pdf = data
        elif data and kind == 'image':
            page_pdf = _image_page(data)
        if page_pdf is None:
            page_pdf = _summary_page(inv)
        try:
            for page in PdfReader(BytesIO(page_pdf)).pages:
                writer.add_page(page)
        except Exception:                                 # noqa: BLE001
            for page in PdfReader(BytesIO(_summary_page(inv))).pages:
                writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def build_accountant_zip(year, month):
    """Return (zip_bytes, summary_dict) for the accountant package."""
    start, end = _month_bounds(year, month)
    base = Invoice.objects.filter(issued_at__gte=start.date(),
                                  issued_at__lt=end.date()).select_related('client')
    income = list(base.filter(direction=Invoice.Direction.INCOME))
    expense = list(base.filter(direction=Invoice.Direction.EXPENSE))

    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(salary_filename(year, month), build_salary_excel(year, month))
        if income:
            z.writestr(f'income_{year}_{month:02d}.pdf', _combined_pdf(income))
        if expense:
            z.writestr(f'expense_{year}_{month:02d}.pdf', _combined_pdf(expense))
        # Original files too, grouped by direction.
        for inv in income + expense:
            data, kind = _invoice_file_bytes(inv)
            if data:
                ext = 'pdf' if kind == 'pdf' else 'jpg'
                folder = 'invoices/income' if inv.direction == 'income' else 'invoices/expense'
                z.writestr(f'{folder}/{inv.number or inv.id}.{ext}', data)

    summary = {
        'income_count': len(income), 'expense_count': len(expense),
        'month': f'{year:04d}-{month:02d}',
    }
    return buf.getvalue(), summary


def zip_filename(year, month):
    return f'accountant_{year}_{month:02d}.zip'
