"""Unix computer-use extension: screenshot + desktop input for macOS/Linux.

The implementation is intentionally substrate-thin: it detects already-installed
platform tools and returns clear unsupported/capability errors instead of
downloading binaries. All GUI actions are meant to run UNDER HUMAN OBSERVATION;
the agent should prefer semantic application APIs when available and ask before
destructive or sensitive UI actions.

Coordinate contract (normalization): ``screenshot`` may downscale the captured
image to fit WXGA (1280x800, Anthropic computer-use guidance) and persists the
exact image->input transform in skill state. Input tools (click/move/drag/...)
accept coordinates in the LAST SCREENSHOT's image space by default and remap
them through that stored transform; pass ``raw=true`` to bypass remapping and
address native input coordinates directly.

Permissions (honest): ``screencapture``/``cliclick`` exit 0 even when a macOS grant is
missing (capturing only the wallpaper, or dropping synthetic input). ``capabilities``
reads the grants through prompt-free preflights in the native helper, and an input
receipt is demoted to a failure when cliclick itself warns that the event was dropped.
Wayland support depends on ydotool (input) and grim/gnome-screenshot (capture); X11
uses xdotool/scrot. Windows desktops use the separate windows_computer_use skill.

Input receipt semantics and partial-failure limits: see SKILL.md, "Input receipts".
"""

from __future__ import annotations

import json
import os
import pathlib
import platform
import re
import shutil
import struct
import subprocess
import sys
import time
import uuid
from typing import Any

from .place import PlaceMixin
from .remote import (  # noqa: F401 - re-exported names the tests and the key/hold_key remote branches use
    _MAX_REMOTE_SHOT_BYTES, _OSWORLD_PKGS_PREFIX, _PYAUTOGUI_BASE_ALIASES, _PYAUTOGUI_MODS, RemoteBackends,
    _osworld_result_ok, cliclick_point,
)

_TIMEOUT_SEC = 10
_NOT_LAUNCHED = -255  # _try: the OS could not start the helper, so nothing was dispatched
# Anthropic computer-use guidance: keep screenshots at/below ~XGA/WXGA so the
# model reasons over a stable, token-cheap coordinate space.
_MAX_IMAGE_W = 1280
_MAX_IMAGE_H = 800
_TRANSFORM_FILE = "coord_transform.json"
_CONNECTIONS_FILE = "connections.json"
_ACTIVE_CONNECTION_FILE = "active_connection.txt"
_REMOTE_BACKENDS = {"osworld_http", "ssh_macos"}


def _png_dimensions(path: pathlib.Path) -> tuple[int, int]:
    """Physical (pixel) width/height from a PNG IHDR; (0, 0) on failure."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(24)
        if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = struct.unpack(">II", header[16:24])
            return int(width), int(height)
    except Exception:
        pass
    return 0, 0


def _png_intact(path: pathlib.Path) -> bool:
    """Full-decode integrity check, not just the IHDR.

    A truncated/zero-padded PNG keeps a valid 24-byte header, so
    ``_png_dimensions`` alone cannot see the damage; in the v6.81.1 OSWorld run
    such a file passed header checks, survived ``_downscale`` (which swallows
    the PIL error and returns the corrupt source) and then killed the whole
    task with a non-retryable provider 400 ("Could not process image"). Decode
    the WHOLE image before ever reporting ok:true. Without PIL, fall back to
    requiring the IEND trailer — weaker, but it still catches truncation.
    """
    try:
        from PIL import Image
    except Exception:
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, path.stat().st_size - 16))
                return b"IEND" in fh.read()
        except Exception:
            return False
    try:
        with Image.open(path) as im:
            im.load()
        return True
    except Exception:
        return False


def _macos_logical_size() -> tuple[int, int]:
    """Logical (point) DESKTOP size via AppleScript; (0, 0) on failure.

    cliclick consumes logical points while ``screencapture`` writes physical
    pixels; exposing both recovers the Retina scale. On a MULTI-DISPLAY Mac
    this is the union of all displays while a no-argument ``screencapture``
    captures only the main display — the derived scale is then approximate.
    """
    try:
        rc, out, _ = _run([
            "osascript", "-e",
            'tell application "Finder" to get bounds of window of desktop',
        ])
        if rc == 0:
            parts = [p.strip() for p in out.replace(",", " ").split()]
            nums = [int(p) for p in parts if p.lstrip("-").isdigit()]
            if len(nums) >= 4:
                return nums[2] - nums[0], nums[3] - nums[1]
    except Exception:
        pass
    return 0, 0


def _helper_python() -> str:
    """Interpreter for the native helper process: the host's agent python when it names one, else this one."""
    return os.environ.get("OUROBOROS_AGENT_PYTHON") or sys.executable


def _pipe(cmd: list[str], text: str, *, timeout: int = _TIMEOUT_SEC) -> tuple[int, str, str]:
    """Run a command with ``text`` on its stdin (the clipboard writer)."""
    proc = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=timeout)
    return int(proc.returncode), proc.stdout or "", proc.stderr or ""


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _which(name: str) -> str:
    return shutil.which(name) or ""


def _coerce_point(x: Any, y: Any) -> tuple[int, int]:
    """Normalize pointer coordinates from the shapes models actually emit.

    Accepted: ints/floats; numeric strings ("663"); and the recurring
    malformation where BOTH coordinates arrive in one string — ``x="663, 500"``
    with ``y`` absent, or with ``y`` a single number duplicating one of the
    pair's. The v6.81.0 OSWorld run hit exactly this 109 times across 100
    tasks, each costing a full round on a ValueError.

    ABSENT (None / empty string) and UNPARSEABLE are different facts: an absent
    y may be recovered from a pair in x; a non-empty y that parses to nothing
    ("invalid") or to several numbers ("500, 42") is contradictory input and
    raises ValueError naming both values — a genuinely garbled call must fail
    loudly, not be guessed into a real pointer action.
    """
    def parse(value: Any, *, name: str) -> list[int] | None:
        """None == genuinely absent; a list is always non-empty; garbage raises."""
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"boolean is not a coordinate: {name}={value!r}")
        if isinstance(value, (int, float)):
            return [int(round(value))]
        text = str(value)
        if not text.strip():
            return None
        found = re.findall(r"-?\d+(?:\.\d+)?", text)
        if not found:
            raise ValueError(f"cannot parse {name} coordinate from {value!r}")
        return [int(round(float(n))) for n in found]

    xs = parse(x, name="x")
    ys = parse(y, name="y")
    if xs is None:
        raise ValueError(f"missing x coordinate (y={y!r})")
    if len(xs) == 1:
        if ys is None:
            raise ValueError(f"missing y coordinate (x={x!r})")
        if len(ys) == 1:
            return xs[0], ys[0]
        raise ValueError(f"ambiguous y coordinate: x={x!r}, y={y!r}")
    if len(xs) == 2:
        # The whole point arrived in x. Accept a truly absent y, or a SINGLE
        # y that merely repeats one of the pair's numbers (the observed
        # duplication shape). A multi-number or disagreeing y contradicts the
        # pair and must not be silently resolved either way.
        if ys is None or (len(ys) == 1 and ys[0] in xs):
            return xs[0], xs[1]
        raise ValueError(f"ambiguous coordinates: x={x!r} carries a pair but y={y!r} disagrees")
    raise ValueError(f"cannot parse coordinates from x={x!r}, y={y!r}")


_cc = cliclick_point  # the same cliclick point format locally and on a Mac over SSH


def _run(cmd: list[str], *, timeout: int = _TIMEOUT_SEC) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    return int(proc.returncode), proc.stdout or "", proc.stderr or ""


def _platform() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    if system == "windows":
        return "windows"
    return system or "unknown"


