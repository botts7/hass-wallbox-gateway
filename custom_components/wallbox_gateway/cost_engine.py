"""Charging cost + savings engine — Python port of the Add-on's cost.js.

Pure (no Home Assistant imports) so it is unit-tested in isolation and proven
equivalent to the validated JS engine via shared scenarios. Lets the
integration compute cost + savings as proper sensors (long-term statistics,
energy dashboard) from a tariff it owns + the firmware charge-log.

Mirrors cost.js function-for-function: tariff bands (flat / time-of-use with
optional seasonal rates), schedule-aware charge segments, per-segment grid/green
allocation, and the counterfactual baseline (plug_in / fixed_time / flat_avg).
Keep the two in sync until the JS side is consolidated onto this.
"""

from __future__ import annotations

from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


# ── tz helpers (mirror cost.js tzDayHour, using a tz name) ──────────
def _tz(tzname):
    if not tzname or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tzname)
    except Exception:
        return timezone.utc


def _local(epoch, tzname):
    return datetime.fromtimestamp(epoch, _tz(tzname))


def _day_hour(epoch, tzname):
    """(js_weekday 0=Sun..6=Sat, hour) to match cost.js getDay()."""
    d = _local(epoch, tzname)
    return ((d.weekday() + 1) % 7, d.hour)


def _local_midnight(epoch, tzname):
    d = _local(epoch, tzname)
    return epoch - (d.hour * 3600 + d.minute * 60 + d.second)


# ── tariff model (mirror cost.js) ───────────────────────────────────
def _month_in_season(month, season):
    f, t = season.get("from"), season.get("to")
    if f is None or t is None:
        return False
    return (f <= month <= t) if f <= t else (month >= f or month <= t)


def _season_for(tariff, epoch, tzname):
    if not tariff.get("seasonal") or not isinstance(tariff.get("seasons"), list):
        return None
    month = _local(epoch, tzname).month - 1  # JS getMonth() is 0-based
    for s in tariff["seasons"]:
        if _month_in_season(month, s):
            return s.get("id")
    return None


def _band_rate(tariff, band, season_id):
    if tariff.get("seasonal") and season_id and band.get("seasonRates") \
            and band["seasonRates"].get(season_id) is not None:
        return band["seasonRates"][season_id]
    return band.get("rate") or 0


def _cheapest_band(tariff):
    bands = tariff.get("bands") or []
    if not bands:
        return {"rate": 0}
    return min(bands, key=lambda b: (b.get("rate") or 0))


def _band_for(tariff, epoch, tzname):
    day, hour = _day_hour(epoch, tzname)
    weekend = day in (0, 6)
    assign = tariff.get("weekend") if (weekend and not tariff.get("weekendSame") and tariff.get("weekend")) \
        else tariff.get("weekday")
    painted = assign.get(str(hour), assign.get(hour)) if isinstance(assign, dict) else None
    if painted:
        for b in (tariff.get("bands") or []):
            if b.get("id") == painted:
                return b
    return _cheapest_band(tariff)


# ── charge segments + cost (mirror cost.js) ─────────────────────────
def _charge_segments(s, charge_log):
    span_end = s["stop"] if (s.get("stop") and s["stop"] > s["ts"]) else s["ts"] + (s.get("dur") or 3600) + 86400
    ivals = [iv for iv in (charge_log or []) if s["ts"] <= iv.get("start", 0) <= span_end]
    if ivals:
        tot_wh = sum(iv.get("wh", 0) for iv in ivals) or 1
        tot_gwh = sum(iv.get("gwh", 0) for iv in ivals)
        return [{
            "start": iv["start"],
            "dur": max(60, (iv.get("stop") or iv["start"]) - iv["start"]),
            "frac": (iv.get("wh", 0)) / tot_wh,
            "gFrac": (iv.get("gwh", 0) / tot_gwh) if tot_gwh > 0 else (iv.get("wh", 0) / tot_wh),
        } for iv in ivals]
    return [{"start": s["ts"], "dur": s.get("dur") or 3600, "frac": 1, "gFrac": 1}]


