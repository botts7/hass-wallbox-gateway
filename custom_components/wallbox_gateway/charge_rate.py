"""Learn the charger's real full-speed power from its own readings.

The charger reports its live power at the wall (before charging losses).
Samples are kept for a rolling two weeks; the learned power is the median of
the full-speed ones (within 15% of the fastest seen), so solar-throttled or
low-current charging doesn't drag it down. Until there are enough full-speed
minutes it returns None and callers use the configured figure.

Battery-side power (what the time-to-charge sums need) is the wall power
times the charging efficiency.

Pure, HA-free; charge_assistant.py does the I/O.
"""

from __future__ import annotations

KEEP_SECS = 14 * 86400
MAX_SAMPLES = 3000
MIN_KW = 1.0            # below this it isn't really charging
SAMPLE_EVERY_SECS = 60
FULL_SPEED_SHARE = 0.85
MIN_FULL_SAMPLES = 30   # about half an hour of full-speed charging


def add_sample(samples: list, now_ts: float, kw: float | None) -> bool:
    """Add one reading ``[ts, kw]`` (at most one a minute, only while charging)
    and drop old ones. True when a sample was added."""
    if kw is None or kw < MIN_KW:
        return False
    if samples and now_ts - samples[-1][0] < SAMPLE_EVERY_SECS:
        return False
    samples.append([round(now_ts), round(float(kw), 3)])
    cutoff = now_ts - KEEP_SECS
    while samples and samples[0][0] < cutoff:
        samples.pop(0)
    del samples[:-MAX_SAMPLES]
    return True


def learned_kw(samples: list) -> float | None:
    """Median wall-side kW of the full-speed samples, or None while learning."""
    vals = [s[1] for s in samples if isinstance(s, (list, tuple)) and len(s) == 2]
    if not vals:
        return None
    top = max(vals)
    full = sorted(v for v in vals if v >= FULL_SPEED_SHARE * top)
    if len(full) < MIN_FULL_SAMPLES:
        return None
    n = len(full)
    mid = full[n // 2] if n % 2 else (full[n // 2 - 1] + full[n // 2]) / 2
    return round(mid, 2)


def battery_kw(wall_kw: float, efficiency: float) -> float:
    """Power that reaches the battery: wall power times efficiency (0.5..1,
    anything else falls back to 0.9)."""
    eff = efficiency if 0.5 <= efficiency <= 1.0 else 0.9
    return wall_kw * eff
