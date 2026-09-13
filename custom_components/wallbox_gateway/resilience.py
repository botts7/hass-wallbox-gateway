"""Pure decision helpers for graceful degradation.

No Home Assistant imports live here, so tests/run_all.py can exercise these
without a HA install — same convention as charge_guards.py / charge_window.py.

They back three behaviours:

  * Partial coordinator updates (#8). One slow secondary endpoint must not
    invalidate data the other endpoints returned successfully in the same tick.
  * Slow-cadence polling (#8). An expensive endpoint gets its own interval
    instead of riding the main poll tick.
  * Once-per-key logging (#8, #9). A persistent fault is logged the first time
    it is seen, not on every poll — 3,659 identical tracebacks in 12 hours
    rotated a user's log away (#9).
  * Unmapped enum fallback (#9). An unknown charger status code degrades to
    `unknown` instead of a free-text label that HA's enum sensor rejects.
"""

from __future__ import annotations

from typing import Any, Hashable, Mapping, Sequence


def partition_results(
    names: Sequence[str],
    results: Sequence[Any],
) -> tuple[dict[str, Any], dict[str, BaseException]]:
    """Split an ``asyncio.gather(..., return_exceptions=True)`` result.

    Returns ``(ok, failed)`` keyed by the caller's names, so the caller can
    decide per endpoint whether a failure is fatal or merely degrading.
    """
    ok: dict[str, Any] = {}
    failed: dict[str, BaseException] = {}
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            failed[name] = result
        else:
            ok[name] = result
    return ok, failed


def first_exception(
    failed: Mapping[str, BaseException],
    types: type | tuple[type, ...],
) -> BaseException | None:
    """First failure that is an instance of ``types``, or None.

    Used for conditions that are fatal wherever they appear — an auth rejection
    on any endpoint means the credentials are wrong for all of them.
    """
    for exc in failed.values():
        if isinstance(exc, types):
            return exc
    return None


def due(cycle: int, every: int) -> bool:
    """True when a slow-cadence job should run on this 1-based poll cycle.

    Cycle 1 always runs so entities populate at startup; after that, every
    ``every``-th cycle. ``every <= 1`` means "every cycle".
    """
    if every <= 1:
        return True
    return cycle % every == 1


def cycles_for(target_seconds: float, poll_interval_seconds: float) -> int:
    """How many poll cycles approximate ``target_seconds``, at least 1.

    Keeps a slow-cadence endpoint anchored to wall-clock time rather than to
    the user's poll interval: at a 10 s tick a 300 s target is every 30th
    cycle; at a 60 s tick it is every 5th.
    """
    if poll_interval_seconds <= 0:
        return 1
    return max(1, round(target_seconds / poll_interval_seconds))


class OnceSeen:
    """Remembers keys so a repeating condition is acted on only the first time.

    Deliberately unbounded-but-tiny: the key spaces here are charger status
    codes and endpoint/exception pairs, both of which are small and closed.
    """

    def __init__(self) -> None:
        self._seen: set[Hashable] = set()

    def is_new(self, key: Hashable) -> bool:
        """True the first time ``key`` is offered; False on every repeat."""
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    def forget(self, key: Hashable) -> None:
        """Allow ``key`` to fire once more (e.g. after it recovered)."""
        self._seen.discard(key)

    def clear(self) -> None:
        self._seen.clear()

    def __contains__(self, key: Hashable) -> bool:
        return key in self._seen

    def __len__(self) -> int:
        return len(self._seen)


def resolve_status_label(
    code: int | None,
    *,
    zentri: bool,
    gen: Any,
    status_codes: Mapping[int, str],
    zentri_codes: Mapping[int, str],
    not_charging_label: str,
) -> tuple[str | None, int | None]:
    """Map a charger status code to its enum label.

    Returns ``(label, unknown_code)``.

    ``label`` is None when the code has no mapping. The sensor then reports
    `unknown`, which is valid for any enum sensor, instead of a free-text
    ``"Code 19"`` that HA rejects with a ValueError on *every* state write (#9).
    ``unknown_code`` carries the offending code back to the caller so it can be
    logged once and exposed as an attribute for reporting.

    Wallbox status 4 is "Paused", but that term covers two different states: an
    active override (Schedule/Solar charging paused — r_dat.gen != 0) and a
    plain stopped/idle session (gen == 0, e.g. after reaching target). Only the
    former is really "Paused" — disambiguated here so idle isn't mislabelled.
    """
    if code is None:
        return None, None
    if code == 4 and not zentri:
        return ("Paused" if (gen or 0) != 0 else not_charging_label), None
    # Zentri uses a different enum; its labels are reused from status_codes so
    # the ENUM `options` list stays valid. Fall back to the MAX table for codes
    # Zentri doesn't define, which is how the original code behaved.
    table = zentri_codes if zentri else status_codes
    label = table.get(code)
    if label is None:
        label = status_codes.get(code)
    if label is None:
        return None, code
    return label, None


def ride_through_critical(streak: int, grace: int, has_prior: bool) -> bool:
    """Whether a critical-endpoint failure should be ridden through this poll.

    A critical read (``/api/status`` / ``/api/charger``) can stall for a few
    seconds under transient gateway pressure (a periodic charger event on the
    Plus BLE path briefly starves the HTTP server). At a 10 s poll that is a
    single failed cycle. Rather than flap every entity to unavailable for that
    one cycle, keep the last-good data — but only if we actually HAVE prior data
    and the failures have not persisted past ``grace`` consecutive polls. A real
    outage (no prior data, or streak reached grace) still fails the update so
    entities correctly go unavailable.

    ``streak`` is the count INCLUDING this failure (1 on the first failed poll).
    Returns True to ride through (return last-good), False to fail the update.
    """
    return has_prior and streak < grace
