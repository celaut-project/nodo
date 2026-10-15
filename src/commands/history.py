"""``nodo earnings``, ``nodo energy``, ``nodo schedule`` -- the TUI's history pages.

Three pages of ``nodo tui`` that until now had no command behind them, because
each folds a catalogue table the TUI reads directly:

* EARNINGS -- ``payments``, money in per payment network over rolling windows
  (``get_earnings`` in app.rs). The other half of that page, the reputation
  staked on this node, already is a command: ``nodo reputation --json``.
* ENERGY -- the latest ``energy_consumption`` sample and that table folded into
  local hours (``get_node_energy`` / ``get_energy_series``), beside the
  ``energy:`` config block.
* SCHEDULE -- ``activity_window`` as configured, whether it is open now, and
  ``demand_history`` folded onto the 24 hours of a clock (``get_demand_by_hour``).

The SQL is the TUI's, so the numbers agree with the screen. Every amount is
returned in MU as a string-safe int (``*_mu``) beside its display form: an MU
total can exceed 2^53, which is also why the TUI sums them outside SQL.
All three are read-only; their settings are written with ``nodo config set``.
"""

from typing import Any, Dict, List

from src.commands import _catalogue as catalogue

WINDOWS = ("day", "week", "month", "year")


def _database():
    from src.utils.config import ConfigManager
    return catalogue.connect(ConfigManager().get("DATABASE_FILE"))


def _config_block(key: str) -> Any:
    from src.utils.config import ConfigManager
    return ConfigManager().get(key)


# --- earnings ---------------------------------------------------------------

def earnings_report(connection) -> List[Dict[str, Any]]:
    """What each payment network brought in, per rolling window.

    Only ``direction = 'in'`` and ``status = 'accepted'`` is earned; ``rejected``
    is counted apart as ``refused_mu`` (a deposit this node could not validate).
    An unparseable amount is dropped, never read as zero.
    """
    if not catalogue.table_exists(connection, "payments"):
        return []
    rows = connection.execute(
        "SELECT COALESCE(NULLIF(TRIM(ledger), ''), 'unknown') AS ledger, status, amount_mu, "
        "COALESCE(created_at, '') >= datetime('now', '-1 day')    AS in_day, "
        "COALESCE(created_at, '') >= datetime('now', '-7 days')   AS in_week, "
        "COALESCE(created_at, '') >= datetime('now', '-30 days')  AS in_month, "
        "COALESCE(created_at, '') >= datetime('now', '-365 days') AS in_year "
        "FROM payments WHERE direction = 'in'"
    ).fetchall()
    by_ledger: Dict[str, Dict[str, int]] = {}
    for row in rows:
        amount = catalogue.mu_or_none(row["amount_mu"])
        if amount is None:
            continue
        entry = by_ledger.setdefault(row["ledger"], {
            "total_mu": 0, **{f"{w}_mu": 0 for w in WINDOWS}, "refused_mu": 0})
        if row["status"] == "rejected":
            entry["refused_mu"] += amount
            continue
        if row["status"] != "accepted":
            continue
        entry["total_mu"] += amount
        for window in WINDOWS:
            if row[f"in_{window}"]:
                entry[f"{window}_mu"] += amount
    return [{"ledger": ledger, **sums} for ledger, sums in sorted(by_ledger.items())]


def earnings(argv=None) -> bool:
    """``nodo earnings [--json]``."""
    from src.utils.monetary import format_mu

    argv = list(argv or [])
    as_json = "--json" in argv
    connection = _database()
    try:
        report = earnings_report(connection)
    except Exception as e:
        return catalogue.emit_error(as_json, f"Could not read payments: {e}")
    finally:
        connection.close()
    if as_json:
        catalogue.emit_json({"earnings": report})
        return True
    if not report:
        print("Nothing earned yet: no incoming payment is recorded.")
    else:
        print(f"{'Network':<12}{'All time':>16}" + "".join(f"{'Last ' + w:>16}" for w in WINDOWS))
        for entry in report:
            print(f"{entry['ledger']:<12}{format_mu(entry['total_mu']):>16}"
                  + "".join(f"{format_mu(entry[w + '_mu']):>16}" for w in WINDOWS))
            if entry["refused_mu"]:
                print(f"{'':<12}refused deposits: {format_mu(entry['refused_mu'])}")
    print("\nReputation staked on this node: `nodo reputation` (--json).")
    return True


# --- energy -----------------------------------------------------------------

