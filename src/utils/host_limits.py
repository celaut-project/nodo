"""Hard ceilings on what this node may take from the machine it runs on.

Nothing else refuses a workload for being too large. `pricing.SCARCITY_*` makes a loaded
machine expensive and `low_demand` gates only the opportunistic fallback, so without a
ceiling a client may rent every core and every byte the host has. On a rented server that
is the point. On the PC its owner is also using, it is not.

Two different kinds of ceiling live here, because they are enforced in two different
places and it is worth being clear about which is which:

**CPU, RAM and disk are admission ceilings.** They are checked against the sum of what
every instance has been *granted* -- the `local_instances` row the maintenance tick
prices it by -- plus what the newcomer asks for, at launch and on every resize. That sum
is a real bound on usage rather than a guess: the hypervisor holds each guest to the
memory size, CFS quota and image size it was created with. Nothing here samples live
load.

**Network is metered as it flows.** There is no grant to add up: traffic is a rate and a
volume, so the day's total is counted in :class:`_DailyTraffic` and the throughput
shaped by :class:`_RateLimiter`, both fed from the tunnel relay
(`src/tunneling/rpc_tunnel.py`). Only tunnelled traffic passes through here; an instance
reachable on a port of its own never touches this node's relay.

The ceilings apply to every instance this node starts, its own included. A cap on what
nodo occupies that the operator's own instances could step over would not be a cap --
and unlike the activity window (`src/utils/activity_window.py`), where exempting the
operator is the whole point, here there is nobody to exempt: a byte held by a dev
instance is as unavailable to the person using the PC as any other.

Shares are of the whole machine as psutil reports it, resolved at use time so a config
edit from the TUI needs no restart. 0 lifts that one ceiling; the section switched off
lifts all of them.

What admission compares is the service's declared `resources.at_most`, which is where a
service says how large it may become. A service that declares no `at_most` at all is not
measured here -- it is not measured by the memory pool or the free-disk check either, for
the same reason: there is no figure to measure. Such an instance still lands in
`local_instances` with the floors the virtualizer gave it, so it counts against the next
launch, and being over the ceiling is enough on its own to refuse one (see
:func:`ceiling_shortfalls` on why zero is a figure and None is not).
"""
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Tuple

import psutil

from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger

env_manager = ConfigManager()

SECTION = "host_limits"

BYTES_PER_GIB = 1024 ** 3
BYTES_PER_MIB = 1024 ** 2

# The CFS period the kernel uses when a request declares a quota but no period, so
# quota/period reads as cores either way. Mirrors resource_availability's own default.
DEFAULT_CPU_PERIOD_US = 100_000

# How long a resolved network setting is reused. The relay asks per message -- tens of
# times a second per tunnel -- and a ceiling that lags by a second is indistinguishable
# from one that does not, whereas a config read per 64 KiB is not free.
_NET_SETTINGS_TTL_S = 1.0

# The longest a single throttle wait blocks its thread. The deficit it could not sleep
# off stays in the bucket, so the shaped rate is unchanged; this only keeps one 64 KiB
# read from parking a relay thread for a minute when the ceiling is very low.
MAX_THROTTLE_WAIT_S = 2.0

# How much tunnelled traffic accumulates in memory before the day's running total is
# written to the database. The total has to survive a restart -- a daily allowance that
# resets whenever the daemon does is not a daily allowance -- and a write per relayed
# message would be a database round-trip per 64 KiB.
DAILY_FLUSH_BYTES = 8 * BYTES_PER_MIB


@dataclass(frozen=True)
class Ceilings:
    """The absolute ceilings the configured shares work out to on this machine.

    None on a field means that resource is not capped, which is what a share of 0 asks
    for and also what an unreadable host total leaves: a capacity psutil cannot report
    is not evidence of a small one, and refusing every launch over a failed reading
    would be a worse answer than not enforcing the ceiling.
    """
    cores: Optional[float]
    ram_bytes: Optional[int]
    disk_bytes: Optional[int]


@dataclass(frozen=True)
class Committed:
    """What every instance this node runs has already been granted, added up."""
    cores: float
    ram_bytes: int
    disk_bytes: int
    instances: int


def is_enabled() -> bool:
    try:
        return bool(env_manager.get(f"{SECTION}.ENABLED", False))
    except Exception:
        return False