def session_cost(tariff, s, charge_log, tzname):
    en = (s.get("en", 0)) / 1000
    gen = (s.get("gen", 0)) / 1000
    green = max(0.0, min(en, gen))
    grid_kwh = max(0.0, en - green)
    if tariff.get("type") == "flat":
        rate = tariff.get("flatRate") or 0
        return {"total": grid_kwh * rate, "green": green, "saved": green * rate}
    step = 300
    season_id = _season_for(tariff, s["ts"], tzname)
    segs = _charge_segments(s, charge_log)
    raw_grid = [max(0.0, en * sg["frac"] - green * sg["gFrac"]) for sg in segs]
    sum_raw = sum(raw_grid)
    grid_per = [g * grid_kwh / sum_raw for g in raw_grid] if sum_raw > 0 else [grid_kwh * sg["frac"] for sg in segs]
    green_per = [green * sg["gFrac"] for sg in segs]

    def dist(amounts):
        total = 0.0
        for sg, amt in zip(segs, amounts):
            n = max(1, -(-int(sg["dur"]) // step))  # ceil
            per = amt / n
            for i in range(n):
                t = sg["start"] + i * step
                if t >= sg["start"] + sg["dur"]:
                    break
                total += per * _band_rate(tariff, _band_for(tariff, t, tzname), season_id)
        return total

    total = dist(grid_per)
    saved = dist(green_per) if green > 0 else 0.0
    return {"total": total, "green": green, "saved": saved}


def baseline_cost(tariff, s, charge_log, tzname, baseline):
    """Counterfactual cost per baseline mode (plug_in / fixed_time / flat_avg)."""
    en = (s.get("en", 0)) / 1000
    gen = (s.get("gen", 0)) / 1000
    green = max(0.0, min(en, gen))
    grid_kwh = max(0.0, en - green)
    if grid_kwh <= 0:
        return 0.0
    if tariff.get("type") == "flat":
        return grid_kwh * (tariff.get("flatRate") or 0)
    season_id = _season_for(tariff, s["ts"], tzname)
    mode = (baseline or {}).get("mode", "plug_in")

    if mode == "flat_avg":
        mid = _local_midnight(s["ts"], tzname)
        rate = sum(_band_rate(tariff, _band_for(tariff, mid + h * 3600, tzname), season_id) for h in range(24))
        return grid_kwh * (rate / 24)

    segs = _charge_segments(s, charge_log)
    dur = sum(sg["dur"] for sg in segs) or (s.get("dur") or 3600)
    if mode == "fixed_time":
        hhmm = (baseline or {}).get("fixedTime", "00:00")
        parts = str(hhmm).split(":")
        fix_min = (int(parts[0]) if parts[0].isdigit() else 0) * 60 + (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0)
        start_ts = _local_midnight(s["ts"], tzname) + fix_min * 60
        if start_ts < s["ts"]:
            start_ts += 86400
    else:  # plug_in
        start_ts = s["ts"]

    step = 300
    n = max(1, -(-int(dur) // step))
    per = grid_kwh / n
    total = 0.0
    for i in range(n):
        t = start_ts + i * step
        if t >= start_ts + dur:
            break
        total += per * _band_rate(tariff, _band_for(tariff, t, tzname), season_id)
    return total


def _burst_session(iv):
    """Treat one charge-log burst as a mini-session for costing — cost needs
    only the burst's own window + energy (no plug-in time)."""
    stop = iv.get("stop") or iv["start"]
    return {"ts": iv["start"], "stop": stop, "en": iv.get("wh", 0),
            "gen": iv.get("gwh", 0), "dur": max(60, stop - iv["start"])}


def summarize_cost(tariff, intervals, tzname, now_epoch):
    """Week/month charging COST from the firmware charge-log bursts (each billed
    at the tariff rate of the hours it actually ran in). No plug-in time needed,
    so this works purely from charge-log + tariff. Returns None without a
    tariff. Savings (which needs plug-in time) is computed separately."""
    if not tariff:
        return None
    now_local = _local(now_epoch, tzname)
    week_ago = now_epoch - 7 * 86400
    month_start = int(now_local.replace(day=1, hour=0, minute=0, second=0,
                                        microsecond=0).timestamp())
    wk = mo = 0.0
    for iv in (intervals or []):
        st = iv.get("start", 0)
        if st < week_ago and st < month_start:
            continue
        cost = session_cost(tariff, _burst_session(iv), [iv], tzname)["total"]
        if st >= week_ago:
            wk += cost
        if st >= month_start:
            mo += cost
    return {"week_cost": wk, "month_cost": mo, "currency": tariff.get("currency", "$")}


def session_savings(tariff, s, charge_log, tzname, baseline):
    """(shift_saved, solar_saved) for one session."""
    cost = session_cost(tariff, s, charge_log, tzname)
    base = baseline_cost(tariff, s, charge_log, tzname, baseline)
    return max(0.0, base - cost["total"]), cost["saved"]
