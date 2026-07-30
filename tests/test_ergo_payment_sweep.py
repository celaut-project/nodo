"""Cold-wallet sweep threshold tests (#186 phase 2.6/2.7) — pure integer nanoERG."""
from src.payment_system.contracts.ergo.interface import (
    DEFAULT_FEE,
    SAFE_MIN_BOX_VALUE,
    compute_sweep_amount,
)

NANO = 1_000_000_000
HOT = 100 * NANO           # hot-wallet limit
MIN = 1 * NANO             # cold-wallet minimum transfer


def test_balance_exactly_at_limit_sweeps_nothing():
    # balance == hot limit -> excess is negative (needs the fee too) -> no sweep.
    assert compute_sweep_amount(HOT, HOT, MIN, DEFAULT_FEE) is None


def test_excess_below_minimum_sweeps_nothing():
    # excess = 0.5 ERG < MIN (1 ERG) -> no sweep.
    balance = HOT + DEFAULT_FEE + (MIN // 2)
    assert compute_sweep_amount(balance, HOT, MIN, DEFAULT_FEE) is None


def test_insufficient_fee_sweeps_nothing():
    # Just above the hot limit but not enough to also cover the fee.
    balance = HOT + (DEFAULT_FEE // 2)
    assert compute_sweep_amount(balance, HOT, MIN, DEFAULT_FEE) is None


def test_excess_at_minimum_sweeps_exactly_the_excess():
    balance = HOT + DEFAULT_FEE + MIN
    swept = compute_sweep_amount(balance, HOT, MIN, DEFAULT_FEE)
    assert swept == MIN
    assert isinstance(swept, int)


def test_large_excess_sweeps_all_above_hot_limit_and_fee():
    balance = HOT + DEFAULT_FEE + (50 * NANO)
    assert compute_sweep_amount(balance, HOT, MIN, DEFAULT_FEE) == 50 * NANO


def test_excess_below_technical_min_box_value_is_not_a_valid_output():
    # min_transfer tiny, but the excess is below the technical minimum box value.
    tiny_min = 1
    balance = HOT + DEFAULT_FEE + (SAFE_MIN_BOX_VALUE - 1)
    assert compute_sweep_amount(balance, HOT, tiny_min, DEFAULT_FEE) is None
