"""CLI and port-binding helpers extracted from server.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import logging
import os
import pathlib
import socket

log = logging.getLogger(__name__)


def _can_bind_port(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(host: str, start: int = 8765, max_tries: int = 10,
                   wait_retries: int = 20, wait_interval: float = 0.5) -> int:
    """Prefer the old port during restart before scanning nearby fallbacks."""
    import time

    for attempt in range(wait_retries):
        if _can_bind_port(host, start):
            return start
        if attempt < wait_retries - 1:
            time.sleep(wait_interval)

    # Fallback ports may also be winding down; retry the whole range.
    fallback_ports = range(start + 1, start + max_tries)
    for attempt in range(wait_retries):
        for port in fallback_ports:
            if _can_bind_port(host, port):
                return port
        if attempt < wait_retries - 1:
            time.sleep(wait_interval)

    raise OSError(f"No free port available in range {start}-{start + max_tries - 1}")


def parse_server_args(default_host: str, default_port: int) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Ouroboros web server.")
    parser.add_argument(
        "--host",
        default=None,
        help=f"Host interface to bind (default: {default_host} or OUROBOROS_SERVER_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help="Port to bind (default: %(default)s or OUROBOROS_SERVER_PORT).",
    )
    args = parser.parse_args()
    args.host_explicit = args.host is not None
    if not args.host_explicit:
        args.host = default_host
    return args


def write_port_file(port_file: pathlib.Path, port: int) -> None:
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port), encoding="utf-8")


@contextmanager
def bound_service_socket(drive_root: pathlib.Path, service: str, host: str, port: int,
                         *, server_host_source: str | None = None):
    """Keep the actual bound socket and its informational identity in one lifetime.

    The existing port selector chooses the port. Uvicorn accepts this socket on
    Linux, macOS and Windows; no second probe/rebind race or process authority.
    """
    from ouroboros.server_process import clear_service_binding, record_service_binding, record_applied_restart_settings

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    binding = None
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        address = sock.getsockname()
        if service in {"main", "host_service"}:
            record_applied_restart_settings({
                "OUROBOROS_SERVER_HOST" if service == "main" else "OUROBOROS_HOST_SERVICE_PORT":
                    host if service == "main" else address[1],
            }, server_host_source=server_host_source if service == "main" else None)
        try:
            binding = record_service_binding(drive_root, service, address[0], address[1], pid=os.getpid())
        except Exception:
            # Endpoint discovery is informational. Main still writes its actual
            # port, which keeps restricted browsers conservative on this address.
            log.warning("Could not record %s service binding; continuing with the bound socket", service, exc_info=True)
        yield sock
    finally:
        sock.close()
        if binding is not None:
            try:
                clear_service_binding(drive_root, service, binding)
            except Exception:
                log.warning("Could not retire %s service binding", service, exc_info=True)
