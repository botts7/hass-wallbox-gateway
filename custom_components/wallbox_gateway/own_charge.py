"""Is the charge running after an HA restart one the Charge Assistant started?

29 Oct live test: HA restarted 26 s after the window end, before the next
check. The assistant came back not knowing the charge was its own, so the
window-end stop never happened and the car kept charging at peak rates.

So the assistant saves a small record when it starts a charge (and clears it
when it stops one), and after a restart takes the charge back only when every
sign agrees it is still that same charge:
- the record is recent (MAX_AGE_S),
- the car is plugged in and charging,
- the gateway still credits the integration with the last command,
- the charger is still in manual mode (our start sets it; Eco-Smart's own
  charging and our hand-back leave it in mode 0),
- and the charger's session id matches, when both are known.
Anything else is left alone, as before.

Pure, HA-free; charge_assistant.py does the I/O.
"""

from __future__ import annotations

OWNER = "integration"
MAX_AGE_S = 24 * 3600

KEEP = "keep"      # charge is ours: take it back
DROP = "drop"      # not ours, or over: forget the record
WAIT = "wait"      # charger data not loaded yet: decide on a later tick


def record(at_s: float, usid: int | None = None) -> dict:
    """The saved record for a charge we started at ``at_s`` (epoch seconds)."""
    return {"at": at_s, "usid": usid}


def decide(saved: dict | None, *, now_s: float, loaded: bool, charging: bool,
           plugged: bool | None, last_by: str | None, manual: bool,
           usid: int | None) -> str:
    """What to do with a record restored after a restart."""
    if not isinstance(saved, dict):
        return DROP
    if not loaded:
        return WAIT
    try:
        age = now_s - float(saved.get("at"))
    except (TypeError, ValueError):
        return DROP
    if not 0 <= age <= MAX_AGE_S:
        return DROP
    if not charging or plugged is not True:
        return DROP
    if (last_by or "") != OWNER or not manual:
        return DROP
    saved_usid = saved.get("usid")
    if saved_usid is not None and usid is not None and saved_usid != usid:
        return DROP
    return KEEP
