"""The computed volatility of the quotes of this node (#636, D).

When ``pricing.PRICE_VOLATILITY`` is not set, the node computes ``v`` from its own history
of the price multiplier (``price_history`` in the database):

* the manager records, once a minute, the multiplier of each resource that a launch
  would record now (``execution_cost.launch_price_bp``, 1x while the node is free);
* for every stretch of length ``T`` in the window (``pricing.PRICE_VOLATILITY_WINDOW_HOURS``),
  the relative rise is the highest multiplier inside the stretch over the one at its
  start, less 1, for the resource that rose the most; a fall counts as 0;
* ``v`` is the p95 of those rises, and never less than ``pricing.PRICE_VOLATILITY_MIN``.
  There is no cap.

With no history, a history shorter than ``T``, or ``T = 0`` (no stretch to measure),
``v`` is the floor. The value is always finite and at least 0. A quote and a launch each
read it once (``price_bound.current_bound``), and a launch stores what it read.
"""
import math
import threading
import time
from collections import deque
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from src.utils.logger import LOGGER as logger

RESOURCES = ("cpu", "mem", "disk")
# One sample a minute: a week of history is about 10 000 rows.
SAMPLE_SECONDS = 60
# A computed value is kept this long, so that a burst of quotes reads the table once.
_CACHE_SECONDS = 60.0

_lock = threading.Lock()
_last_sample: Optional[float] = None
_cache: Dict[tuple, Tuple[float, float]] = {}


def _db():
    from src.database.sql_connection import SQLConnection

    return SQLConnection()


def record(scarcity: Mapping[str, float], now: Optional[float] = None, db=None) -> bool:
    """Record the multipliers of a launch at the load ``scarcity``, at most once per
    ``SAMPLE_SECONDS``. Called by the manager on each tick. Never raises: a missing
    sample only makes the computed volatility a little less informed. True if a sample
    was written."""
    global _last_sample
    now = time.time() if now is None else float(now)
    with _lock:
        if _last_sample is not None and now - _last_sample < SAMPLE_SECONDS:
            return False
        _last_sample = now
    try:
        from src.utils.cost_functions.execution_cost import launch_price_bp
        from src.utils.monetary import prices

        p = prices()
        keep = p.volatility_window_seconds + max(p.volatility_period_seconds, 0)
        (db or _db()).add_price_sample(int(now), launch_price_bp(scarcity), keep)
        return True
    except Exception as e:
        logger(f"[PRICING] Could not record the price multiplier: {e}")
        return False


def rises(samples: Sequence[Tuple[int, Mapping[str, int]]], period_seconds: int) -> List[float]:
    """The relative rise of each stretch of length ``period_seconds`` that starts at a
    sample and ends inside the history: the highest multiplier in ``(start, start + T]``
    over the one at the start, less 1, for the resource that rose the most. A fall is 0.
    ``samples`` are ``(ts, bp per resource)``, oldest first."""
    if period_seconds <= 0 or len(samples) < 2:
        return []
    last_ts = samples[-1][0]
    found: List[float] = []
    windows = {resource: deque() for resource in RESOURCES}  # (index, bp), bp decreasing
    j = 0
    for i, (ts, start_bp) in enumerate(samples):
        end = ts + period_seconds
        if end > last_ts:
            break
        while j < len(samples) and samples[j][0] <= end:
            for resource in RESOURCES:
                window = windows[resource]
                value = int(samples[j][1][resource])
                while window and window[-1][1] <= value:
                    window.pop()
                window.append((j, value))
            j += 1
        rise = 0.0
        for resource in RESOURCES:
            window = windows[resource]
            while window and window[0][0] <= i:
                window.popleft()
            base = int(start_bp[resource])
            if window and base > 0:
                rise = max(rise, window[0][1] / base - 1.0)
        found.append(rise)
    return found


def p95(values: Sequence[float]) -> float:
    """The 95th percentile of ``values``, by nearest rank. 0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)])


def volatility_of(samples: Sequence[Tuple[int, Mapping[str, int]]], period_seconds: int,
                  floor: float) -> float:
    """``max(p95 of the rises, floor)``; the floor alone with no stretch to measure."""
    floor = float(floor) if math.isfinite(float(floor)) and float(floor) >= 0 else 0.0
    found = rises(samples, int(period_seconds))
    if not found:
        return floor
    value = p95(found)
    if not math.isfinite(value) or value < 0:
        return floor
    return max(value, floor)


def computed_volatility(period_seconds: int, window_seconds: int, floor: float,
                        now: Optional[float] = None, db=None) -> float:
    """``v`` from the history of the price of this node over the window. See the module."""
    if int(period_seconds) <= 0:
        return max(float(floor), 0.0)
    now = time.time() if now is None else float(now)
    key = (int(period_seconds), int(window_seconds), float(floor))
    with _lock:
        cached = _cache.get(key)
        if db is None and cached is not None and now - cached[0] < _CACHE_SECONDS:
            return cached[1]
    try:
        samples = (db or _db()).get_price_samples(int(now) - int(window_seconds))
    except Exception as e:
        logger(f"[PRICING] Could not read the price history ({e}); the volatility is the floor.")
        samples = []
    value = volatility_of(samples, int(period_seconds), floor)
    if db is None:
        with _lock:
            _cache[key] = (now, value)
    return value


def reset() -> None:
    """Forget the last sample and the cached values (tests)."""
    global _last_sample
    with _lock:
        _last_sample = None
        _cache.clear()
