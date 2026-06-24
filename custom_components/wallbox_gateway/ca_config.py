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
