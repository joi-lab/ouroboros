"""One logging bootstrap per Ouroboros process, plus the uncaught-exception hooks.

A process configures its logging at its real start, never as a side effect of
which module happened to be ``__main__``: the server from ``server.main()``
whatever the entry point (``python server.py``, ``ouroboros server``, the
``ouroboros-web`` script, Colab), each pool worker at the top of ``worker_main``
after its fork preamble (so a forkserver parent stays single-threaded), and the
launcher keeps its own ``launcher.log`` setup because it ships frozen inside the
app bundle, sharing only ``install_exception_hooks``. The server is the single
writer of ``logs/server.log``; a worker logs to its stderr, which the launcher
copies into ``logs/agent_stdout.log`` (desktop) and Docker keeps as the container
log, so two processes never rotate one file. Every local handler carries the
shared ``SecretRedactingLogFilter``; the module adds handlers beside any a
process already has and never replaces them.

The hooks route an unhandled exception of a thread, or of the main thread, into
``logging`` instead of the interpreter's raw stderr print, so it is formatted like
every other record (its message passes the redaction filter; the traceback itself is
not redacted) and reaches whatever handler an operator or an in-process extension
attached. A hook somebody installed earlier (a test runner,
an error-tracking SDK) still runs after ours. An exception that escapes a
multiprocessing child's target never reaches ``sys.excepthook`` (multiprocessing
catches it first), which is why ``worker_main`` records its own crashes.
"""

from __future__ import annotations

import logging
import pathlib
import sys
import threading
from logging.handlers import RotatingFileHandler
from typing import Any

from ouroboros.observability import SecretRedactingLogFilter

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
SERVER_LOG_MAX_BYTES = 2 * 1024 * 1024
SERVER_LOG_BACKUP_COUNT = 3
UNCAUGHT_LOGGER_NAME = "ouroboros.uncaught"

_configured = False


def configure_process_logging(*, drive_logs: pathlib.Path | None) -> None:
    """Attach this process's local handlers once and install the exception hooks.

    ``drive_logs`` is the data root's ``logs/`` directory and is given only by
    the server process, the single writer of ``server.log``; any other caller
    gets a stderr stream handler alone. A second call in the same process
    changes nothing.
    """
    global _configured
    if _configured:
        return
    _configured = True
    handlers: list[logging.Handler] = []
    if drive_logs is not None:
        drive_logs = pathlib.Path(drive_logs)
        drive_logs.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            drive_logs / "server.log", maxBytes=SERVER_LOG_MAX_BYTES,
            backupCount=SERVER_LOG_BACKUP_COUNT, encoding="utf-8",
        ))
    handlers.append(logging.StreamHandler())
    formatter = logging.Formatter(LOG_FORMAT)
    root = logging.getLogger()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(SecretRedactingLogFilter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    # httpx logs each request URL at INFO; polling transports put credentials in
    # the URL path, so even redacted lines are noise at this level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    install_exception_hooks()


def install_exception_hooks() -> None:
    """Route uncaught thread and main-thread exceptions into ``logging``; idempotent."""
    if getattr(threading.excepthook, "_ouroboros_hook", False):
        return
    previous_thread_hook = threading.excepthook
    previous_sys_hook = sys.excepthook
    # The interpreter defaults only print the traceback raw; our record replaces
    # that print. Anything else installed earlier keeps running after ours.
    chain_thread = previous_thread_hook is not threading.__excepthook__
    chain_sys = previous_sys_hook is not sys.__excepthook__

    def thread_hook(args: Any) -> None:
        name = getattr(args.thread, "name", None) or "unknown"
        exc_info = (args.exc_type, args.exc_value, args.exc_traceback)
        if args.exc_type is SystemExit or not _log_uncaught(f"Uncaught exception in thread {name}", exc_info):
            previous_thread_hook(args)
        elif chain_thread:
            previous_thread_hook(args)

    def sys_hook(exc_type: Any, exc_value: Any, exc_traceback: Any) -> None:
        exc_info = (exc_type, exc_value, exc_traceback)
        if issubclass(exc_type, KeyboardInterrupt) or not _log_uncaught("Uncaught exception", exc_info):
            previous_sys_hook(exc_type, exc_value, exc_traceback)
        elif chain_sys:
            previous_sys_hook(exc_type, exc_value, exc_traceback)

    thread_hook._ouroboros_hook = True  # type: ignore[attr-defined]
    sys_hook._ouroboros_hook = True  # type: ignore[attr-defined]
    threading.excepthook = thread_hook
    sys.excepthook = sys_hook


def _log_uncaught(message: str, exc_info: Any) -> bool:
    """Record one uncaught exception; False leaves it to the previous hook.

    With no root handler (logging never configured, or torn down at exit) the
    record would reach nobody, so the interpreter's own print stays in charge.
    """
    if not logging.getLogger().handlers:
        return False
    try:
        logging.getLogger(UNCAUGHT_LOGGER_NAME).error(message, exc_info=exc_info)
    except Exception:
        return False
    return True
