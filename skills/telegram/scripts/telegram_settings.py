"""Private settings authority shared by the Telegram extension and companion."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from starlette.responses import JSONResponse

try:  # Package import from plugin.py.
    from .platform_support import acquire_file_lock, path_is_link_or_reparse, release_file_lock
except ImportError:  # Direct execution from scripts/companion.py.
    from platform_support import acquire_file_lock, path_is_link_or_reparse, release_file_lock


MINIAPP_MARKER_HEADER = "X-Ouroboros-Telegram-MiniApp"
_SETTINGS_NAME = "settings.json"
_LOCK_NAME = ".settings.lock"
_TRUE = frozenset({"on", "true", "1", "yes"})


class _SocksCredentialsFilter(logging.Filter):
    # HTTPcore 1.0 logs the SOCKS auth tuple at DEBUG, before authentication.
    # Redact that single diagnostic without changing transport or logger levels.
    _telegram_proxy_auth_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if message.startswith("setup_socks5_connection.started ") and " auth=" in message:
            record.msg = message.split(" auth=", 1)[0] + " auth=***"
            record.args = ()
        return True


_socks_logger = logging.getLogger("httpcore.socks")
if not any(getattr(item, "_telegram_proxy_auth_filter", False) for item in _socks_logger.filters):
    _socks_logger.addFilter(_SocksCredentialsFilter())


class TelegramSettingsError(RuntimeError):
    pass


def _settings_path(state_dir: Path) -> Path:
    root = Path(state_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path_is_link_or_reparse(root) or not root.is_dir():
        raise TelegramSettingsError("Telegram state must be a real directory.")
    root = root.resolve(strict=True)
    path = root / _SETTINGS_NAME
    if path_is_link_or_reparse(path):
        raise TelegramSettingsError("Telegram settings are an unsafe link.")
    return path


def load_settings(state_dir: Path) -> dict[str, Any]:
    path = _settings_path(state_dir)
    try:
        path.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise TelegramSettingsError("Could not read Telegram settings.") from exc
    if not path.is_file():
        raise TelegramSettingsError("Telegram settings are invalid.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TelegramSettingsError("Could not read Telegram settings.") from exc
    except (TypeError, ValueError) as exc:
        raise TelegramSettingsError("Telegram settings are invalid.") from exc
    if not isinstance(value, dict):
        raise TelegramSettingsError("Telegram settings are invalid.")
    return value


def owner_chat_id(state_dir: Path) -> int:
    try:
        value = int(str(load_settings(state_dir).get("TELEGRAM_CHAT_ID") or "").strip())
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def validate_telegram_proxy(value: Any) -> str | None:
    """Parse the Telegram-only proxy without echoing its possible credentials."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        url = httpx.Proxy(text).url
        valid = (
            url.scheme in ("http", "https", "socks5", "socks5h")
            and bool(url.host)
            and (url.port is not None or url.scheme in ("http", "https"))
            and (url.port is None or 0 < url.port <= 65535)
            and url.raw_path == b"/" and not url.query and not url.fragment
        )
    except Exception:
        valid = False
    if not valid:
        raise ValueError(
            "TELEGRAM_PROXY must be scheme://[user:password@]host[:port] "
            "with scheme http, https, socks5 or socks5h (SOCKS requires a port)."
        )
    return text


def telegram_proxy(state_dir: Path) -> str | None:
    """The same skill-local setting for bridge, notifier and companion APIs."""
    return validate_telegram_proxy(load_settings(state_dir).get("TELEGRAM_PROXY"))


def miniapp_enabled(state_dir: Path) -> bool:
    raw = str(load_settings(state_dir).get("TELEGRAM_MINIAPP_ENABLED") or "on").strip().lower()
    return raw in _TRUE


def merge_settings(state_dir: Path, patch: dict[str, Any]) -> dict[str, Any]:
    path = _settings_path(state_dir)
    lock_path = path.with_name(_LOCK_NAME)
    if path_is_link_or_reparse(lock_path):
        raise TelegramSettingsError("Telegram settings lock is unsafe.")
    try:
        fd = acquire_file_lock(lock_path, timeout_sec=2.0)
    except Exception as exc:
        raise TelegramSettingsError("Telegram settings are busy.") from exc
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        current = load_settings(path.parent)
        current.update(patch)
        encoded = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write_text(encoded, encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return current
    except OSError as exc:
        raise TelegramSettingsError("Could not persist Telegram settings.") from exc
    finally:
        release_file_lock(fd)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def request_may_change_owner(request: Any) -> bool:
    headers = getattr(request, "headers", {})
    if str(headers.get(MINIAPP_MARKER_HEADER, "")).strip():
        return False
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "").strip()
    return host in {"127.0.0.1", "::1"}


