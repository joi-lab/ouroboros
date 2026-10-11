"""Entry point: asserts toy project behavior end to end."""

import config
import core
import legacy
import models
import report
import utils


def main():
    assert core.aaa(10) == 30
    assert core.run_core(-2) == 18
    assert core.ddd_helper(-5) == 5
    assert utils.scale_all([1, 2]) == [3, 6]
    assert utils.local_shadow() == 51
    assert utils.indirect(4) == 12
    assert utils.prepared_sum([-1, 2]) == 3
    w = models.Widget(5)
    assert w.ddd() == 4
    assert models.widget_total(w) == 17
    assert config.describe() == "ddd is enabled"
    assert report.render(3) == "[9]"
    assert legacy.legacy_flow(1) == 9
    print("OK")


if __name__ == "__main__":
    main()
