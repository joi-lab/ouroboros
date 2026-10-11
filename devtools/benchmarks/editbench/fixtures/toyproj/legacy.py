"""Legacy code kept for compatibility."""

import models


def dddx(value):
    # Prefix collision: dddx is unrelated to the ddd function family.
    return value + 1


def legacy_flow(value):
    d = models.ddd
    return d() + dddx(value)
