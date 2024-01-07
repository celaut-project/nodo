from reputation_graph import compute, spend


"""
¿Que debería de ser el pointer? -> agent id ¿?
"""


def assign_good_reputation(pointer: str, amount: int = 10):
    spend("", amount, pointer)


def assign_bad_reputation(pointer: str, amount: int = 10):
    spend("", (-1) * amount, pointer)


def reputation_feedback(pointer):
    return compute(pointer, pointer)
