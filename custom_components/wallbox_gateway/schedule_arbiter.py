"""Native-schedule arbiter for the Charge Assistant.

When the gateway's ``control_owner`` is the integration AND an acting mode
(smart-charge / solar) is running, the charger's own native schedules would
fight us (they auto-start charging on their window regardless of SOC/solar).
So while we're the active controller we DISABLE the enabled native schedules
(via the proven ``s_sch`` enabled-toggle) and RESTORE them when control reverts.

Safety: the set of schedules we disabled (and their prior enabled-state) is
snapshotted into a Store on disk *before* we touch anything, so a crash /
restart can always restore. Reconciliation is idempotent and only ever
re-enables schedules it previously disabled — never one the user disabled.
"""

from __future__ import annotations

import json
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import GatewayClient, GatewayError, GatewayUnreachable

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1


def _row_to_entry(row: dict, enabled: int) -> dict:
    """Convert an r_schs READ row into an s_sch WRITE entry with a new
    enabled flag, preserving everything else.

    Read shape: start/stop are "HHMM" strings, days is a bitmask int.
    Write shape: start/stop are HHMM ints, days is a [Mon..Sun] bit array.
    """
    bits = int(row.get("days") or 0)
    days = [(bits >> i) & 1 for i in range(7)]
    tgt = row.get("target") if isinstance(row.get("target"), dict) else {}
    return {
        "sid": int(row["sid"]),
        "start": int(str(row.get("start") or "0") or 0),
        "stop": int(str(row.get("stop") or "0") or 0),
        "days": days,
        "mcr": int(row.get("mcr") or 0),
        "type": int(row.get("type") or 0),
        "enabled": int(enabled),
        "target": {
            "type": int(tgt.get("type", 0) or 0),
            "value": int(tgt.get("value", 0) or 0),
        },
        "repeat": int(row.get("repeat") or 1),
    }


class NativeScheduleArbiter:
    """Disables/restores native charger schedules around integration control."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: GatewayClient) -> None:
        self.hass = hass
        self._client = client
        self._store: Store = Store(hass, _STORE_VERSION, f"wallbox_gateway_sched_{entry.entry_id}")
        self._busy = False  # guard against overlapping reconciles

    async def _load(self) -> dict:
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return {"controlling": False, "snapshot": {}}
        return {
            "controlling": bool(data.get("controlling")),
            "snapshot": dict(data.get("snapshot") or {}),
        }

    async def _save(self, state: dict) -> None:
        await self._store.async_save(state)

    async def _read_rows(self) -> list | None:
        """Schedule rows, or None when the read FAILED (BLE busy) — distinct
        from [] which means the charger genuinely has no schedules."""
        try:
            res = await self._client.command(
                {"action": "bapi", "met": "r_schs", "par": "null", "wait": "6000"}
            )
        except (GatewayError, GatewayUnreachable) as e:
            _LOGGER.warning("Schedule arbiter: couldn't read schedules: %s", e)
            return None
        r = res.get("r") if isinstance(res, dict) else None
        if isinstance(r, dict):
            rows = r.get("schedules")
            return rows if isinstance(rows, list) else []
        if isinstance(r, list):
            return r
        return None  # malformed / error reply — treat as a failed read

    async def _set_enabled(self, row: dict, enabled: int) -> bool:
        entry = _row_to_entry(row, enabled)
        par = json.dumps({"schedules": [entry]}, separators=(",", ":"))
        try:
            await self._client.command(
                {"action": "bapi", "met": "s_sch", "par": par, "wait": "6000"}
            )
            return True
        except (GatewayError, GatewayUnreachable) as e:
            _LOGGER.warning(
                "Schedule arbiter: couldn't set schedule #%s enabled=%s: %s",
                row.get("sid"), enabled, e,
            )
            return False

    async def async_reconcile(self, should_control: bool) -> bool:
        """Make the charger's native schedules match our control state.

        should_control True  -> disable every enabled native schedule (snapshot
                                first), so the integration owns charging.
        should_control False -> restore the schedules we disabled.

        Returns True when the desired state is fully applied (so the caller can
        stop retrying), False when it couldn't be applied yet (BLE busy / a
        write failed) and should be retried on the next poll. Idempotent.
        """
        if self._busy:
            return False
        self._busy = True
        try:
            state = await self._load()
            if should_control:
                return await self._take_control(state)
            return await self._release_control(state)
        finally:
            self._busy = False

    async def _take_control(self, state: dict) -> bool:
        rows = await self._read_rows()
        if rows is None:
            # Couldn't read (BLE busy, e.g. just after a gateway reboot). If we
            # were already controlling, leave it; otherwise report not-applied
            # so the caller retries next poll. (rows == [] means no schedules —
            # that IS applied, nothing to disable.)
            return bool(state["controlling"])
        snapshot = dict(state["snapshot"])
        ok = True
        for row in rows:
            if not isinstance(row, dict) or "sid" not in row:
                continue
            if row.get("enabled"):
                snapshot.setdefault(str(int(row["sid"])), 1)
                if await self._set_enabled(row, 0):
                    _LOGGER.info(
                        "Schedule arbiter: disabled native schedule #%s (integration controls)",
                        row["sid"],
                    )
                else:
                    ok = False  # write failed — retry next poll
        # Persist what we've snapshotted even on partial success so a later
        # retry restores everything; only claim "applied" when fully done.
        await self._save({"controlling": True, "snapshot": snapshot})
        return ok

    async def _release_control(self, state: dict) -> bool:
        if not state["controlling"]:
            return True
        snapshot = state["snapshot"]
        ok = True
        if snapshot:
            rows = await self._read_rows()
            if rows is None:
                return False  # couldn't read to restore — retry next poll
            by_sid = {str(int(r["sid"])): r for r in rows
                      if isinstance(r, dict) and "sid" in r}
            for sid, prior in snapshot.items():
                row = by_sid.get(sid)
                if row is not None and prior:
                    if await self._set_enabled(row, 1):
                        _LOGGER.info("Schedule arbiter: restored native schedule #%s", sid)
                    else:
                        ok = False
        if ok:
            await self._save({"controlling": False, "snapshot": {}})
        return ok

    async def async_paused_sids(self) -> list[int]:
        """sids we currently have disabled (for surfacing in the UI)."""
        state = await self._load()
        if not state["controlling"]:
            return []
        return [int(s) for s in state["snapshot"]]
