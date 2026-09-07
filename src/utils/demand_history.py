"""What this node was asked for, by hour of the day (issue #337).

The operator picks the hours this machine works in (`activity_window`), and the TUI
draws that choice as a day. The question the drawing provokes is the one the node could
not answer: *which hours does anybody actually ask for?* -- and, sharper, *how much work
am I turning away by closing at 22:00?*

Nothing recorded it. `tunnel_traffic` keeps a byte count per calendar day, which says
which days were busy and never which hours; `payments.created_at` timestamps a deposit,
which arrives in lumps unrelated to when work runs; `instance_consumption` is a running
average per instance with no history, and its row dies with the instance. Meanwhile the
manager tick already counts the instances it holds and prices every one of them, once an
interval, and drops both figures on the floor.

So this keeps them. One row per local hour, six numbers, appended by the tick and by the
two paths that accept and refuse work:

* ``instances_held`` -- the *peak* within the hour, not the mean. The question is what
  the machine had to fit at once, and a mean hides the hour that actually hurt.
* ``mu_charged`` -- what the ticks in that hour really billed, so a busy hour and a
  profitable hour can be told apart.
* ``admissions`` / ``refusals`` -- launches taken and launches turned away.
* ``refused_closed`` -- of those refusals, the ones that were only refused because the
  window was shut. This is the number the whole table exists for: it is what makes
  "these are my hours" answerable as "these are my hours, and this is what they cost
  me".

Local hours, matching `activity_window` and `tunnel_traffic`: a history in UTC would not
line up with the window it exists to be read against.

Kept in memory between flushes for the same reason `host_limits._DailyTraffic` is -- the
counters move per launch and per tick, and only whole hours need to reach the disk --
and bounded by `RETENTION_DAYS`, so this is not another table that grows without an
owner. Every write is best-effort: a node must not fail to take work because it could
not write a statistic about taking work.
"""
import threading
from datetime import datetime
from typing import Dict, List, Optional

from src.utils.logger import LOGGER as logger

# How much history is kept. Long enough to see a weekly shape, short enough that the
# table stays a rounding error: 24 rows a day, ~2k rows at this setting.
RETENTION_DAYS = 90

# The hour an `hour` column names, as SQLite sorts it lexically: 'YYYY-MM-DDTHH'.
_HOUR_FORMAT = "%Y-%m-%dT%H"


def hour_key(moment: Optional[datetime] = None) -> str:
    """The local hour ``moment`` falls in, as the key its row is stored under."""
    return (moment or datetime.now()).strftime(_HOUR_FORMAT)


class _Hour:
    """One hour's counters, before they reach the database.

    Additive except for ``instances_held``, which is a peak: two flushes of the same
    hour add their launches together, and keep the larger of their two high-water
    marks.
    """

    __slots__ = ("instances_held", "mu_charged", "admissions", "refusals", "refused_closed")

    def __init__(self) -> None:
        self.instances_held = 0
        self.mu_charged = 0
        self.admissions = 0
        self.refusals = 0
        self.refused_closed = 0

    def is_empty(self) -> bool:
        return not (
            self.instances_held
            or self.mu_charged
            or self.admissions
            or self.refusals
            or self.refused_closed
        )

    def as_row(self) -> Dict[str, int]:
        return {
            "instances_held": self.instances_held,
            "mu_charged": self.mu_charged,
            "admissions": self.admissions,
            "refusals": self.refusals,
            "refused_closed": self.refused_closed,
        }


