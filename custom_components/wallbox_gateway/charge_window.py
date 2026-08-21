"""Allowed charging-window logic for the Charge Assistant — pure & testable.

A user can restrict charging to a cheap window (e.g. 00:00–06:00) so the
assistant never charges during expensive hours. Two policies relax that:

  * ``overrun``  — keep charging *past* the window end if the SOC target isn't
                   reached yet (finish the job even if it runs a bit late).
  * ``prestart`` — start *before* the window if that's the only way to reach
                   the target by a departure deadline.

Whenever charging falls OUTSIDE the cheap window (a pre-start or an overrun),
``cost_warn`` is set so the controller can notify the user that the charge is
running during a pricier period — they stay informed and in control.

This module is deliberately free of any Home Assistant imports so it can be
unit-tested in isolation. Times are "minutes since local midnight" (0–1439).
Windows may wrap past midnight: ``start > end`` means the window spans midnight
(e.g. 23:00–07:00).
"""

from __future__ import annotations


def to_minutes(hhmm: str | None) -> int | None:
    """'HH:MM' (or 'HH:MM:SS') → minutes since midnight, or None if unparseable."""
    if not hhmm:
        return None
    parts = str(hhmm).split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def in_window(now_min: int, start_min: int | None, end_min: int | None) -> bool:
    """Is ``now`` within [start, end)? Handles midnight wrap. An unset or
    zero-length window is treated as 'always' (no restriction)."""
    if start_min is None or end_min is None or start_min == end_min:
        return True
    if start_min < end_min:
        return start_min <= now_min < end_min
    # wraps past midnight
    return now_min >= start_min or now_min < end_min


def hhmm_to_minutes(hhmm: int | str | None) -> int | None:
    """Native-schedule 'HHMM' clock value (e.g. 600 or "0600" = 06:00) →
    minutes since midnight, or None if unparseable/out of range. Distinct from
    ``to_minutes`` which parses the config UI's "HH:MM" strings."""
    if hhmm is None or hhmm == "":
        return None
    try:
        v = int(str(hhmm).strip())
    except (TypeError, ValueError):
        return None
    h, m = divmod(v, 100)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _segments(start_min: int, end_min: int) -> list[tuple[int, int]]:
    """Decompose a (possibly midnight-wrapping) [start, end) window into 1–2
    plain non-wrapping intervals on [0, 1440). A zero-length window covers
    nothing."""
    if start_min == end_min:
        return []
    if start_min < end_min:
        return [(start_min, end_min)]
    return [(start_min, 1440), (0, end_min)]  # wraps past midnight


def overlaps(
    a_start: int | None, a_end: int | None,
    b_start: int | None, b_end: int | None,
) -> bool:
    """Do two time-of-day windows [start, end) overlap on the 24-hour clock?

    Times are minutes since midnight; either window may wrap past midnight
    (``start > end``, e.g. 23:00–07:00). A window with an unset endpoint or a
    zero length overlaps nothing (there is no interval to intersect). Used to
    decide whether a native schedule falls inside the integration's active
    (daytime) window — a non-overlapping night schedule is left to run itself.
    """
    if a_start is None or a_end is None or b_start is None or b_end is None:
        return False
    for as_, ae in _segments(a_start, a_end):
        for bs, be in _segments(b_start, b_end):
            if as_ < be and bs < ae:
                return True
    return False


def evaluate(
    now_min: int,
    *,
    start: str | None,
    end: str | None,
    overrun: bool = False,
    prestart: bool = False,
    target_met: bool = False,
    minutes_to_departure: int | None = None,
    minutes_needed: int | None = None,
    already_charging: bool = False,
) -> dict:
    """Decide whether charging is allowed *right now* under the window policy.

    Returns a dict:
      in_window   — is ``now`` inside the configured window?
      allow_charge — may we charge at this instant?
      reason      — 'no_window' | 'in_window' | 'target_met' |
                    'prestart_for_departure' | 'overrun_to_target' | 'outside_window'
      cost_warn   — True when we're charging OUTSIDE the cheap window (pricier)

    ``target_met`` must be a known boolean from the SOC sensor; ``overrun``
    relies on it to stop (the UI should require a SOC entity before offering
    overrun, else it could charge indefinitely outside the window).
    """
    s = to_minutes(start)
    e = to_minutes(end)

    # No window configured → unrestricted, never a cost warning.
    if s is None or e is None or s == e:
        return {"in_window": True, "allow_charge": True,
                "reason": "no_window", "cost_warn": False}

    if in_window(now_min, s, e):
        return {"in_window": True, "allow_charge": True,
                "reason": "in_window", "cost_warn": False}

    # Outside the cheap window from here on.
    if target_met:
        # Already done — never charge outside the window.
        return {"in_window": False, "allow_charge": False,
                "reason": "target_met", "cost_warn": False}

    # Pre-start: must we begin now to be ready by departure?
    if (prestart and minutes_to_departure is not None
            and minutes_needed is not None
            and minutes_to_departure <= minutes_needed):
        return {"in_window": False, "allow_charge": True,
                "reason": "prestart_for_departure", "cost_warn": True}

    # Overrun: keep charging past the window to reach the target. This only
    # EXTENDS an already-running charge — it must never INITIATE a fresh charge
    # outside the cheap window (that would defeat the window entirely, letting a
    # below-target battery start at peak hours). Requires already_charging.
    if overrun and already_charging:
        return {"in_window": False, "allow_charge": True,
                "reason": "overrun_to_target", "cost_warn": True}

    return {"in_window": False, "allow_charge": False,
            "reason": "outside_window", "cost_warn": False}