def _number(key: str, default: float = 0.0) -> float:
    """A numeric setting from this section, or ``default`` when it is not a number."""
    try:
        value = env_manager.get(f"{SECTION}.{key}", default)
    except Exception:
        return default
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _share(key: str) -> float:
    """A share setting, clamped into [0, 1]. 0 means "no ceiling on this one"."""
    return max(0.0, min(_number(key, 0.0), 1.0))


def storage_path() -> str:
    """The directory whose filesystem the disk ceiling is measured against."""
    try:
        return str(env_manager.get("main.STORAGE", "/") or "/")
    except Exception:
        return "/"


def host_totals() -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """(physical cores, total RAM bytes, total storage bytes), each None if unreadable.

    Physical cores rather than logical ones, matching the CPU check in
    `resource_availability`: a CFS quota is compared against the cores the machine
    really has, and counting hyperthreads would double the allowance.
    """
    try:
        cores = psutil.cpu_count(logical=False) or None
    except Exception:
        cores = None
    try:
        ram = int(psutil.virtual_memory().total) or None
    except Exception:
        ram = None
    try:
        disk = int(psutil.disk_usage(storage_path()).total) or None
    except (psutil.Error, OSError):
        try:
            disk = int(psutil.disk_usage("/").total) or None
        except (psutil.Error, OSError):
            disk = None
    return cores, ram, disk


def ceilings() -> Optional[Ceilings]:
    """The ceilings in force, or None when this node is not capped at all."""
    if not is_enabled():
        return None

    cpu_share = _share("MAX_CPU_SHARE")
    ram_share = _share("MAX_RAM_SHARE")
    disk_share = _share("MAX_DISK_SHARE")
    if not (cpu_share or ram_share or disk_share):
        return None

    cores, ram, disk = host_totals()
    return Ceilings(
        cores=cores * cpu_share if (cpu_share and cores) else None,
        ram_bytes=int(ram * ram_share) if (ram_share and ram) else None,
        disk_bytes=int(disk * disk_share) if (disk_share and disk) else None,
    )


def requested_cores(cpu_quota: int, cpu_period: int) -> float:
    """A CFS pair expressed in cores, or 0.0 when no quota is declared."""
    if not cpu_quota:
        return 0.0
    return float(cpu_quota) / float(cpu_period or DEFAULT_CPU_PERIOD_US)


def committed_resources() -> Committed:
    """What the instances on this node hold, from the rows the tick prices them by.

    The database is imported here rather than at module scope on purpose: this module is
    reached from `resource_availability`, which needs nothing but psutil to answer
    "does this shape fit?", and pulling the whole SQL layer (and the identity and
    netifaces imports behind it) into that path would undo the reason it stands alone.

    An unreadable database reports nothing committed. That is the direction that keeps a
    broken read from bricking the node, and it is safe here because the ceiling is not
    the only thing standing between a launch and the machine: the memory pool and the
    free-disk checks in `resource_availability` still apply.
    """
    try:
        from src.database.sql_connection import SQLConnection

        rows = SQLConnection().get_committed_resources()
    except Exception as e:
        logger(f"[LIMITS] Could not read what this node already holds ({e}); "
               "treating the host ceilings as unspent for this decision.")
        return Committed(cores=0.0, ram_bytes=0, disk_bytes=0, instances=0)

    cores = 0.0
    ram_bytes = 0
    disk_bytes = 0
    for row in rows:
        cores += requested_cores(int(row["cpu_quota"] or 0), int(row["cpu_period"] or 0))
        ram_bytes += int(row["mem_limit"] or 0)
        disk_bytes += int(row["disk_space"] or 0)
    return Committed(
        cores=cores,
        ram_bytes=ram_bytes,
        disk_bytes=disk_bytes,
        instances=len(rows),
    )


def _breach(
        resource: str,
        key: str,
        requested: float,
        held: float,
        ceiling: float,
        unit: str,
) -> str:
    return (
        f"{resource} ceiling reached ({SECTION}.{key}). "
        f"This node already holds {held:.4g} {unit} and allows {ceiling:.4g} {unit}; "
        f"this instance asks for {requested:.4g} {unit}."
    )


