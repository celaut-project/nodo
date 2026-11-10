"""The interruption class of a candidate (issue #344, section 6).

A class, not a number added to the score: a term on another scale is the defect that
#282 section 2 describes. The sort key of the balancer is: price bound, then this class,
then the score. The class compares what the node said it does at closing time
(``EstimatedCost.on_close``) and when (``closes_at``) with the funded life of the instance
(``funded_life``). From best to worst:

| Class | on_close            | The close                          |
|-------|---------------------|------------------------------------|
| 0     | REFUSE, or no close | any                                |
| 1     | HIBERNATE           | at or after the end of funded life |
| 2     | STOP                | at or after the end of funded life |
| 3     | HIBERNATE           | before the end of funded life      |
| 4     | STOP                | before the end of funded life      |

A stop is always worse than a hibernation in the same position, and a close inside the
funded life is worse than one outside it. Class 2 comes before class 3: a stop that may
never come, against a pause that will come. This is a judgement; change the order here if
the review says otherwise. The distance of ``closes_at`` is not used inside a class. No
candidate is removed.
"""
import time
from typing import Optional

from protos import celaut_pb2
from src.utils.cost_functions.funded_life import funded_life_seconds

NO_INTERRUPTION = 0
HIBERNATE_OUTSIDE_LIFE = 1
STOP_OUTSIDE_LIFE = 2
HIBERNATE_INSIDE_LIFE = 3
STOP_INSIDE_LIFE = 4


def interruption_class(estimated_cost, initial_mu: Optional[int],
                       now: Optional[float] = None) -> int:
    """The class of the quote ``estimated_cost`` for a start funded with ``initial_mu``
    (None: no ``initial_mu``, the default periods of the node). ``now`` is a unix time."""
    on_close = int(getattr(estimated_cost, "on_close", celaut_pb2.ON_CLOSE_REFUSE))
    closes_at = int(getattr(estimated_cost, "closes_at", 0) or 0)
    if on_close not in (celaut_pb2.ON_CLOSE_STOP, celaut_pb2.ON_CLOSE_HIBERNATE) or closes_at <= 0:
        return NO_INTERRUPTION
    now = time.time() if now is None else now
    end_of_life = now + funded_life_seconds(estimated_cost, initial_mu)
    outside = closes_at >= end_of_life
    if on_close == celaut_pb2.ON_CLOSE_HIBERNATE:
        return HIBERNATE_OUTSIDE_LIFE if outside else HIBERNATE_INSIDE_LIFE
    return STOP_OUTSIDE_LIFE if outside else STOP_INSIDE_LIFE
