"""Printable PDFs for an order: a priced ORDER NOTE and a DELIVERY NOTE.

Aluminium is sold by weight, so both documents lead with metres and weight and
price the order as weight x price/kg, then an order-wide discount and VAT.

Hebrew, right-to-left. python-bidi reorders RTL text and arabic-reshaper joins
Arabic letters, so an Arabic client or profile name inside the otherwise Hebrew
layout still renders. Fonts: Rubik (Hebrew), Amiri (Arabic).

Adapted from the import_system order_pdf, kept self-contained here so the two
projects can drift independently.
"""
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

_FONTS = Path(__file__).parent / 'fonts'
_FONT_FOR = {'ar': ('Amiri', 'Amiri.ttf'), 'he': ('Rubik', 'Rubik.ttf')}
_registered = set()

PRIMARY = colors.HexColor('#2F6F8F')     # matches the desktop accent
LIGHT = colors.HexColor('#eef2f9')
ZEBRA = colors.HexColor('#f7f9fc')
LINE = colors.HexColor('#d0d7e2')
MUTED = colors.HexColor('#6b7280')

_STATUS_HE = {
    'draft': 'טיוטה', 'submitted': 'הוגש', 'confirmed': 'מאושר',
    'picking': 'בליקוט', 'ready': 'מוכן', 'delivered': 'נמסר', 'cancelled': 'בוטל',
}
_STATUS_COLOR = {
    'draft': '#6b7280', 'submitted': '#b45309', 'confirmed': '#2F6F8F',
    'picking': '#7c3aed', 'ready': '#0e7490', 'delivered': '#15803d',
    'cancelled': '#b91c1c',
}

_DN_NOTICE = [
    'יש לדווח על כל חוסר או נזק תוך 72 שעות מקבלת הטובין.',
    'לא ניתן להחזיר טובין ללא אישור מראש מהחברה.',
]


# ─────────────────────────── text shaping ───────────────────────────
def _font(lang):
    name, filename = _FONT_FOR.get(lang, _FONT_FOR['he'])
    if name not in _registered:
        pdfmetrics.registerFont(TTFont(name, str(_FONTS / filename)))
        _registered.add(name)
    return name


_ARABIC_RE = re.compile('[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]')


def _has_arabic(text):
    return bool(text) and bool(_ARABIC_RE.search(str(text)))


def _shape(text):
    text = '' if text is None else str(text)
    if not text:
        return ''
    if _has_arabic(text):
        text = arabic_reshaper.reshape(text)
    return get_display(text)


def _rtl(text):
    raw = '' if text is None else str(text)
    if not raw:
        return ''
    esc = _shape(raw).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return f'<font name="{_font("ar")}">{esc}</font>' if _has_arabic(raw) else esc


def _money(v):
    try:
        return f'{float(v or 0):,.2f}'
    except (TypeError, ValueError):
        return str(v)


def _num(v):
    """A plain number, trimmed of trailing zeros (12.50 -> 12.5, 6.00 -> 6)."""
    try:
        return f'{float(v):g}'
    except (TypeError, ValueError):
        return str(v)


# ─────────────────────────── shared layout ───────────────────────────
def _new_doc(prefix, order):
    fd, path = tempfile.mkstemp(prefix=prefix, suffix='.pdf')
    os.close(fd)
    doc = SimpleDocTemplate(
        path, pagesize=A4,
        rightMargin=15 * mm, leftMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=18 * mm, title=order.number)
    return doc, path


