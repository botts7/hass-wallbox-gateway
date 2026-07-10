"""Release-default enforcer decision (pure) — charge_assistant.release_action.

Verifies the safety gating for "return the charger to a default when the
integration/add-on has been controlling but isn't now":
  - 'keep' never acts; while controlling, never acts.
  - only acts when the gateway still credits US (integration/addon) as owner —
    a manual / native-schedule / none-owner charge is never touched.
  - 'stop' only acts when actually charging; resume_* act regardless.

Needs Home Assistant importable (imports the integration module); self-skips
otherwise so the bare pure-logic suite still runs.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from custom_components.wallbox_gateway.charge_assistant import (
        release_action,
        _DEFAULT_RELEASE,
    )
    from custom_components.wallbox_gateway.const import (
        RELEASE_KEEP,
        RELEASE_RESUME_ECO,
        RELEASE_RESUME_SCHEDULE,
        RELEASE_STOP,
    )

    _HA = True
except Exception:  # noqa: BLE001 - any import failure => skip in bare envs
    _HA = False


def _build_cases():
    K, S, SCH, ECO = (
        RELEASE_KEEP,
        RELEASE_STOP,
        RELEASE_RESUME_SCHEDULE,
        RELEASE_RESUME_ECO,
    )
    # (controlling, default, owner, is_charging) -> expected action (or None)
    return [
        # 'keep' never acts; actively controlling never acts
        ((False, K, "integration", True), None),
        ((True, S, "integration", True), None),
        ((True, SCH, "integration", False), None),
        # 'stop' — only when we own it AND it's charging
        ((False, S, "integration", True), S),
        ((False, S, "integration", False), None),   # nothing to stop
        ((False, S, "addon", True), S),             # add-on owner also acts
        ((False, S, "manual", True), None),         # manual charge — never touch
        ((False, S, "none", True), None),
        ((False, S, "wallbox_schedule", True), None),  # native schedule — never touch
        ((False, S, "", True), None),               # unknown owner — never touch
        # resume_schedule / resume_eco — act when owned + not controlling
        ((False, SCH, "integration", False), SCH),
        ((False, SCH, "integration", True), SCH),
        ((False, SCH, "manual", False), None),
        ((False, ECO, "integration", False), ECO),
        ((False, ECO, "addon", True), ECO),
        ((False, ECO, "none", False), None),
    ]


CASES = _build_cases() if _HA else []


def main():
    if not _HA:
        print("  (HA not importable — skipped)")
        return
    for args, expected in CASES:
        got = release_action(*args)
        assert got == expected, (
            f"release_action{args} -> {got!r}, expected {expected!r}"
        )
    # Default-when-unset is resume_eco (hand the charger back to its own
    # schedule/solar rather than leaving it paused). Degrades gracefully on
    # non-solar chargers — see _resume_and_restore_eco.
    assert _DEFAULT_RELEASE == RELEASE_RESUME_ECO, (
        f"default release should be resume_eco, got {_DEFAULT_RELEASE!r}"
    )
    print(f"  ok  release_action gating — {len(CASES)} combinations")
    print("  ok  default release-default is resume_eco")


if __name__ == "__main__":
    main()
