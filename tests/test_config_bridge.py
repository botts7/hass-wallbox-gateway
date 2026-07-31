"""Config-bridge allow-list (pure) — ca_config.sanitize_options.

Locks the set_config contract that the Add-on GUI, the get_config/set_config
services, and every v3.3 feature write through: exactly which top-level option
keys survive a set_config call, how they're coerced/clamped, and — the load-
bearing rule for the whole v3.3 roadmap — that UNKNOWN top-level keys are
dropped, so new tunables MUST nest under `charge_assistant`.

HA-free (ca_config imports nothing from Home Assistant), so this always runs.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from custom_components.wallbox_gateway.ca_config import (  # noqa: E402
    remove_car,
    sanitize_options,
    upsert_car,
)

# (label, incoming, expected_clean, expected_ignored_count)
CASES = [
    # --- poll_interval: int-coerced and clamped to [1, 3600] ---
    ("poll passthrough", {"poll_interval": 30}, {"poll_interval": 30}, 0),
    ("poll clamps low", {"poll_interval": 0}, {"poll_interval": 1}, 0),
    ("poll clamps high", {"poll_interval": 99999}, {"poll_interval": 3600}, 0),
    ("poll numeric string", {"poll_interval": "45"}, {"poll_interval": 45}, 0),
    ("poll float truncates", {"poll_interval": 12.9}, {"poll_interval": 12}, 0),
    ("poll non-numeric dropped", {"poll_interval": "soon"}, {}, 1),

    # --- charge_assistant / tariff: dicts kept wholesale, non-dicts dropped ---
    ("CA dict kept", {"charge_assistant": {"mode": "solar"}},
     {"charge_assistant": {"mode": "solar"}}, 0),
    ("CA non-dict dropped", {"charge_assistant": "solar"}, {}, 1),
    ("tariff dict kept", {"tariff": {"kind": "flat", "price": 0.3}},
     {"tariff": {"kind": "flat", "price": 0.3}}, 0),
    ("tariff non-dict dropped", {"tariff": 0.3}, {}, 1),

    # --- auto_resume_eco: coerced to bool ---
    ("auto_resume truthy", {"auto_resume_eco": 1}, {"auto_resume_eco": True}, 0),
    ("auto_resume falsy", {"auto_resume_eco": 0}, {"auto_resume_eco": False}, 0),

    # --- release_default: enum-validated ---
    ("release valid keep", {"release_default": "keep"},
     {"release_default": "keep"}, 0),
    ("release valid resume_eco", {"release_default": "resume_eco"},
     {"release_default": "resume_eco"}, 0),
    ("release invalid dropped", {"release_default": "explode"}, {}, 1),

    # --- THE CONTRACT: unknown top-level keys are dropped ---
    ("unknown top-level dropped", {"solar_window": {"start": "09:00"}}, {}, 1),
    ("new tunable must nest under CA",
     {"charge_assistant": {"solar_window": {"start": "09:00"}}},
     {"charge_assistant": {"solar_window": {"start": "09:00"}}}, 0),

    # --- mixed: valid kept, junk dropped, independently ---
    ("mixed valid+junk",
     {"poll_interval": 60, "bogus": 1, "auto_resume_eco": True},
     {"poll_interval": 60, "auto_resume_eco": True}, 1),

    # --- empty / None ---
    ("empty", {}, {}, 0),
    ("none", None, {}, 0),
]


def main():
    for label, incoming, expected_clean, expected_ignored in CASES:
        clean, ignored = sanitize_options(incoming)
        assert clean == expected_clean, (
            f"[{label}] clean={clean!r}, expected {expected_clean!r}"
        )
        assert len(ignored) == expected_ignored, (
            f"[{label}] ignored={ignored!r}, expected {expected_ignored} message(s)"
        )

    # The CA sub-dict is passed through by reference (replaced wholesale by the
    # caller), never deep-copied or merged key-by-key — a nested value the GUI
    # didn't send must NOT survive from the old config.
    ca = {"mode": "target_soc", "target_soc_pct": 80}
    clean, _ = sanitize_options({"charge_assistant": ca})
    assert clean["charge_assistant"] is ca, "CA object must pass through wholesale"

    # Every dropped key yields exactly one operator-facing message.
    _, ignored = sanitize_options({"a": 1, "b": 2, "poll_interval": 10})
    assert len(ignored) == 2 and all(isinstance(m, str) for m in ignored), (
        f"expected 2 string messages for 2 junk keys, got {ignored!r}"
    )

    print(f"  ok  sanitize_options allow-list — {len(CASES)} cases")
    print("  ok  charge_assistant passes through wholesale (no merge/leak)")
    print("  ok  each dropped key emits one log message")

    # ---- Vehicles editor list surgery (Options flow, #130 1b) ----
    a, b = {"name": "A"}, {"name": "B"}
    # append when index is None
    assert upsert_car([a], None, b) == [a, b]
    # replace at an existing index
    assert upsert_car([a, b], 0, {"name": "A2"}) == [{"name": "A2"}, b]
    # out-of-range index is a no-op (stale flow can't crash)
    assert upsert_car([a], 5, b) == [a]
    # None/empty base list
    assert upsert_car(None, None, a) == [a]
    # new list, not the same object (no in-place mutation of entry.options)
    base = [a]
    assert upsert_car(base, None, b) is not base and base == [a]
    # remove
    assert remove_car([a, b], 0) == [b]
    assert remove_car([a, b], 9) == [a, b]      # out of range → unchanged
    assert remove_car([a], None) == [a]         # None index → unchanged
    assert remove_car(None, 0) == []
    print("  ok  upsert_car / remove_car list surgery (append/replace/remove/guard)")


if __name__ == "__main__":
    main()
