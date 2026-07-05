"""Validate entity descriptions against Home Assistant's own rules.

Catches the class of bug where a sensor's state_class is illegal for its
device_class — e.g. an energy sensor with state_class=measurement, which HA
rejects at runtime with a log spam ("...is using state class 'measurement'
which is impossible considering device class ('energy')...", forum/#75-era).

Runs only when Home Assistant is importable; self-skips otherwise so the
pure-logic suite still runs in a bare environment.
"""

import os
import sys

# run_all.py only puts tests/ on sys.path; add the repo root so the real
# integration package (custom_components/) is importable here.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from homeassistant.components.sensor import DEVICE_CLASS_STATE_CLASSES
    from custom_components.wallbox_gateway.sensor import SENSORS

    _HA = True
except Exception:  # noqa: BLE001 - any import failure => skip in bare envs
    _HA = False


def _check_sensor_state_classes():
    """Every sensor's state_class must be legal for its device_class."""
    bad = []
    for d in SENSORS:
        dc = getattr(d, "device_class", None)
        sc = getattr(d, "state_class", None)
        # Only device classes HA constrains are checked; None state_class is
        # always allowed (a device-class sensor with no long-term statistics).
        if dc is None or dc not in DEVICE_CLASS_STATE_CLASSES:
            continue
        allowed = DEVICE_CLASS_STATE_CLASSES[dc]
        if sc is not None and sc not in allowed:
            bad.append(
                f"{d.key}: device_class={dc} state_class={sc} "
                f"(allowed: {sorted(str(a) for a in allowed)} or None)"
            )
    assert not bad, "Invalid device_class/state_class combos:\n  " + "\n  ".join(bad)


CASES = [_check_sensor_state_classes] if _HA else []


def main():
    if not _HA:
        print("  (HA not importable — skipped)")
        return
    for case in CASES:
        case()
        print(f"  ok  {case.__name__} ({len(SENSORS)} sensors)")


if __name__ == "__main__":
    main()
