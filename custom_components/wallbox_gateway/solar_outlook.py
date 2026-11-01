"""Tomorrow's spare solar, hour by hour.

Two kinds of source, both optional, picked by the user (or "auto"):

- **Forecast integrations** (Forecast.Solar, Solcast, Open-Meteo, ...): HA's
  energy forecast hook gives watt-hours per hour per config entry
  (``{"wh_hours": {iso: wh}}``); several entries (panel planes) add up. That's
  *production*, so this module corrects it (forecasts can run well off at a
  given house) and takes off the house's baseline load to get *spare*.
- **An outlook sensor** with an hourly list in its attributes, e.g. Solar
  Comfort's spare solar outlook: ``[{"start": iso, "kwh": x, "kwh_low": y}]``.
  That is already spare and already corrected, so it's used as is, preferring
  the cautious ``kwh_low``.

The correction is learned from history: each day's forecast total next to
what the panels actually made (a production energy sensor). It's a low
percentile of actual/forecast, so the plan errs on the side of less solar,
i.e. more overnight charging.

Pure, HA-free; charge_assistant.py does the I/O.
"""

from __future__ import annotations

from datetime import datetime

DEFAULT_FACTOR = 1.0
FACTOR_MIN, FACTOR_MAX = 0.3, 4.0
CORRECTION_PERCENTILE = 40     # cautious: most days do at least this well
MIN_CORRECTION_DAYS = 5        # until then, no correction (factor 1.0)
FORECAST_KEEP_DAYS = 21


def _parse(ts) -> datetime | None:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def from_wh_hours(forecasts: list) -> dict:
    """Sum forecast-integration replies into ``{hour start (aware datetime): kWh}``.
    Entries that aren't on the hour are bucketed into their hour."""
    out: dict = {}
    for fc in forecasts or []:
        wh = (fc or {}).get("wh_hours") if isinstance(fc, dict) else None
        if not isinstance(wh, dict):
            continue
        for ts, v in wh.items():
            t = _parse(ts)
            if t is None or t.tzinfo is None:
                continue
            try:
                kwh = float(v) / 1000.0
            except (TypeError, ValueError):
                continue
            h = t.replace(minute=0, second=0, microsecond=0)
            out[h] = out.get(h, 0.0) + max(0.0, kwh)
    return out


def from_hourly_attr(hourly) -> dict:
    """Read an outlook sensor's ``hourly`` list into ``{hour start: kWh}``,
    preferring the cautious ``kwh_low`` when present."""
    out: dict = {}
    if not isinstance(hourly, list):
        return out
    for row in hourly:
        if not isinstance(row, dict):
            continue
        t = _parse(row.get("start"))
        if t is None or t.tzinfo is None:
            continue
        v = row.get("kwh_low", row.get("kwh"))
        try:
            kwh = float(v)
        except (TypeError, ValueError):
            continue
        h = t.replace(minute=0, second=0, microsecond=0)
        out[h] = out.get(h, 0.0) + max(0.0, kwh)
    return out


def learn_factor(history: dict, percentile: int = CORRECTION_PERCENTILE,
                 min_days: int = MIN_CORRECTION_DAYS) -> float:
    """Correction factor from ``{"YYYY-MM-DD": {"forecast": kWh, "actual": kWh}}``:
    the given percentile of actual/forecast over days that have both (and a
    meaningful forecast), clamped. 1.0 until there are ``min_days``."""
    ratios = []
    for day in (history or {}).values():
        if not isinstance(day, dict):
            continue
        try:
            f = float(day.get("forecast"))
            a = float(day.get("actual"))
        except (TypeError, ValueError):
            continue
        if f >= 1.0 and a >= 0:
            ratios.append(a / f)
    if len(ratios) < min_days:
        return DEFAULT_FACTOR
    ratios.sort()
    idx = min(len(ratios) - 1, max(0, round((percentile / 100) * (len(ratios) - 1))))
    return max(FACTOR_MIN, min(FACTOR_MAX, ratios[idx]))


def spare(production: dict, factor: float, baseline_kw: float,
          max_hour_kwh: float | None = None) -> dict:
    """Corrected production minus the house's baseline, per hour, never below 0.
    ``max_hour_kwh`` caps an hour at what the panels have ever made (a
    correction factor can't push a forecast past the hardware)."""
    out = {}
    for h, kwh in production.items():
        v = kwh * factor
        if max_hour_kwh is not None and max_hour_kwh > 0:
            v = min(v, max_hour_kwh)
        out[h] = max(0.0, v - max(0.0, baseline_kw))
    return out


def for_day(hourly: dict, day) -> dict:
    """Only the hours of local calendar date ``day``."""
    return {h: v for h, v in hourly.items() if h.date() == day}