def _he_header(company, title_text, detail_rows):
    font = _font('he')

    def s(txt):
        return _shape(txt)

    comp_L = ParagraphStyle('cL', fontName=font, fontSize=15, alignment=TA_LEFT,
                            textColor=PRIMARY, leading=19)
    mut_L = ParagraphStyle('mL', fontName=font, fontSize=8.5, alignment=TA_LEFT,
                           textColor=MUTED, leading=12)
    title_st = ParagraphStyle('t', fontName=font, fontSize=22, alignment=TA_RIGHT,
                              textColor=PRIMARY, leading=26)
    label_st = ParagraphStyle('lb', fontName=font, fontSize=9, alignment=TA_RIGHT,
                              textColor=MUTED, leading=13)
    val_st = ParagraphStyle('v', fontName=font, fontSize=9.5, alignment=TA_LEFT, leading=13)

    addr = ' · '.join(x for x in [company.get('address'), company.get('city')] if x)
    comp_cell = []
    logo = logo_flowable(company)
    if logo is not None:
        comp_cell.append(logo)
        comp_cell.append(Spacer(1, 2.5 * mm))
    if company.get('name'):
        comp_cell.append(Paragraph(_rtl(company['name']), comp_L))
    if addr:
        comp_cell.append(Paragraph(_rtl(addr), mut_L))
    if company.get('phone'):
        comp_cell.append(Paragraph(f'{s("טלפון")}: {_rtl(company["phone"])}', mut_L))
    if company.get('tax_id'):
        comp_cell.append(Paragraph(f'{s("ח.פ/עוסק")}: {_rtl(company["tax_id"])}', mut_L))
    if not comp_cell:
        comp_cell.append(Paragraph(s('AlomForce'), comp_L))

    det_data = [[Paragraph(_rtl(v), val_st), Paragraph(f'{s(k)}:', label_st)]
                for k, v in detail_rows]
    details = Table(det_data, colWidths=[40 * mm, 30 * mm], hAlign='RIGHT')
    details.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LEFTPADDING', (0, 0), (-1, -1), 2), ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ('LINEBELOW', (0, 0), (-1, -2), 0.4, LINE),
    ]))
    title_cell = [Paragraph(s(title_text), title_st), Spacer(1, 4 * mm), details]

    header = Table([[comp_cell, title_cell]], colWidths=[None, 74 * mm])
    header.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    return [header, Spacer(1, 4 * mm),
            HRFlowable(width='100%', thickness=1.4, color=PRIMARY, spaceAfter=8)]


def _he_addr_blocks(right_title, right_lines, left_title, left_lines):
    font = _font('he')

    def s(txt):
        return _shape(txt)

    right = ParagraphStyle('ar', fontName=font, fontSize=9.5, alignment=TA_RIGHT, leading=14)
    muted = ParagraphStyle('am', fontName=font, fontSize=8.5, alignment=TA_RIGHT,
                           textColor=MUTED, leading=12)
    bar_st = ParagraphStyle('bar', fontName=font, fontSize=10, alignment=TA_RIGHT,
                            textColor=colors.white, leading=13)

    def block(title, lines):
        bar = Table([[Paragraph(s(title), bar_st)]], colWidths=[84 * mm])
        bar.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), PRIMARY),
            ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        body = [bar, Spacer(1, 3 * mm)]
        for i, ln in enumerate([x for x in lines if x]):
            body.append(Paragraph(_rtl(ln), right if i == 0 else muted))
        return body

    ab = Table([[block(right_title, right_lines), block(left_title, left_lines)]],
               colWidths=[88 * mm, 88 * mm])
    ab.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (0, -1), 0), ('RIGHTPADDING', (0, 0), (0, -1), 8),
        ('LEFTPADDING', (1, 0), (1, -1), 8), ('RIGHTPADDING', (1, 0), (1, -1), 0),
    ]))
    return [ab, Spacer(1, 7 * mm)]


def _footer_drawer(company, font):
    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.5)
        canvas.line(15 * mm, 14 * mm, A4[0] - 15 * mm, 14 * mm)
        canvas.setFillColor(MUTED)
        foot = ' · '.join(x for x in [company.get('name'), company.get('phone'),
                                      company.get('email')] if x)
        canvas.setFont(_font('ar') if _has_arabic(foot) else font, 8)
        if foot:
            canvas.drawCentredString(A4[0] / 2, 9 * mm, _shape(foot))
        canvas.setFont(font, 8)
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, f'{doc_.page}')
        canvas.restoreState()
    return footer


def _addr_blocks_for(order, company):
    cd = order.client
    client_lines = [cd.name, cd.contact_name, cd.address, cd.city, cd.phone]
    addr = ' · '.join(x for x in [company.get('address'), company.get('city')] if x)
    supplier = [company.get('name'), addr,
                (f'טלפון: {company.get("phone")}' if company.get('phone') else ''),
                (f'ח.פ/עוסק: {company.get("tax_id")}' if company.get('tax_id') else '')]
    return client_lines, supplier


def _detail_rows(order):
    from django.utils import timezone
    # Show the local (Asia/Jerusalem) date; ordered_at is stored UTC-aware, so a
    # late-evening order would otherwise print the previous day.
    ordered = (timezone.localtime(order.ordered_at).strftime('%Y-%m-%d')
               if order.ordered_at else '')
    rows = [("מס' הזמנה", order.number),
            ('תאריך', ordered),
            ("מס' לקוח", order.client.tax_id or order.client.phone or '')]
    if order.required_by:
        rows.append(('תאריך אספקה', order.required_by.strftime('%Y-%m-%d')))
    return rows


