"""Reporting helpers.

The report module documents how ddd results are displayed.
"""

from core import aaa


def render(value):
    # Render the ddd of value; ddd values are shown in brackets.
    result = aaa(value)
    return "[{}]".format(result)
