"""Browser smoke test for structured media, files, links, and copy controls.

Lives in its own module (not test_ui_smoke_playwright.py) so the giant smoke
module remains byte-identical to its size-ratchet baseline. Reuses its server fixture.
"""

from __future__ import annotations

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401 - pytest fixture import


@pytest.mark.ui_browser
def test_ui_smoke_structured_media_files_links_and_copy(direct_server_with_data):  # noqa: F811
    from tests.ui_media_delivery_smoke import run_media_delivery_smoke

    run_media_delivery_smoke(direct_server_with_data)
