"""Notification payload pieces for the auto-start countdown (grace).

22 Nov 2026: a "Charging will start in 5 min" countdown reached the phone about
7 minutes after it was sent (Android held the normal-priority push while the
phone idled), and after it was cancelled it stayed on the phone saying a
charge was coming. Countdown notifications are now sent as time-sensitive
(Android: priority high + ttl 0; iOS: interruption level time-sensitive) and
tagged, so a cancelled countdown is cleared and a started one is replaced by
"Charging now". The HA companion app understands all of these keys; other
notify services ignore what they don't know.

Only a countdown outside the cheap window is urgent (a daytime top-up the
user may want to stop). The nightly in-window countdown (~02:50) stays normal
priority, tagged only, so it never wakes anyone.

Pure, HA-free; charge_assistant.py does the I/O.
"""

from __future__ import annotations

GRACE_TAG = "wb_ca_grace"


def tagged(data: dict, tag: str = GRACE_TAG) -> dict:
    """``data`` with the tag only: replaceable / clearable, normal priority."""
    return {**data, "tag": tag}


def time_sensitive(data: dict, tag: str = GRACE_TAG) -> dict:
    """``data`` plus the keys that make a companion-app notification urgent and
    replaceable."""
    return {**data, "tag": tag, "priority": "high", "ttl": 0,
            "push": {"interruption-level": "time-sensitive"}}


def clear(tag: str = GRACE_TAG) -> dict:
    """The companion-app payload that removes a tagged notification."""
    return {"message": "clear_notification", "data": {"tag": tag}}