# The exact keys the declarative Settings form owns: POST accepts only these and
# the GET that hydrates the form returns only non-secret fields. The bot token
# belongs to Secrets; the proxy is a masked, write-only skill setting.
_SETTINGS_FORM_KEYS = (
    "TELEGRAM_CHAT_ID", "TELEGRAM_MAX_UPDATES_PER_POLL", "TELEGRAM_MIRROR_MODE",
    "TELEGRAM_COMMAND_MODE", "TELEGRAM_SILENT_MODE",
    "TELEGRAM_SUBAGENT_CARDS", "TELEGRAM_MIRROR_PROGRESS", "TELEGRAM_NOTIFY_TASKS",
    "TELEGRAM_NOTIFY_BUDGET", "TELEGRAM_MINIAPP_ENABLED", "TELEGRAM_PROXY",
)

def make_settings_save(api):
    async def _settings_save(request):
        if str(getattr(request, "method", "POST") or "POST").upper() == "GET":
            # Hydration read for the Settings form: only the form's own keys
            # that are actually stored, as strings, so the UI shows what is
            # saved instead of the schema's first option.
            try:
                stored = load_settings(Path(api.get_state_dir()))
            except TelegramSettingsError as exc:
                return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
            values = {key: str(stored[key]) for key in _SETTINGS_FORM_KEYS
                      if key in stored and key != "TELEGRAM_PROXY"}
            if "TELEGRAM_PROXY" in stored:
                values["telegram_proxy_status"] = "Configured" if stored["TELEGRAM_PROXY"] else "Not configured"
            return JSONResponse(values)
        try:
            data = await request.json()
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "message": "Invalid Telegram settings payload."},
                status_code=400,
            )
        if not isinstance(data, dict):
            return JSONResponse(
                {"ok": False, "message": "Invalid Telegram settings payload."},
                status_code=400,
            )
        payload = {key: data.get(key) for key in _SETTINGS_FORM_KEYS if key in data}
        # Password fields intentionally hydrate empty. An untouched field must
        # preserve its saved value; removal is a separate, explicit form action.
        if data.get("clear_telegram_proxy") is True:
            payload["TELEGRAM_PROXY"] = ""
        elif "TELEGRAM_PROXY" in payload:
            try:
                proxy = validate_telegram_proxy(payload["TELEGRAM_PROXY"])
            except ValueError as exc:
                return JSONResponse({"ok": False, "message": str(exc), "error": str(exc)}, status_code=400)
            if proxy is None:
                payload.pop("TELEGRAM_PROXY")
            else:
                payload["TELEGRAM_PROXY"] = proxy
        owner_ignored = False
        if "TELEGRAM_CHAT_ID" in payload and not request_may_change_owner(request):
            payload.pop("TELEGRAM_CHAT_ID", None)
            owner_ignored = True
        try:
            merge_settings(Path(api.get_state_dir()), payload)
        except TelegramSettingsError as exc:
            return JSONResponse(
                {"ok": False, "message": str(exc)},
                status_code=409,
            )
        message = "Telegram settings saved."
        if "TELEGRAM_PROXY" in payload:
            message += " Saved Telegram proxy: " + ("configured." if payload["TELEGRAM_PROXY"] else "cleared.")
            message += " Disable and re-enable the skill to apply the proxy to all Telegram calls."
        if owner_ignored:
            message += " Owner binding was left unchanged."
        return JSONResponse({"ok": True, "owner_ignored": owner_ignored, "message": message})
    return _settings_save


class TelegramSettingsObserver:
    """Read-only companion view of this skill's own settings."""

    def __init__(self, state_dir: Path, _core_port: int) -> None:
        self.state_dir = state_dir

    def owner_chat_id(self) -> int:
        return owner_chat_id(self.state_dir)

    def safe_for_exposure(self, owner: int) -> bool:
        settings = load_settings(self.state_dir)
        try:
            current_owner = int(str(settings.get("TELEGRAM_CHAT_ID") or "").strip())
        except (TypeError, ValueError):
            current_owner = 0
        enabled = str(settings.get("TELEGRAM_MINIAPP_ENABLED") or "on").strip().lower() in _TRUE
        return owner > 0 and current_owner == owner and enabled

__all__ = [
    "MINIAPP_MARKER_HEADER",
    "TelegramSettingsError",
    "TelegramSettingsObserver",
    "load_settings",
    "merge_settings",
    "make_settings_save",
    "miniapp_enabled",
    "owner_chat_id",
    "request_may_change_owner",
    "telegram_proxy",
    "validate_telegram_proxy",
]