# Hebrew names for the profile roles, for the document description.
ROLE_HE = {
    'frame': 'משקוף', 'sash': 'כנף', 'track': 'מסילה', 'mullion': 'חציץ',
    'glazing_bead': 'סרגל זיגוג', 'shutter': 'תריס', 'trim': 'הלבשה',
    'adapter': 'מעבר', 'accessory': 'אביזר', 'post': 'עמוד', 'beam': 'קורה',
    'seal': 'אטם', 'railing': 'מעקה', 'mesh': 'רשת', 'panel': 'פנל', 'other': '',
}


def _line_role(line):
    """The profile's role in the ordered series (e.g. sash), or its first role."""
    sp = None
    if line.series_id:
        sp = line.profile.series_profiles.filter(series_id=line.series_id).first()
    if sp is None:
        sp = line.profile.series_profiles.first()
    return sp.role if sp else ''


def _prof_name(line):
    """A rich Hebrew description: role + series + the profile's own description,
    e.g. 'כנף 1700 עליון תחתון'. Falls back to the profile number."""
    role = ROLE_HE.get(_line_role(line), '')
    series = line.series.code if line.series_id else ''
    desc = (line.profile.description or '').strip()
    parts = [p for p in (role, series, desc) if p]
    return ' '.join(parts) or line.profile.number


def _line_image(line, max_mm=13):
    """A small reportlab Image of the profile's section, or None."""
    field = getattr(line.profile, 'section_image', None)
    if not field:
        return None
    try:
        field.open('rb')
        data = field.read()
        field.close()
    except Exception:                                     # noqa: BLE001
        return None
    if not data:
        return None
    from io import BytesIO
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image
    try:
        iw, ih = ImageReader(BytesIO(data)).getSize()
        if not iw or not ih:
            return None
        side = max_mm * mm
        # Fit inside a square box, preserving aspect ratio.
        if iw >= ih:
            w, h = side, side * ih / iw
        else:
            w, h = side * iw / ih, side
        return Image(BytesIO(data), width=w, height=h, hAlign='CENTER')
    except Exception:                                     # noqa: BLE001
        return None


