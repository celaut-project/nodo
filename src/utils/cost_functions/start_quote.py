"""The quote that a node signs in the ``start`` movement of an instance (#652).

A node writes its maintenance terms for a new instance in ``detail.quote`` of the
``start`` movement of the instance (``statement_check.start_quote_detail``). The
statement signature covers that movement, so the node signs its quote and binds it to the
account of the instance. A third party then checks a charge against the quote with the
signed statements alone (``fault_proofs``).

The terms are those of ``peer_quotes.maintenance_terms``, and they are the figures that
the ticks of the instance charge: the resources that the node reserved, at the price of
the launch, held to ``(1 + v)^k`` of the bound of the launch. A quote here is never lower
than what a tick takes, so an honest node is never accused.
"""
from typing import Mapping, Optional

from protos import celaut_pb2 as celaut
from src.utils.config import ConfigManager
from src.utils.cost_functions.execution_cost import charge_at_launch_price
from src.utils.cost_functions.price_bound import PriceBound

env_manager = ConfigManager()

SHAPE_KEYS = ("mem_limit", "disk_space", "cpu_period", "cpu_quota")


def _seconds() -> int:
    return int(env_manager.get("MANAGER_ITERATION_TIME"))


def _sysresources(shape: Mapping) -> celaut.Sysresources:
    return celaut.Sysresources(**{key: int(shape.get(key) or 0) for key in SHAPE_KEYS})


def shape_of(resources: celaut.Sysresources) -> dict:
    return {key: int(getattr(resources, key)) for key in SHAPE_KEYS}


def local_terms(start: celaut.Sysresources, most: celaut.Sysresources,
                launch_bp: Mapping[str, int], bound: Optional[PriceBound],
                arch: Optional[str], on_close: str, closes_at: int) -> dict:
    """The terms of a local instance: ``start`` is what the node reserved, ``most`` the
    most that the instance may grow to, both after the floors of the node."""
    seconds = _seconds()
    return {
        "init_maintenance_cost": int(charge_at_launch_price(start, seconds, launch_bp, arch)),
        "max_maintenance_cost": int(charge_at_launch_price(most, seconds, launch_bp, arch)),
        "maintenance_seconds_loop": seconds,
        "price_bounded": bound is not None,
        "price_volatility": None if bound is None else float(bound.volatility),
        "volatility_period_seconds": None if bound is None else int(bound.period_seconds),
        "on_close": str(on_close),
        "closes_at": int(closes_at or 0),
    }


def delegated_terms(shape: Mapping) -> Optional[dict]:
    """The terms of an instance that this node gave to a peer, from the shape that its
    ticks charge (``delegated_price.priced_shape``): the start and the most, at the
    multipliers of the launch, with no volatility (#577, #636). None with no shape."""
    if not shape or not shape.get("launch_bp"):
        return None
    seconds = _seconds()
    start = {key: shape.get(f"base_{key}", shape.get(key)) for key in SHAPE_KEYS}
    most = {key: shape.get(f"max_{key}", start[key]) for key in SHAPE_KEYS}
    launch_bp, arch = shape["launch_bp"], shape.get("arch")
    return {
        "init_maintenance_cost": int(charge_at_launch_price(
            _sysresources(start), seconds, launch_bp, arch)),
        "max_maintenance_cost": int(charge_at_launch_price(
            _sysresources(most), seconds, launch_bp, arch)),
        "maintenance_seconds_loop": seconds,
        "price_bounded": True,
        "price_volatility": 0.0,
        "volatility_period_seconds": 0,
        "on_close": "refuse",
        "closes_at": 0,
    }
