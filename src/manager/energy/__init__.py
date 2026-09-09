"""Node energy cost monitoring (issue #258).

Informational only: watts, a derived currency cost, and a per-instance split.
Does not feed MU pricing, billing, or ``low_demand`` budgets.

``energy_tick()`` is the public lifecycle hook. Importing this package must not
raise; every config read has a default.
"""


def __getattr__(name):
    # Lazy so `from src.manager.energy.backends import RaplBackend` (and the
    # tests that do that) does not pull ConfigManager / logger, which need a
    # config.yaml. `from src.manager.energy import energy_tick` still works.
    if name == "energy_tick":
        from src.manager.energy.monitor import energy_tick

        return energy_tick
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["energy_tick"]