# ─────────────────────────── order note ───────────────────────────
def build_order_note_pdf(order, company=None):
    font = _font('he')
    company = company or {}

    def s(txt):
        return _shape(txt)

    right = ParagraphStyle('r', fontName=font, fontSize=9.5, alignment=TA_RIGHT, leading=14)
    thanks = ParagraphStyle('th', fontName=font, fontSize=11, alignment=TA_CENTER,
                            textColor=PRIMARY, leading=15)

    doc, path = _new_doc('order_note_', order)
    story = _he_header(company, 'הזמנה', _detail_rows(order))

    status = order.status
    badge = Table([[Paragraph(
        f'<font color="white">{s("סטטוס")}: {s(_STATUS_HE.get(status, status))}</font>',
        ParagraphStyle('b', fontName=font, fontSize=9.5, alignment=TA_RIGHT))]],
        colWidths=[50 * mm], hAlign='RIGHT')
    badge.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(_STATUS_COLOR.get(status, '#6b7280'))),
        ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10), ('LEFTPADDING', (0, 0), (-1, -1), 10),
    ]))

    client_lines, supplier = _addr_blocks_for(order, company)
    story += _he_addr_blocks('פרטי הלקוח', client_lines, 'מאת (הספק)', supplier)

    # columns (RTL): total | price/kg | weight | metres | description | code | photo
    data = [[s('סה"כ'), s('₪/ק"ג'), s('משקל ק"ג'), s('מטרים'), s('תיאור'),
             s('מק"ט'), s('תמונה')]]
    style = [
        ('FONTNAME', (0, 0), (-1, -1), font), ('FONTSIZE', (0, 0), (-1, -1), 9.5),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (3, -1), 'CENTER'),
        ('ALIGN', (4, 0), (4, -1), 'RIGHT'),
        ('ALIGN', (5, 0), (6, -1), 'CENTER'),
        ('BACKGROUND', (0, 0), (-1, 0), PRIMARY), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, LINE),
    ]
    r = 1
    for line in order.lines.all():
        weight = line.effective_weight_kg or Decimal('0')
        data.append([
            _money(line.line_total), _money(line.price_per_kg), _num(weight),
            _num(line.total_length_m),
            Paragraph(_rtl(_prof_name(line)), right), s(line.profile.number),
            _line_image(line) or ''])
        if r % 2 == 0:
            style.append(('BACKGROUND', (0, r), (-1, r), ZEBRA))
        r += 1
    table = Table(data,
                  colWidths=[22 * mm, 18 * mm, 18 * mm, 16 * mm, None, 20 * mm, 16 * mm],
                  repeatRows=1)
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 5 * mm)]

    # totals: subtotal, discount, VAT, grand total
    muted_r = ParagraphStyle('sub', fontName=font, fontSize=10, alignment=TA_RIGHT,
                             leading=16, textColor=MUTED)
    if order.discount_percent:
        story += [
            Paragraph(f'{s("סה\"כ ביניים")}: {_money(order.subtotal)} ₪', muted_r),
            Paragraph(f'{s("הנחה")} ({_num(order.discount_percent)}%): '
                      f'-{_money(order.discount_amount)} ₪', muted_r),
        ]
    story.append(Paragraph(
        f'{s("מע\"מ")} ({_num(order.vat_percent)}%): {_money(order.vat_amount)} ₪', muted_r))
    story.append(Paragraph(
        f'{s("משקל כולל")}: {_num(order.total_weight_kg)} {s("ק\"ג")}', muted_r))
    story.append(Spacer(1, 2 * mm))

    total_para = Paragraph(
        f'<font color="white">{s("סה״כ לתשלום")}:&nbsp;&nbsp;{_money(order.total)} ₪</font>',
        ParagraphStyle('tot', fontName=font, fontSize=13, alignment=TA_RIGHT, leading=17))
    totals = Table([[total_para]], colWidths=[80 * mm], hAlign='LEFT')
    totals.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), PRIMARY),
                                ('TOPPADDING', (0, 0), (-1, -1), 9),
                                ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
                                ('RIGHTPADDING', (0, 0), (-1, -1), 14)]))
    row = Table([[totals, badge]], colWidths=[90 * mm, None])
    row.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                             ('LEFTPADDING', (0, 0), (-1, -1), 0),
                             ('RIGHTPADDING', (0, 0), (-1, -1), 0)]))
    story.append(row)

    if order.notes.strip():
        story += [Spacer(1, 7 * mm),
                  Paragraph(f'<b>{s("הערות")}:</b> {_rtl(order.notes)}', right)]
    story += [Spacer(1, 10 * mm), Paragraph(s('תודה שבחרתם בנו!'), thanks)]

    ftr = _footer_drawer(company, font)
    doc.build(story, onFirstPage=ftr, onLaterPages=ftr)
    return path


