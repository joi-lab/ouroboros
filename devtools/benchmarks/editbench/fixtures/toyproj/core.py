"""Core numeric helpers for the toy project."""

DEFAULT_SCALE = 3


def ddd(value):
    """Triple the value.

    Note: ddd is short for "triple-d"; see README.md for why ddd
    got this name.
    """
    return value * DEFAULT_SCALE


def ddd_helper(value):
    # ddd_helper prepares input for ddd but is NOT the same function.
    return abs(int(value))


def run_core(value):
    prepared = ddd_helper(value)
    # ddd is applied twice here on purpose.
    return ddd(ddd(prepared))
