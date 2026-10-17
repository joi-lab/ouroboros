"""The browser smokes set aside exactly the engine read's own network line, nothing else."""

from tests.ui_console_errors import unexpected_console_errors

ENGINE = "/api/claudexor/maintenance/harnesses"
LINE = "Failed to load resource: the server responded with a status of 503 (Service Unavailable)"


def test_the_refused_engine_read_line_is_set_aside():
    assert unexpected_console_errors([(LINE, ENGINE)], [(ENGINE, 503)]) == []


def test_a_foreign_503_or_a_script_error_still_fails_the_smoke():
    errors = [(LINE, "/api/files/download"), ("ReferenceError: x is not defined", "")]
    failed = [("/api/files/download", 503), (ENGINE, 503)]
    assert unexpected_console_errors(errors, failed) == [LINE, "ReferenceError: x is not defined"]


def test_nothing_is_set_aside_without_a_recorded_refusal_and_only_once_per_refusal():
    assert unexpected_console_errors([(LINE, ENGINE)], []) == [LINE]
    assert unexpected_console_errors([(LINE, ENGINE), (LINE, ENGINE)], [(ENGINE, 503)]) == [LINE]