# ─────────────────────────── delivery note ───────────────────────────
def build_delivery_note_pdf(order, company=None):
    font = _font('he')
    company = company or {}

    def s(txt):
        return _shape(txt)

    right = ParagraphStyle('r', fontName=font, fontSize=9.5, alignment=TA_RIGHT, leading=14)
    center = ParagraphStyle('ct', fontName=font, fontSize=8, alignment=TA_CENTER,
                            textColor=MUTED, leading=12)
    thanks = ParagraphStyle('th', fontName=font, fontSize=11, alignment=TA_CENTER,
                            textColor=PRIMARY, leading=15)

    doc, path = _new_doc('delivery_note_', order)
    story = _he_header(company, 'תעודת משלוח', _detail_rows(order))

    client_lines, supplier = _addr_blocks_for(order, company)
    ship = [order.client.name, order.client.contact_name,
            order.client.delivery_address or order.client.address,
            order.client.city, order.client.phone]
    story += _he_addr_blocks('כתובת למשלוח', ship, 'מאת (הספק)', supplier)

    # columns (RTL): weight | delivered | ordered(metres) | description | code | photo
    data = [[s('משקל ק"ג'), s('סופק'), s('הוזמן (מ׳)'), s('תיאור'), s('מק"ט'), s('תמונה')]]
    style = [
        ('FONTNAME', (0, 0), (-1, -1), font), ('FONTSIZE', (0, 0), (-1, -1), 9.5),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (2, -1), 'CENTER'),
        ('ALIGN', (3, 0), (3, -1), 'RIGHT'),
        ('ALIGN', (4, 0), (5, -1), 'CENTER'),
        ('BACKGROUND', (0, 0), (-1, 0), PRIMARY), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, LINE),
    ]
    r = 1
    for line in order.lines.all():
        weight = line.effective_weight_kg or Decimal('0')
        # 'Supplied' shows how much was loaded; blank line to fill in if not set.
        supplied = (_num(line.delivered_length_m)
                    if line.delivered_length_m is not None else '____')
        # Reshape each Hebrew part separately so the HTML tags stay intact.
        desc_html = _rtl(_prof_name(line))
        if line.shortage_note:
            note = _rtl('חוסר: ' + line.shortage_note)
            desc_html += f'<br/><font size="8" color="#b3261e">{note}</font>'
        data.append([_num(weight), supplied, _num(line.total_length_m),
                     Paragraph(desc_html, right), s(line.profile.number),
                     _line_image(line) or ''])
        if r % 2 == 0:
            style.append(('BACKGROUND', (0, r), (-1, r), ZEBRA))
        r += 1
    table = Table(data, colWidths=[20 * mm, 16 * mm, 20 * mm, None, 22 * mm, 16 * mm],
                  repeatRows=1)
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 4 * mm)]

    story.append(Paragraph(
        f'{s("משקל כולל")}: {_num(order.total_weight_kg)} {s("ק\"ג")}', right))
    story.append(Spacer(1, 6 * mm))

    if order.notes.strip():
        story += [Paragraph(f'<b>{s("הערות")}:</b> {_rtl(order.notes)}', right),
                  Spacer(1, 5 * mm)]

    # recipient signature box
    signer = order.client.contact_name or order.client.name
    box_inner = [Paragraph(s('חתימת המקבל'),
                           ParagraphStyle('sl', fontName=font, fontSize=9.5,
                                          alignment=TA_RIGHT, textColor=MUTED, leading=13))]
    sig = Table([[box_inner]], colWidths=[85 * mm], rowHeights=[30 * mm], hAlign='RIGHT')
    sig.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.7, LINE), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ]))
    story += [
        Paragraph(f'{s("שם המקבל")}: {_rtl(signer)}', right),
        Spacer(1, 4 * mm), sig, Spacer(1, 3 * mm),
        Paragraph(f'{s("תאריך")}: ____________________', right),
        Spacer(1, 8 * mm),
    ]

    for ln in _DN_NOTICE:
        story.append(Paragraph(s(ln), center))
    story += [Spacer(1, 3 * mm), Paragraph(s('תודה שבחרתם בנו!'), thanks)]

    ftr = _footer_drawer(company, font)
    doc.build(story, onFirstPage=ftr, onLaterPages=ftr)
    return path


# ─────────────────────────── entry points ───────────────────────────
def company_from_shop():
    """Build the supplier block from the Shop singleton."""
    from core.models import Shop
    shop = Shop.get()
    logo = None
    if shop.logo:
        # Read the bytes now so the PDF renderer is storage-agnostic (works the
        # same for a local file or a Cloudinary URL).
        try:
            shop.logo.open('rb')
            logo = shop.logo.read()
        except Exception:                                 # noqa: BLE001
            logo = None
        finally:
            try:
                shop.logo.close()
            except Exception:                             # noqa: BLE001
                pass
    return {
        'name': shop.name, 'address': shop.address, 'city': shop.city,
        'phone': shop.phone, 'email': shop.email, 'tax_id': shop.tax_id,
        'logo': logo,
    }


def logo_flowable(company, max_h=15 * mm, max_w=52 * mm):
    """A reportlab Image for the company logo, scaled to fit, or None.

    Shared by the order/delivery and payslip headers. Accepts the raw image
    bytes in company['logo']; returns None (never raises) if there is no logo or
    the bytes can't be read as an image.
    """
    data = company.get('logo') if company else None
    if not data:
        return None
    from io import BytesIO
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image
    try:
        iw, ih = ImageReader(BytesIO(data)).getSize()
        if not iw or not ih:
            return None
        w, h = max_h * iw / ih, max_h
        if w > max_w:
            w, h = max_w, max_w * ih / iw
        return Image(BytesIO(data), width=w, height=h, hAlign='LEFT')
    except Exception:                                     # noqa: BLE001
        return None


def _read_and_clean(path):
    with open(path, 'rb') as fh:
        data = fh.read()
    try:
        os.remove(path)
    except OSError:
        pass
    return data


def render_order_note(order, company=None):
    return _read_and_clean(build_order_note_pdf(order, company=company))


def render_delivery_note(order, company=None):
    return _read_and_clean(build_delivery_note_pdf(order, company=company))
