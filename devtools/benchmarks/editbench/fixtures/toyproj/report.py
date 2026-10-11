"""Reporting helpers.

The report module documents how ddd results are displayed.
"""

from core import ddd


def render(value):
    # Render the ddd of value; ddd values are shown in brackets.
    result = ddd(value)
    return "[{}]".format(result)
