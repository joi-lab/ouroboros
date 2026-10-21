"""The browser smokes set aside exactly the engine read's own network line, nothing else."""

from types import SimpleNamespace

import pytest

from tests.ui_console_errors import PageErrors, unexpected_console_errors

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


class _Page:
    """Events PageErrors reads, including Playwright's split WebKit error name."""

    def __init__(self):
        self.handlers, self.main_frame = {}, SimpleNamespace(url="")

    def on(self, event, handler):
        self.handlers[event] = handler

    def error(self, text, name=""):
        error = Exception(text)
        error.name = name
        self.handlers["pageerror"](error)

    def commit(self, url, frame=None):
        frame = frame or self.main_frame
        frame.url = url
        self.handlers["framenavigated"](frame)


OWN = "/127.0.0.1:65140/api/state due to access control checks."
CORS = "/localhost:65140/api/state due to access control checks."


def test_only_webkits_report_of_the_pages_own_fetch_is_set_aside():
    page = _Page()
    errors = PageErrors(page, "webkit")
    page.commit("http://127.0.0.1:65140/")
    for text in (OWN, CORS, "TypeError: undefined is not an object"):
        page.error(text, name="Fetch API cannot load http")
    assert errors == [OWN, CORS, "TypeError: undefined is not an object"]
    # Another origin (here only the host differs) is a real cross-origin refusal.
    assert errors.unexpected() == [CORS, "TypeError: undefined is not an object"]


def test_the_same_text_counts_for_an_origin_the_main_frame_never_committed_or_outside_webkit():
    page = _Page()
    errors = PageErrors(page, "webkit")
    page.commit("http://127.0.0.1:65140/", SimpleNamespace(url=""))  # a child frame is not the page
    page.error(OWN, name="Fetch API cannot load http")
    assert errors.unexpected() == [OWN]
    page = _Page()
    chromium = PageErrors(page, "chromium")
    page.commit("http://127.0.0.1:65140/")
    page.error(OWN, name="Fetch API cannot load http")
    assert chromium.unexpected() == [OWN]


def test_an_earlier_visited_origin_is_not_exempt_after_navigation():
    page = _Page()
    errors = PageErrors(page, "webkit")
    page.commit("http://localhost:65140/")  # B
    page.commit("http://127.0.0.1:65140/")  # A
    page.error(CORS, name="Fetch API cannot load http")  # A fetches B without CORS
    assert errors.unexpected() == [CORS]


@pytest.mark.parametrize("split", [False, True])
def test_a_different_scheme_is_cross_origin_even_at_the_same_host_and_port(split):
    page = _Page()
    errors = PageErrors(page, "webkit")
    page.commit("http://127.0.0.1:65140/")
    text = OWN if split else "Fetch API cannot load https:/" + OWN
    page.error(text, name="Fetch API cannot load https" if split else "")
    assert errors.unexpected() == [text]


def test_reload_and_leaving_document_errors_are_classified_when_received():
    page = _Page()
    errors = PageErrors(page, "webkit")
    page.commit("http://127.0.0.1:65140/first")
    page.error(OWN, name="Fetch API cannot load http")  # cancelled by reload
    page.commit("http://127.0.0.1:65140/first")
    page.error(OWN, name="Fetch API cannot load http")  # cancelled before navigation commits
    page.commit("http://localhost:65140/second")
    assert errors == [OWN, OWN]
    assert errors.unexpected() == []
    page.error(OWN, name="Fetch API cannot load http")  # after commit: no old-origin exemption
    assert errors.unexpected() == [OWN]


def test_a_later_navigation_cannot_hide_an_earlier_cross_origin_error():
    page = _Page()
    errors = PageErrors(page, "webkit")
    page.commit("http://127.0.0.1:65140/")
    page.error(CORS, name="Fetch API cannot load http")
    page.commit("http://localhost:65140/")
    assert errors.unexpected() == [CORS]


def test_the_verdict_stays_with_its_entry_when_the_list_changes():
    page = _Page()
    errors = PageErrors(page, "webkit")
    page.commit("http://127.0.0.1:65140/")
    page.error(OWN, name="Fetch API cannot load http")
    errors.clear()
    page.error("TypeError: undefined is not an object")  # lands where the exempt entry was
    assert errors.unexpected() == ["TypeError: undefined is not an object"]
    errors.insert(0, "ReferenceError: x is not defined")  # an adopter's own entry counts
    page.error(OWN, name="Fetch API cannot load http")
    errors.sort()
    assert errors.unexpected() == ["ReferenceError: x is not defined", "TypeError: undefined is not an object"]
    assert errors == ["/127.0.0.1:65140/api/state due to access control checks.",
                      "ReferenceError: x is not defined", "TypeError: undefined is not an object"]


def test_an_unrecoverable_scheme_is_not_assumed_to_be_the_pages_scheme():
    page = _Page()
    errors = PageErrors(page, "webkit")
    page.commit("http://127.0.0.1:65140/")
    page.error(OWN)
    assert errors.unexpected() == [OWN]


def test_full_urls_default_ports_and_an_already_committed_page():
    page = _Page()
    page.main_frame.url = "https://example.test/"
    errors = PageErrors(page, "webkit")
    page.error("Fetch API cannot load https://example.test:443/api/state due to access control checks.")
    assert errors.unexpected() == []
    foreign_port = "https://example.test:444/api/state due to access control checks."
    page.error(foreign_port)
    assert errors.unexpected() == [foreign_port]
