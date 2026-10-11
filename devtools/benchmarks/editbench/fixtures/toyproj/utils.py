"""Utility layer built on top of core."""

from core import ddd, ddd_helper

addd = 41  # unrelated constant whose name merely contains ddd


def scale_all(values):
    return [ddd(v) for v in values]


def local_shadow():
    ddd = 10  # local variable named ddd; it is not a function
    return ddd + addd


def indirect(value):
    alias = ddd
    return alias(value)


def prepared_sum(values):
    return sum(ddd_helper(v) for v in values)