class _Recorder:
    """The current hour's counters, flushed when the hour turns over.

    One lock around the whole thing: the manager tick, the launcher and the gateway's
    refusal paths all reach this from different threads, and a counter incremented
    without one loses events at exactly the load where the history matters.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hour: Optional[str] = None
        self._pending = _Hour()

    def _flush_unlocked(self) -> None:
        if self._hour is None or self._pending.is_empty():
            return
        hour, row = self._hour, self._pending.as_row()
        self._pending = _Hour()
        try:
            from src.database.sql_connection import SQLConnection

            SQLConnection().add_demand_history(hour=hour, **row)
        except Exception as e:
            # Losing a statistic is not worth a raise on the path that took the work.
            logger(f"[DEMAND] Could not record the demand history for {hour} ({e}).")

    def _rollover_unlocked(self, now: str) -> None:
        if self._hour == now:
            return
        self._flush_unlocked()
        self._hour = now
        try:
            from src.database.sql_connection import SQLConnection

            SQLConnection().prune_demand_history(keep_days=RETENTION_DAYS)
        except Exception as e:
            logger(f"[DEMAND] Could not prune the demand history ({e}).")

    def record(
        self,
        *,
        instances_held: Optional[int] = None,
        mu_charged: int = 0,
        admissions: int = 0,
        refusals: int = 0,
        refused_closed: int = 0,
        moment: Optional[datetime] = None,
    ) -> None:
        with self._lock:
            self._rollover_unlocked(hour_key(moment))
            if instances_held is not None:
                self._pending.instances_held = max(
                    self._pending.instances_held, int(instances_held)
                )
            self._pending.mu_charged += int(mu_charged)
            self._pending.admissions += int(admissions)
            self._pending.refusals += int(refusals)
            self._pending.refused_closed += int(refused_closed)

    def flush(self) -> None:
        with self._lock:
            self._flush_unlocked()


_RECORDER = _Recorder()


def record_tick(instances_held: int, mu_charged: int) -> None:
    """What the maintenance tick just held and just charged.

    Called once per iteration with figures it has already computed, so the history costs
    no measurement of its own -- only the writing down.
    """
    _RECORDER.record(instances_held=instances_held, mu_charged=mu_charged)


def record_admission() -> None:
    """A launch this node accepted."""
    _RECORDER.record(admissions=1)


def record_refusal(*, because_closed: bool = False) -> None:
    """A launch this node turned away, and whether the closed window is the only reason.

    ``because_closed`` is the distinction the table is for: a refusal for want of memory
    would have happened at any hour, while one behind a shut window is work the operator
    chose to decline and can decide to accept by moving an edge.
    """
    _RECORDER.record(refusals=1, refused_closed=1 if because_closed else 0)


def flush() -> None:
    """Write the current hour out now, without waiting for it to turn over."""
    _RECORDER.flush()


def history(days: int = 30) -> List[dict]:
    """The recorded hours over the last ``days``, oldest first."""
    try:
        from src.database.sql_connection import SQLConnection

        return SQLConnection().get_demand_history(days=days)
    except Exception as e:
        logger(f"[DEMAND] Could not read the demand history ({e}).")
        return []


def by_hour_of_day(days: int = 30) -> List[int]:
    """Peak instances held per hour of the day, 24 entries, 00:00 first.

    The shape the SCHEDULE page draws under the window: for each hour of the clock, the
    worst hour of that name in the period. A mean across days would flatten a machine
    that is busy every evening into one that is mildly busy all the time, which is the
    opposite of what an operator choosing hours needs to see.
    """
    peaks = [0] * 24
    for row in history(days=days):
        hour = row.get("hour") or ""
        try:
            index = int(hour[-2:])
        except (ValueError, TypeError):
            continue
        if 0 <= index < 24:
            peaks[index] = max(peaks[index], int(row.get("instances_held") or 0))
    return peaks


def refused_by_hour_of_day(days: int = 30) -> List[int]:
    """Work turned away for a shut window, per hour of the clock, 24 entries.

    Read against the window itself this is the cost of the operator's own hours, which
    is the only number here that can change a decision.
    """
    totals = [0] * 24
    for row in history(days=days):
        hour = row.get("hour") or ""
        try:
            index = int(hour[-2:])
        except (ValueError, TypeError):
            continue
        if 0 <= index < 24:
            totals[index] += int(row.get("refused_closed") or 0)
    return totals
