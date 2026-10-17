"""Console errors a browser smoke owns, apart from the engine reads its fixture cannot answer.

The direct-mode UI fixture runs no Claudexor engine. Since #1670 the Accounts section reads
the engine's program inventory once at boot (`web/modules/harness_accounts.js` `_init`), and
the host answers with its typed refusal, 503 (docs/architecture/06-agent-core.md "Vendor
program maintenance": inspection never starts an engine). The page shows "Program inspection
unavailable"; only the browser's own network line reaches the console. Nothing else is set aside.
"""

from __future__ import annotations

from urllib.parse import urlsplit

ENGINE_READS = frozenset({"/api/claudexor/maintenance/harnesses"})


def record_console_errors(page, *, page_errors: bool = True) -> list[tuple[str, str]]:
    """Every console error as (text, source path); an uncaught page error has no source."""
    errors: list[tuple[str, str]] = []
    page.on("console", lambda message: errors.append(
        (message.text, urlsplit((message.location or {}).get("url") or "").path))
        if message.type == "error" else None)
    if page_errors:
        page.on("pageerror", lambda error: errors.append((str(error), "")))
    return errors


def record_failed_loads(page) -> list[tuple[str, int]]:
    """Every response the page receives with an HTTP error status, as (path, status)."""
    failed: list[tuple[str, int]] = []
    page.on("response", lambda response: failed.append((urlsplit(response.url).path, response.status))
            if response.status >= 400 else None)
    return failed


def unexpected_console_errors(console_errors, failed_loads) -> list[str]:
    """The console errors left once each refused engine read's own load line is set aside: only
    a network line whose source is that engine read, at most once per recorded 503 of it."""
    left = list(console_errors)
    for path, status in failed_loads:
        if path not in ENGINE_READS or status != 503:
            continue
        line = f"Failed to load resource: the server responded with a status of {status} "
        match = next((entry for entry in left if entry[1] == path and entry[0].startswith(line)), None)
        if match is not None:
            left.remove(match)
    return [text for text, _source in left]
