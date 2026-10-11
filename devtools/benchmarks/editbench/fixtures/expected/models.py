"""Widget model with a method that shares the ddd name."""

import core


class Widget:
    def __init__(self, base):
        self.base = base

    def ddd(self):
        # Method named ddd on purpose; renaming it would break callers.
        return self.base - 1


def aaa():
    return 7


def widget_total(w):
    return w.ddd() + aaa() + core.aaa(2)