def ceiling_shortfalls(
        *,
        cores: Optional[float] = None,
        ram_bytes: Optional[int] = None,
        disk_bytes: Optional[int] = None,
        committed: Optional[Committed] = None,
) -> List[str]:
    """Every ceiling an instance of this shape would push this node over.

    All of them, not the first, for the same reason `resource_availability` reports every
    shortfall: an operator told only about memory raises the memory share, retries, and
    is then told about disk.

    A resource is asked about when a figure is given for it, and *skipped* when it is
    None. Zero is a figure: a service that declares no memory still becomes an instance
    holding the virtualizer's floor, so "asks for nothing" has to be refused by a node
    already at its ceiling -- otherwise declaring nothing would be the way past it.
    None is for the caller who has nothing to ask about that resource, which is a
    resize leaving it alone: a node pushed over its ceiling (by shares lowered
    underneath it) must not be stopped from releasing what would bring it back under.

    ``committed`` is read once here unless the caller passes its own -- a resize has
    already read it to work out the deltas, and reading it twice could see two different
    machines.
    """
    bounds = ceilings()
    if bounds is None:
        return []
    if cores is None and ram_bytes is None and disk_bytes is None:
        return []

    held = committed if committed is not None else committed_resources()
    shortfalls: List[str] = []

    if bounds.cores is not None and cores is not None and held.cores + cores > bounds.cores:
        shortfalls.append(_breach(
            "CPU", "MAX_CPU_SHARE", cores, held.cores, bounds.cores, "vCPU",
        ))
    if (bounds.ram_bytes is not None and ram_bytes is not None
            and held.ram_bytes + ram_bytes > bounds.ram_bytes):
        shortfalls.append(_breach(
            "Memory", "MAX_RAM_SHARE",
            ram_bytes / BYTES_PER_GIB, held.ram_bytes / BYTES_PER_GIB,
            bounds.ram_bytes / BYTES_PER_GIB, "GiB",
        ))
    if (bounds.disk_bytes is not None and disk_bytes is not None
            and held.disk_bytes + disk_bytes > bounds.disk_bytes):
        shortfalls.append(_breach(
            "Disk", "MAX_DISK_SHARE",
            disk_bytes / BYTES_PER_GIB, held.disk_bytes / BYTES_PER_GIB,
            bounds.disk_bytes / BYTES_PER_GIB, "GiB",
        ))
    return shortfalls


# --- Network -------------------------------------------------------------------------
#
# Traffic has no grant to add up, so it is the one resource here that is measured while
# it moves. Both mechanisms are process-wide singletons: the ceilings are on this node's
# tunnelling, not on one tunnel's, and a per-tunnel bucket would let ten tunnels use ten
# times the configured rate.

_net_settings_lock = threading.Lock()
_net_settings: Optional[Tuple[int, float]] = None
_net_settings_read_at = 0.0


def net_settings() -> Tuple[int, float]:
    """(daily cap in bytes, rate ceiling in bytes per second). 0 means unlimited."""
    global _net_settings, _net_settings_read_at

    now = time.monotonic()
    with _net_settings_lock:
        if _net_settings is not None and now - _net_settings_read_at < _NET_SETTINGS_TTL_S:
            return _net_settings
        if not is_enabled():
            resolved = (0, 0.0)
        else:
            resolved = (
                int(max(0.0, _number("MAX_NET_GIB_PER_DAY")) * BYTES_PER_GIB),
                max(0.0, _number("MAX_NET_MIB_PER_SECOND")) * BYTES_PER_MIB,
            )
        _net_settings, _net_settings_read_at = resolved, now
        return resolved


def net_daily_cap_bytes() -> int:
    return net_settings()[0]


def net_rate_bytes_per_second() -> float:
    return net_settings()[1]


