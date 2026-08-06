"""Pure helpers that normalize the Charge Assistant config into the composable
model — an acting *strategy* plus an independent plug-in-reminder *layer* and an
allowed charging *window* — while staying backward-compatible with the legacy
single-mode shape.

No Home Assistant imports, so it's unit-testable in isolation.

Composable shape (entry.options[CA_KEY]):
    mode:      'off' | 'target_soc' | 'solar' | 'smart_solar'   (the strategy)
    reminder:  {enabled: bool, triggers: [...], notify_service, ...}  (layer)
    window_*:  allowed-window fields (see charge_window.py)
    ...strategy-specific fields (soc_entity, surplus_*, etc.)

Legacy shape: mode == 'reminder' with the reminder fields flat at top level.
That migrates to strategy 'off' + an enabled reminder layer.
"""

from __future__ import annotations

# Option keys (mirror const.py; kept as literals so this stays a pure,
# import-free module that's unit-testable in isolation, like the other cores).
CA_MODE = "mode"
CA_REMINDER = "reminder"
CA_REMINDER_ENABLED = "enabled"
CA_TRIGGERS = "triggers"
MODE_OFF = "off"
MODE_REMINDER = "reminder"

# Top-level option keys the set_config bridge accepts (mirror const.py).
# CONTRACT: these are the ONLY top-level keys that survive set_config. Any other
# top-level key is dropped. New v3.3+ tunables MUST nest under `charge_assistant`
# (replaced wholesale) — never add a new top-level key without also widening this
# allow-list, or the GUI will silently write into the void.
CONF_POLL_INTERVAL = "poll_interval"
CA_KEY = "charge_assistant"
TARIFF_KEY = "tariff"
CA_AUTO_RESUME = "auto_resume_eco"
CA_RELEASE_DEFAULT = "release_default"
_RELEASE_VALUES = ("keep", "stop", "resume_schedule", "resume_eco")

# Top-level keys that belong to the reminder layer. When migrating a legacy
# flat reminder config, these move into the reminder sub-dict.
REMINDER_FIELDS = (
    "triggers", "arrival_entity", "nightly_time", "lead_hours",
    "tariff_entity", "tariff_below", "soc_entity", "skip_above_pct",
    "soc_max_age_min", "quiet_start", "quiet_end", "only_if_scheduled",
    "scheduled_within_h", "notify_service", "title", "tap_path", "message",
    "actionable", "escalate_min",
)


def strategy_of(opts: dict | None) -> str:
    """The acting charging strategy. Legacy mode=='reminder' is not a strategy
    (it's a layer now) → resolves to 'off'."""
    mode = (opts or {}).get(CA_MODE) or MODE_OFF
    return MODE_OFF if mode == MODE_REMINDER else mode


def reminder_config(opts: dict | None) -> dict:
    """The reminder-layer config dict, or {} when reminders are off.

    Accepts both the new nested ``reminder`` sub-dict (with an ``enabled``
    flag) and the legacy flat ``mode=='reminder'`` shape.
    """
    opts = opts or {}
    sub = opts.get(CA_REMINDER)
    if isinstance(sub, dict):
        return dict(sub) if sub.get(CA_REMINDER_ENABLED, True) else {}
    if opts.get(CA_MODE) == MODE_REMINDER:
        return {k: opts[k] for k in REMINDER_FIELDS if k in opts}
    return {}


def reminder_enabled(opts: dict | None) -> bool:
    """True when the reminder layer is on AND has at least one trigger."""
    return bool(reminder_config(opts).get(CA_TRIGGERS))


def upsert_car(cars: list | None, index: int | None, car: dict) -> list:
    """Insert (index is None) or replace (existing index) a car profile.

    Pure list surgery for the native Options flow's Vehicles editor — returns a
    new list of shallow-copied dicts so the caller never mutates entry.options
    in place. An out-of-range index is ignored (returns the list unchanged
    rather than raising, so a stale flow can't crash).
    """
    out = [dict(c) for c in (cars or [])]
    if index is None:
        out.append(dict(car))
    elif 0 <= index < len(out):
        out[index] = dict(car)
    return out


def remove_car(cars: list | None, index: int | None) -> list:
    """Drop the car at ``index`` (no-op if None / out of range). New list."""
    out = [dict(c) for c in (cars or [])]
    if index is not None and 0 <= index < len(out):
        del out[index]
    return out


def sanitize_options(incoming: dict | None) -> tuple[dict, list[str]]:
    """Filter a ``set_config`` options payload down to the accepted keys.

    Returns ``(clean, ignored)``:
      * ``clean``   — the sanitised/coerced options to merge into entry.options.
      * ``ignored`` — human-readable messages for every dropped/coerced-away key,
        which the caller logs (keeps this function HA-free and testable).

    This is the single source of truth for the bridge allow-list, so the GUI,
    the service, and the tests all agree on exactly what survives. See the
    CONTRACT note on the key constants above: unknown top-level keys are dropped;
    nest new settings under ``charge_assistant``.
    """
    clean: dict = {}
    ignored: list[str] = []
    for key, val in (incoming or {}).items():
        if key == CONF_POLL_INTERVAL:
            try:
                clean[key] = max(1, min(3600, int(val)))
            except (TypeError, ValueError):
                ignored.append(f"ignoring non-numeric poll_interval {val!r}")
        elif key in (CA_KEY, TARIFF_KEY):
            if isinstance(val, dict):
                clean[key] = val
            else:
                ignored.append(f"ignoring {key} (expected an object)")
        elif key == CA_AUTO_RESUME:
            clean[key] = bool(val)
        elif key == CA_RELEASE_DEFAULT:
            if val in _RELEASE_VALUES:
                clean[key] = val
            else:
                ignored.append(f"ignoring invalid release_default {val!r}")
        else:
            ignored.append(f"ignoring unknown option key {key!r}")
    return clean, ignored