def _session_type() -> str:
    """linux display-session type: wayland | x11 | unknown (macos: native)."""
    if _platform() == "macos":
        return "native"
    raw = str(os.environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    if raw in ("wayland", "x11"):
        return raw
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


# Aliases accepted by the `key`/`hold_key` tools, mapped per backend.
_KEY_ALIASES = {
    "pagedown": "page-down", "page_down": "page-down", "pgdn": "page-down",
    "pageup": "page-up", "page_up": "page-up", "pgup": "page-up",
    "down": "arrow-down", "up": "arrow-up", "left": "arrow-left", "right": "arrow-right",
    "arrowdown": "arrow-down", "arrowup": "arrow-up", "arrowleft": "arrow-left", "arrowright": "arrow-right",
    "enter": "return", "escape": "esc", "del": "delete", "backspace": "delete",
    "space": "space", "tab": "tab", "home": "home", "end": "end",
}
_X11_KEY_ALIASES = {
    "page-down": "Page_Down", "page-up": "Page_Up",
    "arrow-down": "Down", "arrow-up": "Up", "arrow-left": "Left", "arrow-right": "Right",
    "return": "Return", "esc": "Escape", "delete": "BackSpace", "fwd-delete": "Delete",
    "space": "space", "tab": "Tab", "home": "Home", "end": "End",
    "cmd": "super", "command": "super", "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt", "shift": "shift",
    "super": "super", "meta": "Meta_L",
    # X11 keysyms are case-sensitive; lowercase fN must map back to FN.
    **{f"f{i}": f"F{i}" for i in range(1, 17)},
}
_MAC_MODS = {
    "cmd": "cmd", "command": "cmd", "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt", "shift": "shift", "fn": "fn",
}
# Linux additionally accepts the super/meta modifier names directly.
_ALL_MODS = {**_MAC_MODS, "super": "super", "meta": "meta"}


class _ComputerUse(PlaceMixin, RemoteBackends):
    _MAX_IMAGE_W = _MAX_IMAGE_W
    _MAX_IMAGE_H = _MAX_IMAGE_H

    def __init__(self, api: Any) -> None:
        self.api = api
        self.state_dir = pathlib.Path(api.get_state_dir())
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # Seams the mixins use; resolved at call time so tests can patch the module names.
    @staticmethod
    def _plat() -> str:
        return _platform()

    @staticmethod
    def _session() -> str:
        return _session_type()

    @staticmethod
    def _which_tool(name: str) -> str:
        return _which(name)

    @staticmethod
    def _helper_python() -> str:
        return _helper_python()

    def _run_cmd(self, cmd: list[str], *, timeout: int = _TIMEOUT_SEC) -> tuple[int, str, str]:
        return _run(cmd, timeout=timeout)

    @staticmethod
    def _json_text(payload: dict[str, Any]) -> str:
        return _json(payload)

    @staticmethod
    def _png_dims(path: pathlib.Path) -> tuple[int, int]:
        return _png_dimensions(path)

    @staticmethod
    def _png_ok(path: pathlib.Path) -> bool:
        return _png_intact(path)

    # ------------------------------------------------------------------
    # connection registry / backend selection
    # ------------------------------------------------------------------

    def _connections_path(self) -> pathlib.Path:
        return self.state_dir / _CONNECTIONS_FILE

    def _active_connection_path(self) -> pathlib.Path:
        return self.state_dir / _ACTIVE_CONNECTION_FILE

    def _read_connections(self) -> dict[str, Any]:
        """Read connection registry; always includes local default."""
        data: dict[str, Any] = {"active": "local", "connections": {"local": {"backend": "local", "enabled": True}}}
        try:
            raw = json.loads(self._connections_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                conns = raw.get("connections")
                if isinstance(conns, dict):
                    data["connections"].update({str(k): v for k, v in conns.items() if isinstance(v, dict)})
                active = str(raw.get("active") or "").strip()
                if active:
                    data["active"] = active
        except Exception:
            pass
        try:
            active_file = self._active_connection_path().read_text(encoding="utf-8").strip()
            if active_file:
                data["active"] = active_file
        except Exception:
            pass
        data["connections"].setdefault("local", {"backend": "local", "enabled": True})
        # Unknown active name is PRESERVED (not reset to local); _active_connection fails it closed.
        return data

    def _atomic_write(self, path: pathlib.Path, text: str) -> None:
        """Write+rename: a crash can't leave a torn registry file (which could route remote→local)."""
        tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def _write_connections(self, data: dict[str, Any]) -> None:
        data.setdefault("connections", {})
        data["connections"].setdefault("local", {"backend": "local", "enabled": True})
        # Registry first, active pointer last: a lost second write still names a live connection.
        self._atomic_write(self._connections_path(), json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        try:
            self._atomic_write(self._active_connection_path(), str(data.get("active") or "local"))
        except Exception:
            pass

    def _active_connection(self) -> tuple[str, dict[str, Any]]:
        data = self._read_connections()
        name = str(data.get("active") or "local")
        conn = dict((data.get("connections") or {}).get(name) or {})
        if name == "local":
            return name, (conn or {"backend": "local", "enabled": True})
        # FAIL CLOSED: any NON-local active connection that is missing from the
        # registry (corrupt connections.json), disabled, or carries an unknown
        # backend is marked disabled — it must NEVER fall back to the local
        # desktop. _is_remote() below still returns True for such a name, so the
        # input tools route into _remote_pyautogui (which refuses on "disabled")
        # rather than silently driving the host.
        backend = str(conn.get("backend") or "").strip().lower()
        if not conn or backend not in _REMOTE_BACKENDS or not conn.get("enabled", True):
            marker = {**conn, "backend": backend or "unknown", "disabled": True}
            if not conn:
                marker["missing"] = True
            return name, marker
        return name, conn

    def _disabled_connection_error(self, name: str, conn: dict[str, Any]) -> str:
        return _json({
            "ok": False, "connection": name, "backend": str(conn.get("backend") or "local"),
            "error": f"active connection {name!r} is unusable (disabled or unknown backend); re-add it via add_connection or switch with use_local/activate_connection",
        })

    def _active_backend_name(self) -> str:
        _name, conn = self._active_connection()
        return str(conn.get("backend") or "local").strip().lower() or "local"

    def _is_remote(self) -> bool:
        # Any non-local ACTIVE name is "remote" for dispatch purposes: usable
        # remotes act on the VM; unusable ones (disabled/missing/unknown) are
        # refused in the remote path — never silently handled locally.
        name, _conn = self._active_connection()
        return name != "local"

    def list_connections(self) -> str:
        data = self._read_connections()
        active = str(data.get("active") or "local")
        safe: dict[str, Any] = {"active": active, "connections": {}}
        for name, conn in (data.get("connections") or {}).items():
            if not isinstance(conn, dict):
                continue
            c = {k: v for k, v in conn.items() if "key" not in str(k).lower() and "secret" not in str(k).lower()}
            c["active"] = name == active
            safe["connections"][name] = c
        return _json({"ok": True, **safe})

    def add_connection(self, *, name: str, backend: str, target: str = "", target_file: str = "",
                       host: str = "", user: str = "", port: int = 22,
                       ssh_alias: str = "", enabled: bool = True, activate: bool = False) -> str:
        """Add/update a connection. Does not accept or store private keys."""
        name = str(name or "").strip()
        backend = str(backend or "").strip().lower()
        if not name or name == "local":
            return _json({"ok": False, "error": "name is required and cannot be 'local'"})
        if backend not in {"osworld_http", "ssh_macos"}:
            return _json({"ok": False, "error": "backend must be one of: osworld_http, ssh_macos"})
        conn: dict[str, Any] = {"backend": backend, "enabled": bool(enabled)}
        if backend == "osworld_http":
            if target:
                conn["target"] = str(target).strip().rstrip("/")
            if target_file:
                conn["target_file"] = str(target_file).strip()
            if not conn.get("target") and not conn.get("target_file"):
                return _json({"ok": False, "error": "osworld_http requires target or target_file"})
        if backend == "ssh_macos":
            if ssh_alias:
                conn["ssh_alias"] = str(ssh_alias).strip()
            else:
                if not host:
                    return _json({"ok": False, "error": "ssh_macos requires host or ssh_alias"})
                conn.update({"host": str(host).strip(), "user": str(user or "").strip(), "port": int(port or 22)})
        data = self._read_connections()
        data.setdefault("connections", {})[name] = conn
        if activate:
            data["active"] = name
        self._write_connections(data)
        return _json({"ok": True, "connection": name, "backend": backend, "active": data.get("active") == name})

    def activate_connection(self, *, name: str) -> str:
        name = str(name or "").strip()
        data = self._read_connections()
        if name not in data.get("connections", {}):
            return _json({"ok": False, "error": f"unknown connection {name!r}"})
        data["active"] = name
        self._write_connections(data)
        return _json({"ok": True, "active": name, "connection": data["connections"][name]})

    def use_local(self) -> str:
        data = self._read_connections()
        data["active"] = "local"
        self._write_connections(data)
        return _json({"ok": True, "active": "local"})

    def test_connection(self, *, name: str = "") -> str:
        if name:
            data = self._read_connections()
            conn = dict((data.get("connections") or {}).get(str(name)) or {})
            if not conn:
                return _json({"ok": False, "error": f"unknown connection {name!r}"})
            conn_name = str(name)
        else:
            conn_name, conn = self._active_connection()
        backend = str(conn.get("backend") or "local").lower()
        if backend == "local":
            return _json({"ok": True, "connection": conn_name, "backend": "local", **self._capabilities()})
        if backend == "osworld_http":
            return self._test_osworld(conn, conn_name)
        if backend == "ssh_macos":
            return self._test_ssh_macos(conn, conn_name)
        return _json({"ok": False, "connection": conn_name, "error": f"unsupported backend {backend!r}"})

    # ------------------------------------------------------------------
    # capabilities / coordinate transform
    # ------------------------------------------------------------------

    def _capabilities(self) -> dict[str, Any]:
        plat = _platform()
        session = _session_type()
        return {
            "platform": plat,
            "session_type": session,
            "screenshot": {
                "screencapture": bool(_which("screencapture")),
                "gnome_screenshot": bool(_which("gnome-screenshot")),
                "scrot": bool(_which("scrot")),
                "grim": bool(_which("grim")),
            },
            "input": {
                "cliclick": bool(_which("cliclick")),
                "xdotool": bool(_which("xdotool")),
                "ydotool": bool(_which("ydotool")),
                "wtype": bool(_which("wtype")),
                "osascript": bool(_which("osascript")),
            },
            "downscale": {
                "sips": bool(_which("sips")),
                "magick": bool(_which("magick") or _which("convert")),
            },
            "notes": [
                "ALL actions are for supervised, low-risk local workflows under human observation.",
                "macOS requires Screen Recording for screenshot and Accessibility for input.",
                "macOS grants are read by prompt-free preflights (see permissions); a dropped input is reported "
                "from the helper's own warning. Nothing here prompts for a grant.",
                "macOS scroll is unsupported (cliclick has no scroll-wheel command); use key paging instead.",
                "macOS middle-click and mouse_down/up for non-left buttons are unsupported via cliclick.",
                "Linux X11 needs xdotool (input) + gnome-screenshot/scrot (capture).",
                "Linux Wayland needs ydotool (input; ydotoold running) or wtype (typing only) "
                "+ grim or gnome-screenshot (capture). xdotool does NOT work on Wayland.",
                "Input coordinates default to the LAST screenshot's image space (auto-remapped); "
                "pass raw=true for native input coordinates.",
                "Windows desktops use the separate windows_computer_use skill.",
            ],
            "permission_state_verified": False,
        }

    def capabilities(self) -> str:
        name, conn = self._active_connection()
        payload = {"ok": True, **self._capabilities()}
        payload["active_connection"] = name
        payload["active_backend"] = str(conn.get("backend") or "local")
        if self._is_remote():
            payload["remote"] = {k: v for k, v in conn.items() if "key" not in k.lower() and "secret" not in k.lower()}
            if conn.get("disabled"):
                return _json(payload)
            backend = str(conn.get("backend") or "")
            payload["frontmost"] = self._remote_front(conn)
            payload["targeting"] = {"app": "supported", "resolved_from": "wmctrl rows in the guest" if backend == "osworld_http"
                                    else "lsappinfo on the remote Mac", "availability": "checked at use",
                                    "note": "the remote machine's grants and tools are not probed here; a missing tool or a dropped "
                                            "input is reported by the call that needed it"}
            return _json(payload)
        plat, session = _platform(), _session_type()
        payload["frontmost"] = self._front_local()
        if plat == "macos":
            perms = self._native("perms", timeout=15)
            if perms.get("ok"):
                payload["permissions"] = {k: perms.get(k, "unknown") for k in ("accessibility", "screen_recording", "post_events", "listen_events")}
                payload["permission_state_verified"] = True
                payload["targeting"] = {"app": "supported" if _which("lsappinfo") else "unsupported",
                                        "note": ("activation by Accessibility, else NSRunningApplication, else System Events; verified by lsappinfo"
                                                 if perms.get("accessibility") == "trusted" else
                                                 "Accessibility untrusted: activation tries NSRunningApplication and System Events; verified by lsappinfo")}
            else:
                payload["permissions"] = {"error": perms.get("error") or "preflight unavailable"}
                payload["targeting"] = {"app": "unavailable", "note": f"the native helper gave no answer: {perms.get('error')}"}
        elif plat == "linux":
            if session == "wayland":
                payload["targeting"] = {"app": "unsupported", "session_type": session, "note": "the compositor exposes no window list or activation"}
            elif _which("wmctrl") and _which("xdotool"):
                payload["targeting"] = {"app": "supported", "session_type": session}
            else:
                missing = [t for t in ("wmctrl", "xdotool") if not _which(t)]
                payload["targeting"] = {"app": "unsupported", "session_type": session,
                                        "note": f"{' and '.join(missing)} missing: a switch could not be made or verified, so targeted inputs are refused"}
        return _json(payload)

    def _save_transform(self, data: dict[str, Any]) -> None:
        try:
            (self.state_dir / _TRANSFORM_FILE).write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _load_transform(self) -> dict[str, Any]:
        try:
            return json.loads((self.state_dir / _TRANSFORM_FILE).read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _map_xy(self, x: int, y: int, *, raw: bool) -> tuple[int, int, dict[str, Any]]:
        """Map screenshot-image coordinates to native input coordinates.

        Returns (input_x, input_y, note). Without a stored transform (no
        screenshot yet, or raw=true) coordinates pass through unchanged.
        """
        if raw:
            return int(x), int(y), {"coordinate_space": "raw"}
        tf = self._load_transform()
        sx = float(tf.get("sx") or 0.0)
        sy = float(tf.get("sy") or 0.0)
        if sx <= 0.0 or sy <= 0.0:
            return int(x), int(y), {"coordinate_space": "raw (no stored screenshot transform)"}
        return (
            int(round(int(x) * sx)) + int(tf.get("ox") or 0),
            int(round(int(y) * sy)) + int(tf.get("oy") or 0),
            {
                "coordinate_space": "screenshot",
                "mapped_from": [int(x), int(y)],
                "transform_ts": tf.get("ts"),
            },
        )

    # ------------------------------------------------------------------
    # screenshot (with WXGA normalization)
    # ------------------------------------------------------------------

    def _downscale(self, src: pathlib.Path, max_w: int, max_h: int) -> tuple[pathlib.Path, int, int]:
        """Downscale PNG to fit max_w x max_h; returns (path, w, h) (src on no-op/failure)."""
        px_w, px_h = _png_dimensions(src)
        if px_w <= 0 or px_h <= 0 or (px_w <= max_w and px_h <= max_h):
            return src, px_w, px_h
        ratio = min(max_w / px_w, max_h / px_h)
        new_w = max(1, int(px_w * ratio))
        new_h = max(1, int(px_h * ratio))
        dest = src.with_name(src.stem + f"-{new_w}x{new_h}.png")
        # PRIMARY: in-process PIL — deterministic and dependency-light so the
        # downscale (and therefore the stored image->input coord_transform) ALWAYS
        # happens. Without this the skill silently no-oped on hosts lacking
        # sips/ImageMagick, returning a full-resolution image with an identity
        # transform while view_image re-downscaled independently — a coordinate-
        # space mismatch that made every click land off-target.
        try:
            from PIL import Image
            resample = getattr(Image, "Resampling", Image).LANCZOS
            with Image.open(src) as im:
                im.convert("RGB").resize((new_w, new_h), resample).save(dest, format="PNG")
            gw, gh = _png_dimensions(dest)
            if dest.exists() and gw > 0 and gh > 0:
                return dest, gw, gh
        except Exception:
            pass
        # FALLBACK: external resizers (macOS sips / ImageMagick) when PIL is absent.
        if _which("sips"):  # macOS built-in
            cmd = ["sips", "-z", str(new_h), str(new_w), str(src), "--out", str(dest)]
        elif _which("magick"):
            cmd = ["magick", str(src), "-resize", f"{new_w}x{new_h}!", str(dest)]
        elif _which("convert"):
            cmd = ["convert", str(src), "-resize", f"{new_w}x{new_h}!", str(dest)]
        else:
            return src, px_w, px_h
        try:
            rc, _, _ = _run(cmd, timeout=15)
        except Exception:
            return src, px_w, px_h
        if rc != 0 or not dest.exists():
            return src, px_w, px_h
        got_w, got_h = _png_dimensions(dest)
        return dest, (got_w or new_w), (got_h or new_h)

    def screenshot(self, *, job_id: str = "manual", max_width: int = _MAX_IMAGE_W,
                   max_height: int = _MAX_IMAGE_H, app: str = "") -> str:
        _conn_name, conn = self._active_connection()
        if conn.get("disabled"):
            return self._disabled_connection_error(_conn_name, conn)
        backend = str(conn.get("backend") or "local").lower()
        if backend in ("osworld_http", "ssh_macos"):
            shot = (self._osworld_screenshot if backend == "osworld_http" else self._ssh_macos_screenshot)(
                conn, max_width=max_width, max_height=max_height)
            if str(app or "").strip():
                parsed = json.loads(shot)
                if parsed.get("ok"):
                    # One screen per remote: app= selects nothing there, and the result says so instead of echoing it.
                    parsed["display"] = {"selected_by": "default", "app_display_unread": f"app= is not applied to {backend} captures"}
                    return _json(parsed)
            return shot
        job_dir = self.api.skill_job_dir(job_id or uuid.uuid4().hex[:8])
        out_dir = pathlib.Path(job_dir) / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"screenshot-{int(time.time())}.png"
        plat = _platform()
        session = _session_type()
        display: dict[str, Any] = {}
        if plat == "macos" and _which("screencapture"):
            display = self._capture_display(app)
            if "error" in display:
                return _json({"ok": False, "error": display["error"]})
            cmd = ["screencapture", "-x", *(["-D", str(display["index"])] if not display.get("main", True) else []), str(out_path)]
        elif plat == "linux" and session == "wayland" and _which("grim"):
            cmd = ["grim", str(out_path)]
        elif plat == "linux" and _which("gnome-screenshot"):
            cmd = ["gnome-screenshot", "-f", str(out_path)]
        elif plat == "linux" and session != "wayland" and _which("scrot"):
            cmd = ["scrot", str(out_path)]
        else:
            return _json({
                "ok": False,
                "error": "no supported screenshot backend found for this session "
                         f"(platform={plat}, session={session})",
                "capabilities": self._capabilities(),
            })
        try:
            rc, stdout, stderr = _run(cmd, timeout=15)
        except Exception as exc:  # noqa: BLE001 - surface host permission errors plainly
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}", "cmd": cmd})
        if rc != 0 or not out_path.exists():
            return _json({"ok": False, "error": stderr.strip() or stdout.strip() or f"exit {rc}", "cmd": cmd})

        px_w, px_h = _png_dimensions(out_path)
        max_w = max(320, min(int(max_width or _MAX_IMAGE_W), 4096))
        max_h = max(240, min(int(max_height or _MAX_IMAGE_H), 4096))
        img_path, img_w, img_h = self._downscale(out_path, max_w, max_h)

        # Input-space size: macOS input consumes LOGICAL points; X11/Wayland
        # input consumes the same pixel space the capture was taken in.
        input_w, input_h = px_w, px_h
        scale_note = ""
        approx = False
        if plat == "macos":
            from_display = bool(display.get("width"))
            log_w, log_h = (int(display["width"]), int(display["height"])) if from_display else _macos_logical_size()
            if log_w > 0 and log_h > 0:
                input_w, input_h = log_w, log_h
                scale = round(px_w / log_w, 4) if log_w else 0.0
                if scale and abs(scale - round(scale)) > 0.02 and abs(scale * 2 - round(scale * 2)) > 0.02:
                    approx = True
                # A real device scale is >=1.0; a sub-1 ratio means the logical
                # bounds span MULTIPLE displays while screencapture grabbed only
                # the main one — the transform is wrong even if the ratio looks
                # "clean" (e.g. two identical Retinas give exactly 0.5).
                if scale and scale < 0.95:
                    approx = True
                if not from_display:
                    # The Finder desktop size is the union of all displays, not the captured one: approximate by provenance.
                    approx = True
                scale_note = (
                    "macOS input uses LOGICAL points; this transform already maps "
                    "image pixels to logical points."
                )
            else:
                # Logical size unavailable (e.g. Automation TCC denied): the
                # pixel-based transform would be 2x off for cliclick's logical
                # points on Retina. Flag it honestly instead of silently lying.
                approx = True
                scale_note = (
                    "WARNING: macOS logical desktop size unavailable — the "
                    "transform maps to PIXELS, but cliclick consumes LOGICAL "
                    "points (2x off on Retina). Grant Automation access or "
                    "prefer ax_tree marks with raw=true."
                )
        result: dict[str, Any] = {
            "ok": True,
            "path": str(img_path),
            "backend": cmd[0],
            "image_width": img_w, "image_height": img_h,
            "capture_width_px": px_w, "capture_height_px": px_h,
            "input_width": input_w, "input_height": input_h,
            "downscaled": img_path != out_path,
            "auto_attach_image": str(img_path),
        }
        if display.get("index"):
            result["display"] = {k: display[k] for k in ("index", "x", "y", "width", "height", "main", "selected_by", "app_window_unread") if k in display}
            if display.get("selected_by") == "app":
                result["app"] = display["app"]
        elif app:
            result["display"] = {"selected_by": "default", "app_display_unread": display.get("app_display_unread")
                                 or (f"app= is not applied to {plat} captures" if plat != "macos" else "no display list from the native helper")}
        if img_path != out_path:
            result["full_resolution_path"] = str(out_path)
        if img_w > 0 and img_h > 0 and input_w > 0 and input_h > 0:
            sx = round(input_w / img_w, 6)
            sy = round(input_h / img_h, 6)
            transform = {
                "sx": sx, "sy": sy,
                "image_w": img_w, "image_h": img_h,
                "input_w": input_w, "input_h": input_h,
                "platform": plat, "session": session,
                "approx": approx, "ts": time.time(),
                "ox": int(display.get("x", 0)), "oy": int(display.get("y", 0)),
            }
            self._save_transform(transform)
            result["coord_transform"] = transform
            result["coordinate_note"] = (
                "Pass coordinates read off THIS image directly to click/move/drag — "
                "they are auto-remapped through coord_transform (image -> input space). "
                + scale_note
                + (" Multi-display scale is approximate; prefer ax_tree marks." if approx else "")
            )
        return _json(result)

    # ------------------------------------------------------------------
    # pointer actions
    # ------------------------------------------------------------------

    def _capture_display(self, app: str = "") -> dict[str, Any]:
        """The display to capture: the main one, or the one holding ``app``'s front window (bounds from CoreGraphics).

        The answer says how the display was chosen (``selected_by``: default, app, or fallback when the app's window
        could not be located) so a main-display capture is never presented as the named app's display."""
        res = self._native("displays", timeout=10)
        displays = res.get("displays") if res.get("ok") else None
        query = str(app or "").strip()
        if not displays:
            return {"app_display_unread": res.get("error") or "no display list"} if query else {}
        chosen = {**next((d for d in displays if d.get("main")), displays[0]), "selected_by": "default"}
        if query:
            found = self._native("resolve", query, timeout=15)
            if not found.get("ok"):
                return {"error": self._target_error(query, found)}
            target = found["candidates"][0]
            tree = self._native("tree", target["pid"], 1, 1500, timeout=8)
            node = (tree.get("nodes") or [{}])[0] if tree.get("ok") else {}
            if "x" in node and "w" in node:
                cx, cy = node["x"] + node["w"] // 2, node["y"] + node["h"] // 2
                for d in displays:
                    if d["x"] <= cx < d["x"] + d["width"] and d["y"] <= cy < d["y"] + d["height"]:
                        return {**d, "selected_by": "app", "app": target}
                chosen["app_window_unread"] = f"the window's centre ({cx},{cy}) lies on no active display"
            else:
                chosen["app_window_unread"] = str(tree.get("error") or tree.get("reason") or "the window's bounds could not be read")
            chosen["selected_by"] = "fallback"
        return chosen

    def _takes_negative_points(self) -> bool:
        """A Mac desktop (local or over SSH) has screen space left of/above the origin; X11, Wayland and an OSWorld guest do not."""
        if self._is_remote():
            _name, conn = self._active_connection()
            return str(conn.get("backend") or "") == "ssh_macos"
        return _platform() == "macos"

    def _outside_desktop(self, *points: tuple[int, int]) -> str:
        """A mapped point left of or above the origin on a desktop that has no such space: the stored screenshot
        transform belongs to another display or backend. Empty when every point is fine."""
        if self._takes_negative_points():
            return ""
        bad = [p for p in points if p[0] < 0 or p[1] < 0]
        if not bad:
            return ""
        return (f"the stored screenshot transform maps this point to {bad[0][0]},{bad[0][1]}, outside this desktop "
                "(a screenshot of another display or backend?): take a screenshot of this desktop first, or pass raw=true "
                "only if these are this desktop's native coordinates")

    def _backend_unavailable(self, action: str) -> str:
        return _json({
            "ok": False,
            "error": f"no supported {action} backend found "
                     f"(platform={_platform()}, session={_session_type()})",
            "capabilities": self._capabilities(),
        })

    def click(self, *, x: int, y: int | None = None, button: str = "left", double: bool = False,
              triple: bool = False, raw: bool = False, app: str = "") -> str:
        plat = _platform()
        button = str(button or "left").strip().lower()
        if button not in ("left", "right", "middle"):
            return _json({"ok": False, "error": f"unknown button {button!r} (use left/right/middle)"})
        try:
            x, y = _coerce_point(x, y)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        if (int(x) < 0 or int(y) < 0) and not self._takes_negative_points():
            return _json({"ok": False, "error": f"negative coordinates not allowed ({x},{y})"})
        ix, iy, note = self._map_xy(x, y, raw=raw)
        if self._outside_desktop((ix, iy)):
            return _json({"ok": False, "error": self._outside_desktop((ix, iy))})
        repeat = 3 if triple else (2 if double else 1)
        if self._is_remote():
            _name, conn = self._active_connection()
            return self._remote_pyautogui(
                conn,
                f"pyautogui.click({ix}, {iy}, clicks={repeat}, interval=0.1, button={button!r})",
                note=note, target=app,
            )
        if plat == "macos" and _which("cliclick"):
            if button == "middle":
                return _json({"ok": False, "error": "middle-click unsupported on macOS (cliclick has no middle button)"})
            if button == "right":
                op = "rc"
            elif triple:
                op = "tc"
            elif double:
                op = "dc"
            else:
                op = "c"
            cmd = ["cliclick", f"{op}:{_cc(ix, iy)}"]
        elif plat == "linux" and _session_type() == "wayland" and _which("ydotool"):
            button_code = {"left": "0xC0", "middle": "0xC2", "right": "0xC1"}[button]
            return self._exec_sequence([
                ["ydotool", "mousemove", "--absolute", "-x", str(ix), "-y", str(iy)],
                ["ydotool", "click", "--repeat", str(repeat), button_code],
            ], extra=note, app=app)
        elif plat == "linux" and _which("xdotool"):
            button_id = {"left": "1", "middle": "2", "right": "3"}[button]
            cmd = ["xdotool", "mousemove", str(ix), str(iy), "click", "--repeat", str(repeat), button_id]
        else:
            return self._backend_unavailable("click")
        return self._exec_input(cmd, extra=note, app=app)

    def move(self, *, x: int, y: int | None = None, raw: bool = False) -> str:
        plat = _platform()
        try:
            x, y = _coerce_point(x, y)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        if (int(x) < 0 or int(y) < 0) and not self._takes_negative_points():
            return _json({"ok": False, "error": f"negative coordinates not allowed ({x},{y})"})
        ix, iy, note = self._map_xy(x, y, raw=raw)
        if self._outside_desktop((ix, iy)):
            return _json({"ok": False, "error": self._outside_desktop((ix, iy))})
        if self._is_remote():
            _name, conn = self._active_connection()
            return self._remote_pyautogui(conn, f"pyautogui.moveTo({ix}, {iy})", note=note)
        if plat == "macos" and _which("cliclick"):
            cmd = ["cliclick", f"m:{_cc(ix, iy)}"]
        elif plat == "linux" and _session_type() == "wayland" and _which("ydotool"):
            cmd = ["ydotool", "mousemove", "--absolute", "-x", str(ix), "-y", str(iy)]
        elif plat == "linux" and _which("xdotool"):
            cmd = ["xdotool", "mousemove", str(ix), str(iy)]
        else:
            return self._backend_unavailable("mouse-move")
        return self._exec_input(cmd, extra=note)

    def left_click_drag(self, *, start_x: Any, start_y: Any = None, end_x: Any = None,
                        end_y: Any = None, raw: bool = False, app: str = "") -> str:
        plat = _platform()
        try:
            start_x, start_y = _coerce_point(start_x, start_y)
            end_x, end_y = _coerce_point(end_x, end_y)
        except ValueError as exc:
            return _json({"ok": False, "error": str(exc)})
        if any(int(v) < 0 for v in (start_x, start_y, end_x, end_y)) and not self._takes_negative_points():
            return _json({"ok": False, "error": "negative coordinates not allowed"})
        sx_, sy_, note = self._map_xy(start_x, start_y, raw=raw)
        ex_, ey_, _ = self._map_xy(end_x, end_y, raw=raw)
        if self._outside_desktop((sx_, sy_), (ex_, ey_)):
            return _json({"ok": False, "error": self._outside_desktop((sx_, sy_), (ex_, ey_))})
        if self._is_remote():
            _name, conn = self._active_connection()
            return self._remote_pyautogui(
                conn,
                f"pyautogui.moveTo({sx_}, {sy_}); pyautogui.dragTo({ex_}, {ey_}, duration=0.5, button='left')",
                note=note, target=app,
            )
        if plat == "macos" and _which("cliclick"):
            cmd = ["cliclick", f"dd:{_cc(sx_, sy_)}", f"dm:{_cc(ex_, ey_)}", f"du:{_cc(ex_, ey_)}"]
        elif plat == "linux" and _session_type() == "wayland" and _which("ydotool"):
            # ydotool encodes press/release in the button byte: 0x40|btn=down,
            # 0x80|btn=up (0xC0|btn = full click). There are no --down/--up flags.
            steps = [
                ["ydotool", "mousemove", "--absolute", "-x", str(sx_), "-y", str(sy_)],
                ["ydotool", "click", "0x40"],
                ["ydotool", "mousemove", "--absolute", "-x", str(ex_), "-y", str(ey_)],
                ["ydotool", "click", "0x80"],
            ]
            return self._exec_sequence(steps, extra=note, app=app)
        elif plat == "linux" and _which("xdotool"):
            cmd = ["xdotool", "mousemove", str(sx_), str(sy_), "mousedown", "1",
                   "mousemove", str(ex_), str(ey_), "mouseup", "1"]
        else:
            return self._backend_unavailable("drag")
        return self._exec_input(cmd, extra=note, app=app)

    def mouse_down(self, *, x: Any = None, y: Any = None, button: str = "left", raw: bool = False, app: str = "") -> str:
        return self._mouse_press(x=x, y=y, button=button, raw=raw, press=True, app=app)

    def mouse_up(self, *, x: Any = None, y: Any = None, button: str = "left", raw: bool = False) -> str:
        return self._mouse_press(x=x, y=y, button=button, raw=raw, press=False)

    def _mouse_press(self, *, x: int, y: int, button: str, raw: bool, press: bool, app: str = "") -> str:
        plat = _platform()
        button = str(button or "left").strip().lower()
        if button not in ("left", "right", "middle"):
            return _json({"ok": False, "error": f"unknown button {button!r} (use left/right/middle)"})
        # Coordinates are OPTIONAL here ("press where the pointer is"). ABSENT is
        # None or the legacy -1 sentinel — both mean the same and are mapped to
        # None BEFORE coercion, so mouse_down(x="663, 500") recovers the pair
        # exactly like click does instead of treating the sentinel as a
        # conflicting y (v6.81.1 review round 5). A provided coordinate goes
        # through the same normalizer as click/move.
        x = None if (isinstance(x, int) and x == -1) else x
        y = None if (isinstance(y, int) and y == -1) else y
        if x is None and y is None:
            has_xy = False
            x = y = -1
        else:
            try:
                x, y = _coerce_point(x, y)
            except ValueError as exc:
                return _json({"ok": False, "error": str(exc)})
            if (int(x) < 0 or int(y) < 0) and not self._takes_negative_points():
                # A provided point the destination has no space for is refused, as click does: never a press at
                # wherever the pointer happens to be.
                return _json({"ok": False, "error": f"negative coordinates not allowed ({x},{y})"})
            has_xy = True
        ix = iy = 0
        note: dict[str, Any] = {}
        if has_xy:
            ix, iy, note = self._map_xy(x, y, raw=raw)
            if self._outside_desktop((ix, iy)):
                return _json({"ok": False, "error": self._outside_desktop((ix, iy))})
        if self._is_remote():
            _name, conn = self._active_connection()
            fn = "mouseDown" if press else "mouseUp"
            code = (f"pyautogui.{fn}(x={ix}, y={iy}, button={button!r})" if has_xy
                    else f"pyautogui.{fn}(button={button!r})")
            return self._remote_pyautogui(conn, code, note=note, target=app)
        if plat == "macos" and _which("cliclick"):
            if button != "left":
                return _json({"ok": False, "error": "mouse_down/up supports only the left button on macOS (cliclick dd/du)"})
            op = "dd" if press else "du"
            target = f"{op}:{_cc(ix, iy)}" if has_xy else f"{op}:."
            cmd = ["cliclick", target]
        elif plat == "linux" and _session_type() == "wayland" and _which("ydotool"):
            # press = 0x40|btn, release = 0x80|btn (left +0, right +1, middle +2).
            offset = {"left": 0, "right": 1, "middle": 2}[button]
            code = (0x40 if press else 0x80) | offset
            steps = []
            if has_xy:
                steps.append(["ydotool", "mousemove", "--absolute", "-x", str(ix), "-y", str(iy)])
            steps.append(["ydotool", "click", f"0x{code:02X}"])
            return self._exec_sequence(steps, extra=note, app=app)
        elif plat == "linux" and _which("xdotool"):
            button_id = {"left": "1", "middle": "2", "right": "3"}[button]
            cmd = ["xdotool"]
            if has_xy:
                cmd += ["mousemove", str(ix), str(iy)]
            cmd += ["mousedown" if press else "mouseup", button_id]
        else:
            return self._backend_unavailable("mouse press")
        return self._exec_input(cmd, extra=note, app=app)

    def cursor_position(self) -> str:
        plat = _platform()
        if self._is_remote():
            _name, conn = self._active_connection()
            return self._remote_pyautogui(conn, "print('pos', pyautogui.position())", input_effect=False)
        if plat == "macos" and _which("cliclick"):
            try:
                rc, out, err = _run(["cliclick", "p"])
            except Exception as exc:  # noqa: BLE001
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            if rc == 0:
                nums = [p for p in out.replace(",", " ").split() if p.lstrip("-").isdigit()]
                if len(nums) >= 2:
                    return _json({"ok": True, "x": int(nums[0]), "y": int(nums[1]),
                                  "coordinate_space": "input (macOS logical points)"})
            return _json({"ok": False, "error": err.strip() or out.strip() or f"exit {rc}"})
        if plat == "linux" and _which("xdotool"):
            try:
                rc, out, err = _run(["xdotool", "getmouselocation"])
            except Exception as exc:  # noqa: BLE001
                return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            if rc == 0:
                fields = dict(
                    part.split(":", 1) for part in out.split() if ":" in part
                )
                try:
                    return _json({"ok": True, "x": int(fields.get("x", "0")), "y": int(fields.get("y", "0")),
                                  "coordinate_space": "input (X11 pixels)"})
                except ValueError:
                    pass
            return _json({"ok": False, "error": err.strip() or out.strip() or f"exit {rc}"})
        return self._backend_unavailable("cursor-position")

    # ------------------------------------------------------------------
    # keyboard actions
    # ------------------------------------------------------------------

    def type_text(self, *, text: str, interval_ms: int = 0, app: str = "", via: str = "") -> str:
        plat = _platform()
        text = str(text or "")
        via = str(via or "").strip().lower()
        if via not in ("", "type", "paste"):
            return _json({"ok": False, "error": f"unknown via {via!r} (use '' / type / paste)"})
        if self._is_remote():
            _name, conn = self._active_connection()
            return self._remote_type_text(conn, text, interval_ms, target=app, via=via)
        if via == "paste":
            # The model's choice for long or non-ASCII text (cliclick types ~70 chars/s and hit the 18 s cap on
            # 1,281 characters): write the clipboard, then paste. The clipboard is replaced, and said so; it is
            # written only after the target was verified in front, so a refusal leaves the clipboard alone.
            if plat == "macos" and _which("cliclick") and _which("pbcopy"):
                def copy() -> str:
                    rc, _out, err = _pipe(["pbcopy"], text)
                    return "" if rc == 0 else f"pbcopy failed: {err.strip() or rc}"
                return self._exec_input(["cliclick", "kd:cmd", "t:v", "ku:cmd"], app=app, before=copy,
                                        extra={"method": "paste", "clipboard_replaced": True, "chars": len(text)})
            return _json({"ok": False, "error": f"via=paste is supported on local and SSH macOS only (platform={plat})"})
        if plat == "macos" and _which("cliclick"):
            cmd = ["cliclick", f"t:{text}"]
        elif plat == "linux" and _session_type() == "wayland" and (_which("ydotool") or _which("wtype")):
            # `--` terminates option parsing so text starting with '-' is typed,
            # not parsed as flags.
            if _which("ydotool"):
                cmd = ["ydotool", "type", "--key-delay", str(max(0, int(interval_ms or 0))), "--", text]
            else:
                cmd = ["wtype", "--", text]
        elif plat == "linux" and _which("xdotool"):
            cmd = ["xdotool", "type", "--delay", str(max(0, int(interval_ms or 0))), "--", text]
        else:
            return self._backend_unavailable("typing")
        # Long text / per-char delay can exceed the 10s input cap; allow up to
        # the registered tool budget.
        return self._exec_input(cmd, timeout=18, app=app)

    def _parse_combo(self, combo: str) -> tuple[list[str], str, str]:
        """(modifiers, base_key, error). Split ONLY on '+' (key names contain '-').

        The base key is alias-normalized when a lowercase alias matches; the
        ORIGINAL token is preserved otherwise (X11 keysyms like ``F5`` or
        ``XF86AudioPlay`` are case-sensitive). The original base is stashed on
        ``self._last_base_raw`` for backend-specific case handling.
        """
        raw_parts = [p.strip() for p in combo.split("+") if p.strip()]
        if not raw_parts:
            return [], "", "keys is required"
        parts = [p.lower() for p in raw_parts]
        mod_tokens = parts[:-1]
        unknown = [t for t in mod_tokens if t not in _ALL_MODS]
        if unknown:
            return [], "", f"unknown key modifier(s): {unknown}"
        self._last_base_raw = raw_parts[-1]
        base = _KEY_ALIASES.get(parts[-1], parts[-1])
        return mod_tokens, base, ""

    def key(self, *, keys: str, app: str = "") -> str:
        combo = str(keys or "").strip()
        if not combo:
            return _json({"ok": False, "error": "keys is required"})
        # WHITESPACE = a SEQUENCE of combos, executed in order ("Left Left",
        # "shift+Right shift+Right"). Splitting only on '+' made both shapes fail,
        # and fail differently: the modified form errored with a nonsense modifier
        # ("unknown key modifier(s): ['right shift']"), while the bare form
        # SILENTLY no-opped — `pyautogui.press('left left')` is simply ignored, so
        # the agent believed a keypress happened that never did. Both were measured
        # in the v6.81.1 OSWorld run; the silent one is the more dangerous.
        steps = combo.split()
        if len(steps) == 1:
            return _json(self._key_step(combo, app=app))
        # The target is brought forward once; before every later step the front is read again and a targeted
        # call stops, sending nothing more, when its application is no longer in front. The aggregate names
        # every step's recipient when they differ: the 27.10 shape (cmd+tab, then an arrow) must not read as
        # "both went to the same app".
        refusal, ctx = self._begin(app)
        if refusal:
            return _json(refusal)
        results: list[dict[str, Any]] = []
        for index, step in enumerate(steps):
            results.append(self._key_step(step, ctx=ctx, recheck=index > 0))
            if not results[-1].get("ok"):
                break
        recipients = [r.get("delivered_to") or r.get("front_at_dispatch") or "unknown"
                      for r in results if r.get("dispatched") is True]  # the dispatched steps, whether or not their front was read
        last = results[-1]
        if last.get("ok"):
            payload: dict[str, Any] = {"ok": True, "sequence": steps, "steps": len(steps), "keys": combo, "dispatched": True}
        else:
            done = len(results) - 1
            dispatched = last.get("dispatched")  # the failing step's helper ran (its outcome may be partial)
            if dispatched is None and not last.get("transport_failed"):
                dispatched = False  # a refusal: nothing ran
            outcome = "failed" if dispatched else ("not sent" if dispatched is False else "outcome unknown, the remote call did not answer")
            payload = {"ok": False, "error": f"step {steps[done]!r} {outcome}: {last.get('error')}",
                       "steps_done": done, "steps_total": len(steps), "keys": combo, "dispatched": dispatched}
            for key in ("reason", "candidates", "front", "cmd", "returncode", "permission", "untyped", "transport_failed"):
                if key in last:
                    payload[key] = last[key]
        # Each step's own receipt mark survives the sequence, a failed step's included; so does the switch that
        # ran before a first step that never did (an invalid combo, a refusal).
        if any(r.get("observed_effect") is True for r in results):
            payload["observed_effect"] = True
            if last.get("activation_done"):
                payload["activation_done"] = True
        elif (ctx.get("target") or {}).get("activated"):
            payload["observed_effect"] = True
            payload["activation_done"] = True
        if not isinstance(ctx.get("delivered_to"), dict) and isinstance(results[0].get("delivered_to"), dict):
            ctx["delivered_to"] = results[0]["delivered_to"]  # a remote probe read it in the first step
        stderr = "\n".join(r.get("helper_stderr") or "" for r in results).strip()
        payload = self._finish(payload, ctx, stderr, after=ctx.get("front_last"))
        if not last.get("ok") and last.get("reason") in ("permission_denied", "untypable_characters") and last.get("note"):
            # A demoted step keeps its own explanation; after completed steps it is said about the next step.
            done = len(results) - 1
            if done:
                at = payload.get("delivered_to")
                where = (at.get("app") or at.get("title")) if isinstance(at, dict) else None
                payload["note"] = (f"{done} of {len(steps)} step(s) completed while {where or 'an unknown application'} was in front; "
                                   f"the next step: {last['note']}; the rest were not sent")
            else:
                payload["note"] = last["note"]
        if len({json.dumps(r, sort_keys=True) if isinstance(r, dict) else "unknown" for r in recipients}) > 1:
            payload["steps_delivered_to"] = recipients
        return _json(payload)

    def _key_step(self, combo: str, *, app: str = "", ctx: dict[str, Any] | None = None, recheck: bool = False) -> dict[str, Any]:
        """One key combo on the active backend: with ``app`` it is targeted by itself; with ``ctx`` it is a later
        step of one call (the switch already happened; ``recheck`` reads the front again first)."""
        plat = _platform()
        mods, base, err = self._parse_combo(combo)
        if err:
            return {"ok": False, "error": err, "dispatched": False, "note": "the input was not sent"}
        if self._is_remote():
            _name, conn = self._active_connection()
            tokens = [_PYAUTOGUI_MODS.get(m, m) for m in mods]
            base_raw = getattr(self, "_last_base_raw", base)
            b = _PYAUTOGUI_BASE_ALIASES.get(base, base_raw if len(base_raw) == 1 else base)
            tokens.append(b)
            code = f"pyautogui.press({tokens[0]!r})" if len(tokens) == 1 else "pyautogui.hotkey(" + ", ".join(repr(t) for t in tokens) + ")"
            return json.loads(self._remote_pyautogui(conn, code, target=app, ctx=ctx, later=recheck))
        if plat == "macos" and _which("cliclick"):
            non_mac = [t for t in mods if t not in _MAC_MODS]
            if non_mac:
                return {"ok": False, "error": f"modifier(s) {non_mac} are not available on macOS", "dispatched": False, "note": "the input was not sent"}
            inner = f"t:{base}" if len(base) == 1 else f"kp:{base}"
            if mods:
                mac_mods = [_MAC_MODS[t] for t in mods]
                cmd = ["cliclick", f"kd:{','.join(mac_mods)}", inner, f"ku:{','.join(mac_mods)}"]
            else:
                cmd = ["cliclick", inner]
        elif plat == "linux" and _session_type() == "wayland":
            # ydotool `key` accepts ONLY raw KEYCODE:STATE pairs (e.g. 28:1
            # 28:0) and treats anything else as a no-op delay with exit 0 —
            # a silent fake success. Be honest instead: key combos are
            # unsupported on Wayland; use type_text (ydotool/wtype) for text.
            return {
                "ok": False,
                "error": (
                    "key combos are unsupported on Wayland (ydotool key takes raw "
                    "keycodes only); use type_text for text input or an X11 session"
                ),
                "capabilities": self._capabilities(),
            }
        elif plat == "linux" and _which("xdotool"):
            # Aliases map to canonical keysyms; unknown multi-char tokens keep
            # their ORIGINAL case (X11 keysyms like XF86AudioPlay, KP_Enter).
            base_raw = getattr(self, "_last_base_raw", base)
            x_base = _X11_KEY_ALIASES.get(base, base if len(base) == 1 else base_raw)
            x_mods = [_X11_KEY_ALIASES.get(m, m) for m in mods]
            x_combo = "+".join(x_mods + [x_base])
            cmd = ["xdotool", "key", x_combo]
        else:
            return json.loads(self._backend_unavailable("key"))
        return self._exec_input_dict(cmd, app=app, ctx=ctx, recheck=recheck, extra={"keys": combo})

    def hold_key(self, *, keys: str, duration_ms: int = 500, app: str = "") -> str:
        combo = str(keys or "").strip()
        if not combo:
            return _json({"ok": False, "error": "keys is required"})
        duration = max(50, min(10_000, int(duration_ms or 500)))
        plat = _platform()
        tokens = [p.strip() for p in combo.split("+") if p.strip()]
        if not tokens:
            return _json({"ok": False, "error": "keys is required"})
        if self._is_remote():
            _name, conn = self._active_connection()
            pya = [_PYAUTOGUI_MODS.get(t.lower(), (t if len(t) == 1 else t.lower())) for t in tokens]
            downs = "; ".join(f"pyautogui.keyDown({t!r})" for t in pya)
            ups = "; ".join(f"pyautogui.keyUp({t!r})" for t in reversed(pya))
            return self._remote_pyautogui(conn, f"{downs}; time.sleep({duration/1000.0!r}); {ups}", timeout=max(12, duration // 1000 + 8), target=app)
        if plat == "macos" and _which("cliclick"):
            # cliclick `kd:`/`ku:` accept ONLY modifiers (cmd/ctrl/alt/shift/fn);
            # `kp:` is press-AND-RELEASE — it cannot hold. Be honest: only
            # pure-modifier combos are holdable on macOS. `w:` is WAIT in ms.
            lowered = [t.lower() for t in tokens]
            non_mods = [t for t, low in zip(tokens, lowered) if low not in _MAC_MODS]
            if non_mods:
                return _json({"dispatched": False, "note": "the input was not sent",
                    "ok": False,
                    "error": (
                        f"cliclick cannot hold non-modifier key(s) {non_mods}; "
                        "only modifier combos (cmd/ctrl/alt/shift/fn) are holdable on macOS"
                    ),
                })
            held = ",".join(_MAC_MODS[low] for low in lowered)
            cmd = ["cliclick", f"kd:{held}", f"w:{duration}", f"ku:{held}"]
            return self._exec_input(cmd, timeout=max(12, duration // 1000 + 4), app=app)
        if plat == "linux" and _session_type() == "wayland":
            # ydotool's key syntax is raw KEYCODE:STATE pairs and silently
            # ignores anything else; no honest hold contract — unsupported.
            return _json({"ok": False, "error": "hold_key is unsupported on Wayland (ydotool has no reliable keydown/keyup names)"})
        if plat == "linux" and _which("xdotool"):
            mods, base, err = self._parse_combo(combo)
            if err:
                return _json({"ok": False, "error": err, "dispatched": False, "note": "the input was not sent"})
            # Same case-preservation as key(): unknown multi-char tokens keep
            # their ORIGINAL case (X11 keysyms are case-sensitive).
            base_raw = getattr(self, "_last_base_raw", base)
            x_base = _X11_KEY_ALIASES.get(base, base if len(base) == 1 else base_raw)
            x_mods = [_X11_KEY_ALIASES.get(m, m) for m in mods]
            x_combo = "+".join(x_mods + [x_base]) if (x_mods or x_base) else x_base
            steps = [["xdotool", "keydown", x_combo]]
            return self._hold_then_release(steps, [["xdotool", "keyup", x_combo]], duration, app=app)
        return self._backend_unavailable("hold-key")

    def _hold_then_release(self, down_steps: list[list[str]], up_steps: list[list[str]],
                           duration_ms: int, app: str = "") -> str:
        refusal, ctx = self._begin(app)
        if refusal:
            return _json(refusal)
        total = len(down_steps) + len(up_steps)
        for done, cmd in enumerate(down_steps):
            rc, out, err = self._try(cmd)
            if rc != 0:
                return self._step_failure(cmd, rc, out, err, done, total, ctx)
        time.sleep(min(10.0, duration_ms / 1000.0))
        for done, cmd in enumerate(up_steps, len(down_steps)):
            rc, out, err = self._try(cmd)
            if rc != 0:
                return self._step_failure(cmd, rc, out, err, done, total, ctx)
        payload: dict[str, Any] = {"ok": True, "held_ms": duration_ms, **({"observed_effect": True, "dispatched": True} if total else {})}
        return _json(self._finish(payload, ctx) if total else payload)

    def wait(self, *, ms: int = 500) -> str:
        delay = max(0, min(10_000, int(ms or 0)))
        time.sleep(delay / 1000.0)
        return _json({"ok": True, "waited_ms": delay})

    # ------------------------------------------------------------------
    # scroll / windows / accessibility
    # ------------------------------------------------------------------

    def scroll(self, *, clicks: int = 3, direction: str = "down", app: str = "") -> str:
        direction = str(direction or "down").lower()
        amount = max(1, min(20, abs(int(clicks or 1))))
        plat = _platform()
        if self._is_remote():
            _name, conn = self._active_connection()
            # X11 pyautogui = one wheel detent per unit (like the local path): 1:1, no multiplier.
            if direction in ("up", "down"):
                delta = amount if direction == "up" else -amount
                return self._remote_pyautogui(conn, f"pyautogui.scroll({delta})", target=app)
            if direction in ("left", "right"):
                delta = amount if direction == "right" else -amount
                return self._remote_pyautogui(conn, f"pyautogui.hscroll({delta})", target=app)
            return _json({"ok": False, "error": f"unknown scroll direction {direction!r} (use up/down/left/right)"})
        if plat == "macos":
            # cliclick has NO scroll-wheel command — its `w:` is WAIT. Faking it
            # returned ok:true while nothing scrolled. Be honest: page via the
            # `key` tool (page-down / arrow keys) on macOS instead.
            return _json({
                "ok": False,
                "error": "scroll is unsupported on macOS (no cliclick scroll-wheel command); "
                         "use the key tool with page-down/page-up or arrow keys instead",
                "capabilities": self._capabilities(),
            })
        if plat == "linux" and _session_type() == "wayland" and _which("ydotool"):
            # ydotool wheel: positive y = up, negative = down (REL_WHEEL units).
            delta = amount if direction == "up" else -amount
            if direction not in ("up", "down"):
                return _json({"ok": False, "error": f"unsupported scroll direction {direction!r} on Wayland (use up/down)"})
            cmd = ["ydotool", "mousemove", "--wheel", "-x", "0", "-y", str(delta)]
            return self._exec_input(cmd, app=app)
        if plat == "linux" and _which("xdotool"):
            button = {"down": "5", "up": "4", "left": "6", "right": "7"}.get(direction)
            if button is None:
                return _json({"ok": False, "error": f"unknown scroll direction {direction!r} (use up/down/left/right)"})
            cmd = ["xdotool", "click", "--repeat", str(amount), button]
            return self._exec_input(cmd, app=app)
        return self._backend_unavailable("scroll")

    # ------------------------------------------------------------------
    # exec helpers
    # ------------------------------------------------------------------

    def _try(self, cmd: list[str], *, timeout: int = _TIMEOUT_SEC) -> tuple[int, str, str]:
        """(exit code, stdout, stderr); ``_NOT_LAUNCHED`` when the OS could not start the helper (it never ran)."""
        try:
            return _run(cmd, timeout=timeout)
        except OSError as exc:  # the process was never created (binary missing, not executable, exec failure)
            return _NOT_LAUNCHED, "", f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - it ran and did not finish well (a timeout): outcome unknown
            return 1, "", f"{type(exc).__name__}: {exc}"

    def _exec_input(self, cmd: list[str], *, timeout: int = _TIMEOUT_SEC, extra: dict[str, Any] | None = None,
                    app: str = "", before: Any = None) -> str:
        return _json(self._exec_input_dict(cmd, timeout=timeout, extra=extra, app=app, before=before))

    def _exec_input_dict(self, cmd: list[str], *, timeout: int = _TIMEOUT_SEC, extra: dict[str, Any] | None = None,
                         app: str = "", ctx: dict[str, Any] | None = None, recheck: bool = False,
                         before: Any = None) -> dict[str, Any]:
        """One input process. Its successful exit is the receipt (``observed_effect``); a failed or timed-out
        process has an unknown outcome and carries none. With ``app`` the target is brought forward and verified
        first; nothing is sent when that fails. With ``ctx`` (a later step of one call) the earlier switch is
        reused and ``recheck`` reads the front again first. ``before`` runs after the target is verified and
        before the input (the clipboard write of via=paste); its error message cancels the input."""
        if ctx is None:
            refusal, ctx = self._begin(app)
            if refusal:
                return refusal
        step = ctx
        if recheck:
            refusal, step = self._recheck(ctx)
            if refusal:
                return refusal
        activated = bool((ctx.get("target") or {}).get("activated"))
        if before is not None:
            try:
                problem = before()
            except Exception as exc:  # noqa: BLE001 - a failed preparation cancels the input, with a receipt
                problem = f"{type(exc).__name__}: {exc}"
            if problem:
                payload: dict[str, Any] = {"ok": False, "error": problem, "cmd": cmd, "dispatched": False,
                                           "reason": "clipboard_failed" if (extra or {}).get("method") == "paste" else "preparation_failed"}
                if activated:
                    payload["observed_effect"] = True  # the switch ran before the input was cancelled
                    payload["activation_done"] = True
                return self._finish(payload, step)
        rc, stdout, stderr = self._try(cmd, timeout=timeout)
        if rc != 0:
            payload = {"ok": False, "error": stderr.strip() or stdout.strip() or f"exit {rc}", "cmd": cmd,
                       "dispatched": rc != _NOT_LAUNCHED}
            if rc != _NOT_LAUNCHED:
                payload["returncode"] = rc
            else:
                payload["reason"] = "helper_not_launched"
            if activated:
                payload["observed_effect"] = True  # the switch ran before the input failed
                payload["activation_done"] = True
            if before is not None and extra:
                # The preparation (the clipboard write) completed; only the helper after it failed. A replaced
                # clipboard is an observed external effect of its own.
                payload.update({k: extra[k] for k in ("method", "clipboard_replaced", "chars") if k in extra})
                if extra.get("clipboard_replaced"):
                    payload["observed_effect"] = True
                    payload["clipboard_done"] = True
        else:
            payload = {"ok": True, "cmd": cmd, "observed_effect": True, "dispatched": True}
            if extra:
                payload.update(extra)
        payload = self._finish(payload, step, stderr)
        ctx["front_last"] = step.get("front_last")  # the call's last front read, for a sequence's aggregate
        return payload

    def _exec_sequence(self, steps: list[list[str]], *, extra: dict[str, Any] | None = None, app: str = "") -> str:
        refusal, ctx = self._begin(app)
        if refusal:
            return _json(refusal)
        for done, cmd in enumerate(steps):
            rc, stdout, stderr = self._try(cmd)
            if rc != 0:
                return self._step_failure(cmd, rc, stdout, stderr, done, len(steps), ctx)
        payload: dict[str, Any] = {"ok": True, "steps": len(steps)}
        if steps:
            payload["observed_effect"] = True
            payload["dispatched"] = True
        if extra:
            payload.update(extra)
        return _json(self._finish(payload, ctx) if steps else payload)

    def _step_failure(self, cmd: list[str], rc: int, stdout: str, stderr: str, done: int, total: int,
                      ctx: dict[str, Any] | None = None) -> str:
        """A failed step of a multi-process input: the call failed, and the ``done`` steps
        before it ran and reported success, so their receipt mark stays."""
        payload: dict[str, Any] = {"ok": False, "error": stderr.strip() or stdout.strip() or f"exit {rc}",
                                   "cmd": cmd, "steps_done": done, "steps_total": total, "dispatched": rc != _NOT_LAUNCHED}
        if rc != _NOT_LAUNCHED:
            payload["returncode"] = rc
        else:
            payload["reason"] = "helper_not_launched"
        if done or ((ctx or {}).get("target") or {}).get("activated"):
            payload["observed_effect"] = True
            if not done:
                payload["activation_done"] = True
        return _json(self._finish(payload, ctx) if ctx else payload)


def register(api: Any) -> None:
    impl = _ComputerUse(api)
    _xy = {"x": {"type": "integer"}, "y": {"type": "integer"}, "raw": {"type": "boolean", "default": False}}
    # `app`: a running application (name, bundle id / WM_CLASS, or pid:<n> from window_list). Empty = untargeted,
    # which is also what a voice model sends for an optional field it does not use.
    _app = {"app": {"type": "string", "default": ""}}
    _targeted = ("app= acts in that application (brought forward and verified in front first; nothing is sent otherwise). "
                 "The result names who was in front at dispatch, not that the app reacted.")
    api.register_tool("list_connections", lambda: impl.list_connections(), description="List configured local/remote computer-use connections and the active backend.", schema={"type": "object", "properties": {}}, timeout_sec=5)
    api.register_tool("add_connection", impl.add_connection, description="Add or update a remote computer-use connection: backend osworld_http (target or target_file) or ssh_macos (ssh_alias or host). No secrets are stored; SSH uses your existing ssh config/agent; activate=true also selects it.", schema={"type": "object", "properties": {"name": {"type": "string"}, "backend": {"type": "string"}, "target": {"type": "string", "default": ""}, "target_file": {"type": "string", "default": ""}, "host": {"type": "string", "default": ""}, "user": {"type": "string", "default": ""}, "port": {"type": "integer", "default": 22}, "ssh_alias": {"type": "string", "default": ""}, "enabled": {"type": "boolean", "default": True}, "activate": {"type": "boolean", "default": False}}, "required": ["name", "backend"]}, timeout_sec=10)
    api.register_tool("test_connection", impl.test_connection, description="Run a safe non-mutating health check for a configured connection (default: the active one).", schema={"type": "object", "properties": {"name": {"type": "string", "default": ""}}}, timeout_sec=30)
    api.register_tool("activate_connection", impl.activate_connection, description="Select a configured connection as the active target for screenshot/input tools.", schema={"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, timeout_sec=5)
    api.register_tool("use_local", lambda: impl.use_local(), description="Return computer-use tools to the local desktop backend.", schema={"type": "object", "properties": {}}, timeout_sec=5)
    api.register_tool("capabilities", lambda: impl.capabilities(), description="Who is in front, which OS grants this process holds (prompt-free preflights on macOS), whether app= targeting works here, and the installed backends.", schema={"type": "object", "properties": {}}, timeout_sec=45)
    api.register_tool("screenshot", impl.screenshot, description="Capture the main display (or, with app=, the display holding that app's window on macOS; display.selected_by says which was captured), downscale to <=WXGA and persist the image->input transform. The image is attached automatically; an engine without vision gets its path.", schema={"type": "object", "properties": {"job_id": {"type": "string", "default": "manual"}, "max_width": {"type": "integer", "default": _MAX_IMAGE_W}, "max_height": {"type": "integer", "default": _MAX_IMAGE_H}, **_app}}, timeout_sec=90)
    # y is deliberately NOT in `required` for click/move: models recurringly emit the
    # whole point inside x ("663, 500") with y omitted, and a required-field rejection
    # then fires BEFORE the handler's _coerce_point can recover the pair.
    api.register_tool("click", impl.click, description="Click at screenshot-space coordinates (auto-remapped; raw=true for native input space). " + _targeted, schema={"type": "object", "properties": {**_xy, "button": {"type": "string", "default": "left"}, "double": {"type": "boolean", "default": False}, "triple": {"type": "boolean", "default": False}, **_app}, "required": ["x"]}, timeout_sec=90)
    # Models call these names unprompted (111 such calls in one OSWorld run, all
    # previously "Unknown tool"): register them as thin aliases of click.
    api.register_tool("double_click", lambda **kw: impl.click(**{**kw, "double": True}), description="Alias of click with double=true (app= as in click).", schema={"type": "object", "properties": {**_xy, "button": {"type": "string", "default": "left"}, **_app}, "required": ["x"]}, timeout_sec=90)
    api.register_tool("triple_click", lambda **kw: impl.click(**{**kw, "triple": True}), description="Alias of click with triple=true (app= as in click).", schema={"type": "object", "properties": {**_xy, "button": {"type": "string", "default": "left"}, **_app}, "required": ["x"]}, timeout_sec=90)
    api.register_tool("move", impl.move, description="Move the mouse pointer to screenshot-space coordinates (auto-remapped; raw=true for native input space).", schema={"type": "object", "properties": _xy, "required": ["x"]}, timeout_sec=60)
    # start_y/end_y are recoverable from a pair packed into start_x/end_x, so only
    # the x-side fields stay required (same rationale as click/move above).
    api.register_tool("left_click_drag", impl.left_click_drag, description="Drag with the left button from start to end (screenshot-space, auto-remapped; raw=true for native). " + _targeted, schema={"type": "object", "properties": {"start_x": {"type": "integer"}, "start_y": {"type": "integer"}, "end_x": {"type": "integer"}, "end_y": {"type": "integer"}, "raw": {"type": "boolean", "default": False}, **_app}, "required": ["start_x", "end_x"]}, timeout_sec=90)
    api.register_tool("mouse_down", impl.mouse_down, description="Press and hold a mouse button (left only on macOS) at the pointer or at screenshot-space coordinates. " + _targeted, schema={"type": "object", "properties": {**_xy, "button": {"type": "string", "default": "left"}, **_app}}, timeout_sec=90)
    api.register_tool("mouse_up", impl.mouse_up, description="Release a held mouse button at the pointer, or at screenshot-space coordinates (auto-remapped; raw=true for native input space). Never changes which app is in front.", schema={"type": "object", "properties": {**_xy, "button": {"type": "string", "default": "left"}}}, timeout_sec=60)
    api.register_tool("cursor_position", lambda: impl.cursor_position(), description="Report the current pointer position in native input coordinates.", schema={"type": "object", "properties": {}}, timeout_sec=10)
    api.register_tool("type_text", impl.type_text, description="Type text into the focused field of the frontmost app, or of app= (brought forward and verified first). via=paste writes the clipboard and pastes (long or non-ASCII text on a local or SSH Mac; the clipboard is replaced); an OSWorld guest pastes such text through its clipboard by itself.", schema={"type": "object", "properties": {"text": {"type": "string"}, "interval_ms": {"type": "integer", "default": 0}, **_app, "via": {"type": "string", "default": ""}}, "required": ["text"]}, timeout_sec=180)
    api.register_tool("key", impl.key, description="Press keys in the front app or app= (verified; else nothing sent). Result: who was in front, not that the app reacted. Combos with '+', sequences with spaces (cmd+s, ctrl+l, page-down, 'down down return'); a targeted sequence stops when its app leaves the front. Scriptable apps are better driven by their own commands through run_command.", schema={"type": "object", "properties": {"keys": {"type": "string"}, **_app}, "required": ["keys"]}, timeout_sec=90)
    api.register_tool("hold_key", impl.hold_key, description="Hold a key or modifier combo for a duration (50-10000 ms). " + _targeted, schema={"type": "object", "properties": {"keys": {"type": "string"}, "duration_ms": {"type": "integer", "default": 500}, **_app}, "required": ["keys"]}, timeout_sec=90)
    api.register_tool("scroll", impl.scroll, description="Scroll 1-20 wheel steps; app= acts in that app (verified first, else nothing is sent). X11/OSWorld: up/down/left/right; Wayland: up/down; macOS: unsupported, page with key. The result names who was in front at dispatch.", schema={"type": "object", "properties": {"clicks": {"type": "integer", "default": 3}, "direction": {"type": "string", "default": "down"}, **_app}}, timeout_sec=90)
    api.register_tool("wait", impl.wait, description="Sleep up to 10s so the UI can settle before the next observation.", schema={"type": "object", "properties": {"ms": {"type": "integer", "default": 500}}}, timeout_sec=12)
    api.register_tool("activate_app", impl.activate_app, description="Bring a running application to the front (name, bundle id / WM_CLASS, or pid:<n> from window_list; resolved from the running list, never launched) and report who is in front afterwards. Inputs with app= do this themselves; use this to switch before several inputs or to check that a switch works.", schema={"type": "object", "properties": {"app": {"type": "string"}, "wait_ms": {"type": "integer", "default": 2000}}, "required": ["app"]}, timeout_sec=90)
    api.register_tool("window_list", impl.window_list, description="Running applications with their windows: who is in front, titles, bounds, pid, and on macOS whether the app has its own scripting commands (scriptable). app= filters; limit caps the rows (truncated says so).", schema={"type": "object", "properties": {**_app, "limit": {"type": "integer", "default": 50}}}, timeout_sec=60)
    api.register_tool("ax_tree", impl.ax_tree, description="Read one window as text through the Accessibility API (macOS), bounded: the frontmost app's window or app='s (without bringing it forward). Nodes with role/label/value/bounds plus numbered marks (positions to click with raw=true; a mark is not proof the element reacts); truncated_by names every cut. An OSWorld remote returns the guest's whole tree; Linux reports unsupported with the window rows, SSH Macs unsupported.", schema={"type": "object", "properties": {**_app, "max_nodes": {"type": "integer", "default": 150}, "timeout_ms": {"type": "integer", "default": 3000}, "root": {"type": "string", "default": "window"}, "include_web": {"type": "boolean", "default": False}}}, timeout_sec=45)
    api.register_tool("remote_exec", impl.remote_exec, description="Run a shell command on the active remote connection only; refuses on local backend. OSWorld/Linux guest: each call is a FRESH `bash -lc` starting in $HOME. SSH Mac: each call runs through the remote account's login shell via ssh. Either way this is NOT the visible desktop terminal: it does not inherit that terminal's working directory and leaves nothing in its shell history.", schema={"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 60}}, "required": ["command"]}, timeout_sec=120)