def energy_report(connection, hours: int) -> Dict[str, Any]:
    report: Dict[str, Any] = {"latest": None, "hourly": []}
    if not catalogue.table_exists(connection, "energy_consumption"):
        return report
    row = connection.execute(
        "SELECT timestamp, watts, price_per_kwh, currency, backend, is_floor "
        "FROM energy_consumption ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        report["latest"] = {**dict(row), "is_floor": bool(row["is_floor"])}
    # Grouped by local hour; `timestamp` is UTC (CURRENT_TIMESTAMP), so the bound
    # compares in UTC and only the bucket key is shifted -- as the TUI does.
    report["hourly"] = [dict(r) for r in connection.execute(
        "SELECT strftime('%Y-%m-%dT%H', timestamp, 'localtime') AS hour, "
        "MAX(watts) AS peak_watts, SUM(energy_joules) AS joules, "
        "SUM(energy_joules / 3.6e6 * price_per_kwh) AS cost "
        "FROM energy_consumption WHERE timestamp >= datetime('now', ?) "
        "GROUP BY 1 ORDER BY 1 ASC", (f"-{hours} hours",))]
    return report


def energy(argv=None) -> bool:
    """``nodo energy [--json] [--hours N]`` (default 720 = the TUI's month)."""
    argv = list(argv or [])
    as_json = "--json" in argv
    try:
        hours = _take_int(argv, "--hours", 720)
    except ValueError as e:
        return catalogue.emit_error(as_json, str(e))
    connection = _database()
    try:
        report = energy_report(connection, hours)
    except Exception as e:
        return catalogue.emit_error(as_json, f"Could not read energy_consumption: {e}")
    finally:
        connection.close()
    report["config"] = _config_block("energy")
    report["hours"] = hours
    if as_json:
        catalogue.emit_json(report)
        return True
    latest = report["latest"]
    if latest:
        print(f"Now: {latest['watts']:.1f} W{' (floor)' if latest['is_floor'] else ''} "
              f"via {latest['backend'] or '?'} at {latest['timestamp']} UTC; "
              f"{latest['price_per_kwh'] or 0} {latest['currency'] or ''}/kWh")
    else:
        print("No energy sample recorded yet.")
    hourly = report["hourly"]
    if hourly:
        peak = max(hourly, key=lambda h: h["peak_watts"] or 0)
        print(f"Last {hours}h: {len(hourly)} hours sampled, peak {peak['peak_watts']:.1f} W at "
              f"{peak['hour']}, {sum(h['joules'] or 0 for h in hourly) / 3.6e6:.3f} kWh, "
              f"cost {sum(h['cost'] or 0 for h in hourly):.4f}")
    print("Config (energy:):")
    for key, value in (report["config"] or {}).items():
        print(f"  {key}: {value}")
    return True


# --- schedule ---------------------------------------------------------------

def demand_by_hour(connection, days: int) -> Dict[str, List[int]]:
    """Peak instances held and launches refused-while-closed, per hour of the day."""
    held, refused = [0] * 24, [0] * 24
    if catalogue.table_exists(connection, "demand_history"):
        for hour, peak, closed in connection.execute(
            "SELECT CAST(substr(hour, -2) AS INTEGER), MAX(instances_held), SUM(refused_closed) "
            "FROM demand_history WHERE hour >= strftime('%Y-%m-%dT%H', 'now', 'localtime', ?) "
            "GROUP BY substr(hour, -2)", (f"-{days} days",)):
            if hour is not None and 0 <= hour < 24:
                held[hour], refused[hour] = max(peak or 0, 0), max(closed or 0, 0)
    return {"held": held, "refused": refused}


def schedule(argv=None) -> bool:
    """``nodo schedule [--json] [--days N]`` (default 30 = the TUI's month)."""
    from src.utils import activity_window

    argv = list(argv or [])
    as_json = "--json" in argv
    try:
        days = _take_int(argv, "--days", 30)
    except ValueError as e:
        return catalogue.emit_error(as_json, str(e))
    connection = _database()
    try:
        demand = demand_by_hour(connection, days)
    except Exception as e:
        return catalogue.emit_error(as_json, f"Could not read demand_history: {e}")
    finally:
        connection.close()
    report = {
        "config": _config_block("activity_window"),
        "enabled": activity_window.is_enabled(),
        "open_now": activity_window.is_open(),
        "windows": [[start.strftime("%H:%M"), end.strftime("%H:%M")]
                    for start, end in activity_window.windows()],
        "stops_running_instances": activity_window.stops_running_instances(),
        "days": days,
        "demand_by_hour": demand,
    }
    if as_json:
        catalogue.emit_json(report)
        return True
    if not report["enabled"]:
        print("Schedule: not enforced -- work is taken at any hour.")
    else:
        spans = ", ".join(f"{a}-{b}" for a, b in report["windows"]) or "none"
        print(f"Schedule: {'OPEN' if report['open_now'] else 'CLOSED'} now; windows {spans}; "
              f"at closing time {'stop' if report['stops_running_instances'] else 'refuse new work'}.")
    print(f"Demand by hour of day, last {days} days (peak held / refused while closed):")
    for hour in range(24):
        print(f"  {hour:02d}:00  {demand['held'][hour]:>4}  {demand['refused'][hour]:>4}")
    return True


def _take_int(argv: List[str], flag: str, default: int) -> int:
    for index, argument in enumerate(argv):
        if argument == flag or argument.startswith(flag + "="):
            text = argument.split("=", 1)[1] if "=" in argument else (
                argv[index + 1] if index + 1 < len(argv) else "")
            try:
                value = int(text)
            except ValueError:
                raise ValueError(f"{flag} needs a whole number, e.g. `{flag} {default}`.")
            if value <= 0:
                raise ValueError(f"{flag} must be positive.")
            return value
    return default
