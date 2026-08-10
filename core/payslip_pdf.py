"""A printable payslip (תלוש שכר), Hebrew RTL, styled like the order notes."""
import os
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from core.order_pdf import (
    LINE, MUTED, PRIMARY, ZEBRA, _font, _new_doc, _read_and_clean, _rtl, _shape,
)

_MONTHS_HE = ['', 'ינואר', 'פברואר', 'מרץ', 'אפריל', 'מאי', 'יוני', 'יולי',
              'אוגוסט', 'ספטמבר', 'אוקטובר', 'נובמבר', 'דצמבר']
_BASIS_HE = {'hourly': 'לפי שעה', 'daily': 'לפי יום', 'monthly': 'חודשי'}


def _money(v):
    try:
        return f'{float(v or 0):,.2f}'
    except (TypeError, ValueError):
        return str(v)


def _num(v):
    try:
        return f'{float(v):g}'
    except (TypeError, ValueError):
        return str(v)


def build_payslip_pdf(payslip, company=None):
    font = _font('he')
    company = company or {}
    w = payslip.worker

    def s(txt):
        return _shape(txt)

    right = ParagraphStyle('r', fontName=font, fontSize=9.5, alignment=TA_RIGHT, leading=14)
    doc, path = _new_doc('payslip_', _Named(f'{payslip.year}-{payslip.month:02d}'))

    period = f'{_MONTHS_HE[payslip.month]} {payslip.year}'
    detail_rows = [
        ('עובד', w.full_name),
        ("ת.ז.", w.id_number or ''),
        ('תקופה', period),
        ('בסיס שכר', _BASIS_HE.get(payslip.pay_basis, payslip.pay_basis)),
    ]
    story = _header(company, 'תלוש שכר', detail_rows)

    # earnings table: amount | detail
    data = [[s('סכום (₪)'), s('פירוט')]]
    style = [
        ('FONTNAME', (0, 0), (-1, -1), font), ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'), ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('BACKGROUND', (0, 0), (-1, 0), PRIMARY), ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('TOPPADDING', (0, 0), (-1, -1), 6), ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('LINEBELOW', (0, 1), (-1, -1), 0.4, LINE),
    ]

    def line(label, amount):
        data.append([_money(amount), Paragraph(_rtl(label), right)])

    base_label = {
        'hourly': f'שכר בסיס ({_num(payslip.regular_hours)} שעות × {_money(payslip.hourly_rate)})',
        'daily': f'שכר בסיס ({payslip.days_worked} ימים)',
        'monthly': 'שכר בסיס (חודשי)',
    }.get(payslip.pay_basis, 'שכר בסיס')
    line(base_label, payslip.base_pay)
    if payslip.overtime_pay:
        ot = f'שעות נוספות ({_num(payslip.overtime_125_hours)}×125%'
        if payslip.overtime_150_hours:
            ot += f', {_num(payslip.overtime_150_hours)}×150%'
        ot += ')'
        line(ot, payslip.overtime_pay)
    for adj in payslip.adjustments.all():
        line(adj.label, adj.amount)

    r = 1
    for _ in range(len(data) - 1):
        if r % 2 == 0:
            style.append(('BACKGROUND', (0, r), (-1, r), ZEBRA))
        r += 1
    table = Table(data, colWidths=[35 * mm, None], repeatRows=1)
    table.setStyle(TableStyle(style))
    story += [table, Spacer(1, 6 * mm)]

    # summary line: hours and total
    story.append(Paragraph(
        f'{s("סה\"כ שעות")}: {_num(payslip.total_hours)} · '
        f'{s("ימי עבודה")}: {payslip.days_worked}', right))
    story.append(Spacer(1, 3 * mm))

    total_para = Paragraph(
        f'<font color="white">{s("סה״כ לתשלום")}:&nbsp;&nbsp;{_money(payslip.total_pay)} ₪</font>',
        ParagraphStyle('tot', fontName=font, fontSize=13, alignment=TA_RIGHT, leading=17))
    totals = Table([[total_para]], colWidths=[80 * mm], hAlign='LEFT')
    totals.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), PRIMARY),
                                ('TOPPADDING', (0, 0), (-1, -1), 9),
                                ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
                                ('RIGHTPADDING', (0, 0), (-1, -1), 14)]))
    story.append(totals)

    if payslip.note.strip():
        story += [Spacer(1, 6 * mm),
                  Paragraph(f'<b>{s("הערות")}:</b> {_rtl(payslip.note)}', right)]

    from core.order_pdf import _footer_drawer
    ftr = _footer_drawer(company, font)
    doc.build(story, onFirstPage=ftr, onLaterPages=ftr)
    return path


def _header(company, title_text, detail_rows):
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

    from core.order_pdf import logo_flowable

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
    if company.get('tax_id'):
        comp_cell.append(Paragraph(f'{s("ח.פ/עוסק")}: {_rtl(company["tax_id"])}', mut_L))
    if not comp_cell:
        comp_cell.append(Paragraph(s('AlomForce'), comp_L))

    det_data = [[Paragraph(_rtl(v), val_st), Paragraph(f'{s(k)}:', label_st)]
                for k, v in detail_rows]
    details = Table(det_data, colWidths=[42 * mm, 28 * mm], hAlign='RIGHT')
    details.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 2), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
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


class _Named:
    """_new_doc only needs a .number for the PDF title."""
    def __init__(self, number):
        self.number = number


def render_payslip(payslip, company=None):
    return _read_and_clean(build_payslip_pdf(payslip, company=company))
