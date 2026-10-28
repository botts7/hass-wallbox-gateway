"""What to do while a charge the assistant started is getting going.

Measured on a Pulsar MAX with a BYD (27-28 Oct 2026), the charger reports
3 -> 2 ("waiting for car") -> 1 (charging) and the car starts drawing 10-16 s
after the start. The old watcher re-asserted at 12 s: on the MAX a start sent
to a session in status 2 PAUSES it (start is a toggle), which interrupted the
car mid-handshake, and the car then sometimes stopped requesting charge until
it was replugged. Re-asserted every 10 minutes, the charger went into Error.

So the rules:
- status 2 / 3 (handshake in progress): never send anything, just wait.
- status 1 or power flowing: done.
- status 4 / 18 (the charger took it back, e.g. Eco-Smart re-queued it): one
  re-assert, and only once.
- status 7 (Error): stop at once.
- still not charging at the last check: give up cleanly.

Pure, HA-free; charge_assistant.py does the I/O.
"""

from __future__ import annotations

# Seconds between checks, from the start: checks at 12, 30, 60 and 90 s.
CHECK_DELAYS_S = (12, 18, 30, 30)

WAITING = frozenset({2, 3})     # handshake in progress: never interrupt
STARTED = frozenset({1, 2})     # a started session: a stop is safe (measured on a MAX)
TAKEN_BACK = frozenset({4, 18})  # paused / queued by Eco-Smart
ERROR = 7

DONE = "done"
WAIT = "wait"
REASSERT = "reassert"
GIVE_UP = "give_up"
FAULT = "error"


def outcome(code: int | None, charging: bool, check_index: int, reasserted: bool) -> str:
    """Decision at check number ``check_index`` (0-based) of CHECK_DELAYS_S."""
    if charging or code == 1:
        return DONE
    if code == ERROR:
        return FAULT
    last = check_index >= len(CHECK_DELAYS_S) - 1
    if code in TAKEN_BACK and not reasserted and not last:
        return REASSERT
    if last:
        return GIVE_UP
    return WAIT


def give_up_message(code: int | None) -> str:
    """One notification for a start that didn't take, by what the charger shows."""
    if code in WAITING:
        return ("The charger is ready but the car isn't taking charge. Unplug and "
                "replug the car to charge; I won't try again until you do.")
    if code in TAKEN_BACK:
        return ("Tried to start charging but the charger keeps pausing it (Eco-Smart / "
                "Solar-Only may be on). I won't try again until the car is unplugged.")
    return ("Tried to start charging but it didn't start. I won't try again until the "
            "car is unplugged.")


ERROR_MESSAGE = ("The charger reports an error, so charging is stopped. Unplug and replug "
                 "the car, or restart the charger from the Wallbox app.")
