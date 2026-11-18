"""How much to charge tonight, given tomorrow's solar and the car's habits.

    need          = energy (from the grid side) to reach the target
    floor short   = energy to reach the morning floor (always charged tonight)
    solar         = sum over tomorrow's hours of
                    min(spare solar, charger max) when the car's chance of
                    being home and plugged in is at least the threshold,
                    less any solar kept for the aircon in that hour
    tonight       = max(floor short, need - solar), capped at need

So the floor is always met, a sunny day the car is home for cuts the night
charge, and without a pattern (unknown) or a forecast no hour counts and the
night charges as before. Everything is in kWh at the battery; ``efficiency``
turns it into grid kWh.

Pure, HA-free; charge_assistant.py does the I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Plan:
    need_kwh: float            # grid kWh to reach the target
    floor_kwh: float           # grid kWh to reach the floor (always tonight)
    solar_kwh: float           # usable solar tomorrow, at the battery
    tonight_kwh: float         # grid kWh planned tonight
    target_pct: float          # the SOC tonight's charge stops at
    solar_hours: list = field(default_factory=list)   # [(hour, kWh)] counted
    reason: str = ""


def _pct_to_kwh(pct: float, battery_kwh: float) -> float:
    return max(0.0, pct) / 100.0 * battery_kwh


def plan(*, soc: float, target_pct: float, floor_pct: float, battery_kwh: float,
         efficiency: float, spare_by_hour: dict, home_chance, threshold: float,
         charger_kw: float, reserve_kwh_by_hour: dict | None = None) -> Plan:
    """``home_chance(hour) -> float | None``: chance the car is home and plugged
    in during that hour (None = unknown, treated as away)."""
    eff = efficiency if 0.5 <= efficiency <= 1.0 else 0.9
    floor_pct = min(floor_pct, target_pct)
    need_batt = max(0.0, _pct_to_kwh(target_pct - soc, battery_kwh))
    floor_batt = max(0.0, _pct_to_kwh(floor_pct - soc, battery_kwh))

    counted = []
    solar = 0.0
    for hour in sorted(spare_by_hour):
        p = home_chance(hour)
        if p is None or p < threshold:
            continue
        v = min(max(0.0, spare_by_hour[hour]), max(0.0, charger_kw))
        if reserve_kwh_by_hour:
            v = max(0.0, v - max(0.0, reserve_kwh_by_hour.get(hour, 0.0)))
        if v > 0:
            # Solar goes into the battery at the charger's efficiency too.
            counted.append((hour, round(v, 2)))
            solar += v * eff
    tonight_batt = min(need_batt, max(floor_batt, need_batt - solar))
    target = soc + (tonight_batt / battery_kwh * 100.0 if battery_kwh > 0 else 0.0)
    target = min(target_pct, max(soc, target))

    p = Plan(
        need_kwh=round(need_batt / eff, 1),
        floor_kwh=round(floor_batt / eff, 1),
        solar_kwh=round(solar, 1),
        tonight_kwh=round(tonight_batt / eff, 1),
        target_pct=round(target, 1),
        solar_hours=counted,
    )
    p.reason = explain(p, soc, target_pct, floor_pct)
    return p


def _kwh(v: float) -> str:
    """'about 4 kWh', or 'under 1 kWh' rather than 'about 0 kWh'."""
    return "under 1 kWh" if v < 0.5 else f"about {v:.0f} kWh"


def explain(p: Plan, soc: float, target_pct: float, floor_pct: float) -> str:
    """One plain sentence for the evening notification and the sensor."""
    if p.need_kwh <= 0:
        return f"No charge needed tonight ({soc:.0f}%, target {target_pct:.0f}%)."
    if p.solar_kwh <= 0:
        return (f"Tonight {p.tonight_kwh:.0f} kWh to {p.target_pct:.0f}%. "
                "No solar counted for tomorrow (not sunny enough, or the car usually isn't home).")
    if p.tonight_kwh <= p.floor_kwh and p.floor_kwh > 0:
        return (f"Tonight {p.tonight_kwh:.0f} kWh to the {floor_pct:.0f}% floor. "
                f"Tomorrow {_kwh(p.solar_kwh)} from solar if the car is home.")
    if p.tonight_kwh <= 0:
        return (f"No charge tonight. Tomorrow {_kwh(p.solar_kwh)} from solar "
                "if the car is home.")
    return (f"Tonight {p.tonight_kwh:.0f} kWh to {p.target_pct:.0f}%. "
            f"Tomorrow {_kwh(p.solar_kwh)} from solar if the car is home.")
