"""Pytest config for the HA-coupled tests.

These run under pytest-homeassistant-custom-component (a real hass instance),
unlike the pure-logic suites that tests/run_all.py drives without HA installed.

    pip install pytest-homeassistant-custom-component
    pytest tests/test_coordinator_partial_update.py
"""

import os
import sys

import pytest

# Repo root on sys.path so `custom_components.wallbox_gateway` imports as a
# package (relative imports inside it need that).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA load the integration from custom_components/."""
    yield
