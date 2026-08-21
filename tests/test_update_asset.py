"""Regression tests for the firmware Update entity's asset picker (pick_asset).

Covers the board="ota" case: a gateway flashed via `pio run -e ota -t upload`
reports board="ota" (the env name), which matches no release asset — the picker
must fall back to the default (only-shipping) target so OTA still works.
Self-skips when HA isn't importable (update.py imports homeassistant).
Run:  py tests/test_update_asset.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))
import select  # noqa: F401,E402  (preload before the package shadows it)

try:
    from wallbox_gateway.update import pick_asset
    from wallbox_gateway.const import DEFAULT_BOARD
    _HA_OK = True
except Exception as e:  # pragma: no cover - environment without HA
    print(f"--- test_update_asset: SKIPPED (HA not importable: {e})")
    _HA_OK = False

CASES = []
def case(fn):
    CASES.append(fn); return fn


def _rel(*names):
    return {"assets": [{"name": n, "browser_download_url": "u"} for n in names]}


# A typical release: 3 raw partition bins + the named board image.
FULL = _rel("bootloader.bin", "firmware.bin", "partitions.bin",
            f"wallbox-gateway-v3.2.8-{DEFAULT_BOARD}.bin")


@case
def test_exact_board_match():
    assert pick_asset(FULL, DEFAULT_BOARD)["name"] == f"wallbox-gateway-v3.2.8-{DEFAULT_BOARD}.bin"


@case
def test_ota_board_falls_back_to_default():
    # The whole point: board="ota" (espota-env build) → default board's asset.
    assert pick_asset(FULL, "ota")["name"] == f"wallbox-gateway-v3.2.8-{DEFAULT_BOARD}.bin"


@case
def test_unknown_board_returns_none_not_wrong_arch():
    # An UNRECOGNISED board must NOT get a default image — handing a classic
    # ESP32-WROOM an esp32s3 build would brick it. Only the known 'ota' alias
    # remaps; anything else with no matching asset fails visibly.
    assert pick_asset(FULL, "whatever") is None


@case
def test_esp32dev_gets_its_own_asset_not_default():
    # A real multi-target release: the WROOM gateway must get the esp32dev image,
    # never the esp32s3 one.
    rel = _rel("bootloader.bin", "firmware.bin",
               f"wallbox-gateway-v1-{DEFAULT_BOARD}.bin", "wallbox-gateway-v1-esp32dev.bin")
    assert pick_asset(rel, "esp32dev")["name"] == "wallbox-gateway-v1-esp32dev.bin"


@case
def test_ota_alias_needs_the_esp32s3_asset_present():
    # board 'ota' maps to esp32s3; if only an esp32dev asset exists, don't guess.
    rel = _rel("bootloader.bin", "firmware.bin", "wallbox-gateway-v1-esp32dev.bin")
    assert pick_asset(rel, "ota") is None


@case
def test_single_bin_release_still_works():
    rel = _rel("wallbox-gateway-v1-esp32s3.bin")
    assert pick_asset(rel, "ota")["name"] == "wallbox-gateway-v1-esp32s3.bin"


@case
def test_no_named_asset_returns_none():
    rel = _rel("bootloader.bin", "firmware.bin")   # ambiguous, no board asset
    assert pick_asset(rel, "ota") is None


def main():
    if not _HA_OK:
        return
    for fn in CASES:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\n{len(CASES)}/{len(CASES)} passed")


if __name__ == "__main__":
    main()
