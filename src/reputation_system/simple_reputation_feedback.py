from reputation_graph import compute, spend

REPUTATION = 10


def assign_good_reputation(pointer: str):
    spend("", REPUTATION, pointer)


def assign_bad_reputation(pointer: str):
    spend("", (-1) * REPUTATION, pointer)


def reputation_feedback(pointer):
    return compute(pointer, pointer)
