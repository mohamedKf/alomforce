"""Printable QR labels for profiles.

The office prints a sheet of labels and sticks one on each rack/bundle; a
warehouse worker scans it in the phone app to pull up what the profile is. The
QR encodes the plain profile number, so the phone just looks that number up in
the catalog -- no separate code registry to keep in sync.
"""
from io import BytesIO

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

# Reuse the order-note font handling so the Hebrew/Arabic descriptions render
# (Helvetica has no Hebrew glyphs) and read right-to-left.
from core.order_pdf import _font, _has_arabic, _shape

# 3 columns x 8 rows of labels per A4 page.
COLS = 3
ROWS = 8
MARGIN = 12 * mm
GUTTER = 4 * mm


def _qr_image(text):
    qr = qrcode.QRCode(box_size=10, border=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return ImageReader(buf)


def render_qr_labels(rows):
    """rows: iterable of (number, description). Returns PDF bytes."""
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    page_w, page_h = A4
    cell_w = (page_w - 2 * MARGIN - (COLS - 1) * GUTTER) / COLS
    cell_h = (page_h - 2 * MARGIN - (ROWS - 1) * GUTTER) / ROWS
    per_page = COLS * ROWS

    for i, (number, description) in enumerate(rows):
        slot = i % per_page
        if slot == 0 and i > 0:
            c.showPage()
        col = slot % COLS
        row = slot // COLS
        x = MARGIN + col * (cell_w + GUTTER)
        # reportlab's origin is bottom-left; lay rows out top to bottom.
        y = page_h - MARGIN - (row + 1) * cell_h - row * GUTTER

        # border
        c.setStrokeColor(colors.HexColor('#DDE4EC'))
        c.setLineWidth(0.6)
        c.roundRect(x, y, cell_w, cell_h, 4, stroke=1, fill=0)

        # QR, centred in the upper part of the cell
        qr_size = min(cell_w, cell_h) - 16 * mm
        qr_x = x + (cell_w - qr_size) / 2
        qr_y = y + cell_h - qr_size - 5 * mm
        c.drawImage(_qr_image(str(number)), qr_x, qr_y, qr_size, qr_size)

        # profile number (bold) + description under the QR
        c.setFillColor(colors.HexColor('#14213A'))
        c.setFont('Helvetica-Bold', 13)
        c.drawCentredString(x + cell_w / 2, y + 9 * mm, str(number))
        desc = (description or '')[:24]
        if desc:
            # Rubik for Hebrew, the Arabic font when the text is Arabic; shaped
            # so RTL descriptions read correctly.
            font = _font('ar') if _has_arabic(desc) else _font('he')
            c.setFont(font, 7.5)
            c.setFillColor(colors.HexColor('#6B7785'))
            c.drawCentredString(x + cell_w / 2, y + 4.5 * mm, _shape(desc))

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.getvalue()
