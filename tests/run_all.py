"""Run all pure-logic unit tests for the integration (no Home Assistant needed).

These cover the charger-agnostic cores: the cheapest-window planner, the
charger-control adapter, and (added by later phases) the charge guards. The
HA-coupled controller paths are validated separately (on-hardware + a future
pytest-homeassistant harness). Run:  py tests/run_all.py
"""

import importlib
import os
import sys

# Preload stdlib that the integration package would shadow once its dir is on
# sys.path: it ships select.py / number.py (HA platforms) that otherwise mask
# the stdlib `select` asyncio imports lazily. Importing here keeps them cached.
import asyncio  # noqa: F401,E402
import select   # noqa: F401,E402

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)

MODULES = [
    "test_price_planner",
    "test_charger_control",
    "test_charge_guards",
    "test_cost_engine",
    "test_charge_window",          # allowed-window + composable normalize
    "test_controller_decisions",   # needs HA importable; self-skips if not
]


def main():
    total = 0
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            print(f"--- {name}: (not present yet, skipped)")
            continue
        print(f"--- {name}")
        before = len(getattr(mod, "CASES", []))
        mod.main()
        total += before
    print(f"\nALL SUITES PASSED ({total} cases)")


if __name__ == "__main__":
    main()
