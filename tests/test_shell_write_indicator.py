"""Focused regressions for the coarse shell write-shape classifier."""

from ouroboros.tools.shell_guards import shell_has_write_indicator


def test_urlopen_is_not_mistaken_for_open_write_shape():
    command = (
        "python3 -c \"import urllib.request; "
        "print(urllib.request.urlopen('https://example.com').status)\""
    )

    assert shell_has_write_indicator(command) is False


def test_builtin_open_write_shape_remains_detected():
    command = "python3 -c \"open('result.txt', 'w').write('done')\""

    assert shell_has_write_indicator(command) is True