class _DailyTraffic:
    """The day's tunnelled byte count, kept in memory and flushed to the database.

    The database is what makes the allowance daily rather than per-run: a counter that
    reset whenever the daemon restarted would let an operator on a metered connection
    blow through a 20 GiB ceiling by restarting. In memory between flushes because the
    relay accounts per message; only whole `DAILY_FLUSH_BYTES` blocks reach the disk,
    plus whatever a settling tunnel leaves behind.

    The day is the host's local calendar day, so it turns over at the operator's
    midnight rather than at UTC's.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: Optional[date] = None
        self._total = 0
        self._unflushed = 0

    @staticmethod
    def _load(day: date) -> int:
        try:
            from src.database.sql_connection import SQLConnection

            return int(SQLConnection().get_tunnel_traffic(day=day.isoformat()))
        except Exception as e:
            logger(f"[LIMITS] Could not read today's tunnelled traffic ({e}); "
                   "counting this run's from zero.")
            return 0

    @staticmethod
    def _store(day: date, byte_count: int) -> None:
        if byte_count <= 0:
            return
        try:
            from src.database.sql_connection import SQLConnection

            SQLConnection().add_tunnel_traffic(day=day.isoformat(), byte_count=byte_count)
        except Exception as e:
            logger(f"[LIMITS] Could not record {byte_count} tunnelled bytes ({e}).")

    def _rollover_unlocked(self, today: date) -> None:
        if self._day == today:
            return
        if self._day is not None and self._unflushed:
            self._store(self._day, self._unflushed)
        self._day = today
        self._total = self._load(today)
        self._unflushed = 0

    def total(self, today: Optional[date] = None) -> int:
        """Bytes tunnelled so far today, flushed and unflushed together."""
        with self._lock:
            self._rollover_unlocked(today or datetime.now().date())
            return self._total

    def account(self, byte_count: int, today: Optional[date] = None) -> int:
        """Add ``byte_count`` to today's total and return it.

        Accounted after the bytes have moved, like the billing beside it: data already
        relayed is never thrown away for want of allowance, at the cost of the ceiling
        being noticed one message late.
        """
        pending = 0
        with self._lock:
            self._rollover_unlocked(today or datetime.now().date())
            self._total += max(0, byte_count)
            self._unflushed += max(0, byte_count)
            day = self._day
            if self._unflushed >= DAILY_FLUSH_BYTES:
                pending, self._unflushed = self._unflushed, 0
            total = self._total
        if pending and day is not None:
            self._store(day, pending)
        return total

    def flush(self) -> None:
        """Write out what has not reached the database yet."""
        with self._lock:
            pending, self._unflushed = self._unflushed, 0
            day = self._day
        if pending and day is not None:
            self._store(day, pending)


class _RateLimiter:
    """Shapes tunnelled throughput to the configured MiB per second.

    A token bucket holding at most one second's worth of allowance, so a burst after an
    idle stretch is one second long rather than unbounded. It makes the relay wait; it
    never closes anything, which is the difference between a slow transfer and a broken
    one.

    The wait happens outside the lock: two relay threads sleeping in turn is the point,
    and sleeping under the lock would serialise them into half the configured rate.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens = 0.0
        self._checked_at = time.monotonic()

    def _delay_for(self, byte_count: int, rate: float) -> float:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(rate, self._tokens + (now - self._checked_at) * rate)
            self._checked_at = now
            self._tokens -= byte_count
            if self._tokens >= 0:
                return 0.0
            return -self._tokens / rate

    def wait(self, byte_count: int) -> float:
        """Sleep long enough that ``byte_count`` did not exceed the ceiling.

        Returns how long it actually slept, which is what the tests read. A single call
        never blocks for more than `MAX_THROTTLE_WAIT_S`; the debt it could not sleep off
        stays in the bucket, so the next call pays it and the shaped rate is unchanged.
        """
        rate = net_rate_bytes_per_second()
        if rate <= 0 or byte_count <= 0:
            return 0.0
        delay = min(self._delay_for(byte_count, rate), MAX_THROTTLE_WAIT_S)
        if delay > 0:
            time.sleep(delay)
        return delay


_daily_traffic = _DailyTraffic()
_rate_limiter = _RateLimiter()


def account_tunnel_traffic(byte_count: int) -> bool:
    """Count relayed bytes against the daily ceiling. False once it is spent.

    Called for every message the relay moves in either direction, whether or not
    traffic is being billed: the ceiling is a policy about this machine's connection and
    has nothing to do with `pricing.NET_MU_PER_GIB` being zero.
    """
    cap = net_daily_cap_bytes()
    if cap <= 0:
        return True
    return _daily_traffic.account(byte_count) < cap


def daily_allowance_spent() -> bool:
    """Whether today's tunnelled traffic ceiling is already used up.

    Asked before a tunnel is opened, so a node with nothing left refuses cleanly instead
    of handing out a socket it will close on the first message.
    """
    cap = net_daily_cap_bytes()
    return cap > 0 and _daily_traffic.total() >= cap


def daily_allowance_reason() -> str:
    cap = net_daily_cap_bytes()
    return (
        f"This node has relayed its whole daily tunnel allowance "
        f"({cap / BYTES_PER_GIB:.4g} GiB, {SECTION}.MAX_NET_GIB_PER_DAY). "
        "It resets at local midnight."
    )


def throttle_tunnel_traffic(byte_count: int) -> float:
    """Hold tunnelled throughput to `MAX_NET_MIB_PER_SECOND`. Returns the wait taken."""
    return _rate_limiter.wait(byte_count)


def flush_tunnel_traffic() -> None:
    """Persist the day's count. Called when a tunnel settles, so a short-lived one
    still leaves its traffic on record."""
    _daily_traffic.flush()
