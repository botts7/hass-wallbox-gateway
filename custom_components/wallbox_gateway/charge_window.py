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
