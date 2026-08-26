"""attendance_auto_close.py — close shifts a worker forgot to clock out of.

A shift left open past the stale threshold (no human is really on shift that
long) is closed by the system: clock_out is set equal to clock_in, so it books
zero hours rather than paying a runaway open shift, the shift is flagged
`auto_closed`, and a pending correction request is raised so the worker can
submit the real end time and the manager can approve it.

Entry points:
  • close_one(shift)              — close one specific stale shift
  • close_stale_for_worker(user) — close a worker's stale shift (lazy check on
                                    clock-in / when the app polls the clock)
  • close_all_stale()            — sweep every worker (periodic command)

Mirrors the TruckForce driver-attendance auto-close.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import Shift, ShiftCorrectionRequest

# How long after clock-in a still-open shift is treated as "forgotten".
STALE_SHIFT_HOURS = getattr(settings, 'STALE_SHIFT_HOURS', 17)


def _is_stale(shift, threshold_hours=STALE_SHIFT_HOURS):
    """True if the shift is open and older than the threshold."""
    if shift.clock_out is not None:
        return False
    return (timezone.now() - shift.clock_in) > timedelta(hours=threshold_hours)


@transaction.atomic
def close_one(shift, threshold_hours=STALE_SHIFT_HOURS):
    """Close a single stale shift. Returns True if this call closed it.

    Atomic + SELECT FOR UPDATE so two concurrent callers can't both close the
    same row (the second sees clock_out already set).
    """
    shift = Shift.objects.select_for_update().get(pk=shift.pk)
    if not _is_stale(shift, threshold_hours):
        return False

    # Zero-hour shift: we have no evidence of the real end time, so we don't
    # pay for it. The worker supplies it through the correction flow.
    shift.clock_out = shift.clock_in
    shift.auto_closed = True
    shift.save(update_fields=['clock_out', 'auto_closed'])

    # A pending correction so the manager sees it and the worker fills in the
    # real clock-out. Skipped if one already exists for that day (idempotent on
    # repeated sweeps).
    work_date = timezone.localtime(shift.clock_in).date()
    reason = _(
        'Auto-closed: shift stayed open more than %(h)dh with no clock-out. '
        'Enter your real clock-out time.'
    ) % {'h': threshold_hours}
    ShiftCorrectionRequest.objects.get_or_create(
        worker=shift.worker,
        work_date=work_date,
        status=ShiftCorrectionRequest.Status.PENDING,
        defaults={'shift': shift, 'reason': reason},
    )
    return True


def close_stale_for_worker(user, threshold_hours=STALE_SHIFT_HOURS):
    """Close a specific worker's stale open shifts. Returns how many closed.

    Called just-in-time before clock-in decides "already clocked in", and when
    the app polls the clock, so a forgotten shift is resolved the moment the
    worker comes back rather than only on the nightly sweep.
    """
    cutoff = timezone.now() - timedelta(hours=threshold_hours)
    candidates = Shift.objects.filter(
        worker=user, clock_out__isnull=True, clock_in__lte=cutoff)
    closed = 0
    for shift in candidates:
        try:
            if close_one(shift, threshold_hours):
                closed += 1
        except Exception as exc:  # noqa: BLE001 - never block the caller
            print(f'[AUTO-CLOSE] failed for shift {shift.pk}: {exc}', flush=True)
    return closed


def close_all_stale(threshold_hours=STALE_SHIFT_HOURS):
    """Close every stale open shift across all workers. Returns how many."""
    cutoff = timezone.now() - timedelta(hours=threshold_hours)
    candidates = Shift.objects.filter(
        clock_out__isnull=True, clock_in__lte=cutoff).select_related('worker')
    closed = 0
    for shift in candidates:
        try:
            if close_one(shift, threshold_hours):
                closed += 1
        except Exception as exc:  # noqa: BLE001
            print(f'[AUTO-CLOSE] failed for shift {shift.pk}: {exc}', flush=True)
    return closed
