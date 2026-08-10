"""Salary from clocked hours, under Israeli overtime law.

Overtime is reckoned per day against a daily norm (default 8 hours):
    - hours up to the norm       -> 100%
    - the next 2 hours           -> 125%
    - anything beyond that       -> 150%

Pay basis decides the base:
    - hourly   : regular hours x hourly_rate
    - daily    : days worked x daily_rate  (base covers the regular hours)
    - monthly  : the fixed monthly salary  (base covers the regular hours)

When the worker's overtime toggle is off, every hour is paid flat with no
premium (and for daily/monthly there is simply no overtime line).

The hourly rate that overtime is priced from is the worker's own for hourly
pay, daily_rate / daily_norm for daily pay, and monthly_salary / 182 for
monthly pay (182 = full-time monthly hours in Israel since 2018).
"""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from core.models import MONTHLY_HOURS, PayBasis

_2 = Decimal('0.01')
OT1_RATE = Decimal('1.25')
OT2_RATE = Decimal('1.50')


def _money(value):
    return Decimal(value).quantize(_2, rounding=ROUND_HALF_UP)


def _hours(value):
    return Decimal(value).quantize(_2, rounding=ROUND_HALF_UP)


def _daily_split(day_hours, norm, overtime_enabled):
    """Split one day's hours into regular / 125% / 150% buckets."""
    if not overtime_enabled:
        return day_hours, Decimal('0'), Decimal('0')
    regular = min(day_hours, norm)
    ot125 = min(max(day_hours - norm, Decimal('0')), Decimal('2'))
    ot150 = max(day_hours - norm - Decimal('2'), Decimal('0'))
    return regular, ot125, ot150


def _hourly_rate(worker):
    """The rate overtime is priced from, per the worker's pay basis."""
    norm = worker.daily_regular_hours or Decimal('8')
    if worker.pay_basis == PayBasis.HOURLY:
        return worker.hourly_rate or Decimal('0')
    if worker.pay_basis == PayBasis.DAILY:
        return (worker.daily_rate or Decimal('0')) / norm
    return (worker.monthly_salary or Decimal('0')) / MONTHLY_HOURS


def compute_payroll(worker, shifts):
    """Return a pay breakdown for `worker` over the given closed `shifts`.

    `shifts` is any iterable of Shift; open shifts (no clock_out) are skipped so
    a half-finished shift never inflates pay.
    """
    norm = worker.daily_regular_hours or Decimal('8')
    overtime = worker.overtime_enabled

    # Sum hours per calendar day (local time), so overtime is per day.
    per_day = defaultdict(Decimal)
    for shift in shifts:
        if shift.clock_out is None:
            continue
        minutes = Decimal(shift.duration_minutes)
        day = timezone.localtime(shift.clock_in).date()
        per_day[day] += minutes / Decimal('60')

    reg = ot125 = ot150 = Decimal('0')
    for day_hours in per_day.values():
        r, o1, o2 = _daily_split(day_hours, norm, overtime)
        reg += r
        ot125 += o1
        ot150 += o2

    total_hours = reg + ot125 + ot150
    days_worked = len(per_day)
    rate = _hourly_rate(worker)

    if worker.pay_basis == PayBasis.HOURLY:
        base_pay = reg * (worker.hourly_rate or Decimal('0'))
    elif worker.pay_basis == PayBasis.DAILY:
        base_pay = Decimal(days_worked) * (worker.daily_rate or Decimal('0'))
    else:  # monthly
        base_pay = worker.monthly_salary or Decimal('0')

    overtime_pay = ot125 * rate * OT1_RATE + ot150 * rate * OT2_RATE
    total_pay = base_pay + overtime_pay

    return {
        'pay_basis': worker.pay_basis,
        'overtime_enabled': overtime,
        'days_worked': days_worked,
        'regular_hours': float(_hours(reg)),
        'overtime_125_hours': float(_hours(ot125)),
        'overtime_150_hours': float(_hours(ot150)),
        'total_hours': float(_hours(total_hours)),
        'hourly_rate': float(_money(rate)),
        'base_pay': float(_money(base_pay)),
        'overtime_pay': float(_money(overtime_pay)),
        'total_pay': float(_money(total_pay)),
    }
