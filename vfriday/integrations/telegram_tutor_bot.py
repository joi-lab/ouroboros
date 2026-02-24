"""Separate Telegram Tutor Bot gateway for Viktor-Friday."""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests

from vfriday.schemas import TriggerType
from vfriday.settings import load_settings
from vfriday.storage import Storage


class TutorTelegramBot:
    """Minimal long-polling Telegram bot that proxies to Orchestrator API."""

    def __init__(self, token: str, orchestrator_url: str, storage: Storage):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.orchestrator_url = orchestrator_url.rstrip("/")
        self.storage = storage

    def get_updates(self, offset: int, timeout: int = 20) -> list[dict]:
        r = requests.get(
            f"{self.base}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + 5,
        )
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            return []
        return body.get("result") or []

    def send(self, chat_id: int, text: str) -> None:
        requests.post(
            f"{self.base}/sendMessage",
            data={"chat_id": int(chat_id), "text": text[:3900], "disable_web_page_preview": True},
            timeout=30,
        )

    def _extract_image_base64(self, msg: Dict[str, Any]) -> Tuple[str, str]:
        file_id = ""
        mime = "image/png"
        if msg.get("photo"):
            photo = msg["photo"][-1]
            file_id = str(photo.get("file_id") or "")
            mime = "image/jpeg"
        elif msg.get("document"):
            doc = msg["document"]
            if str(doc.get("mime_type") or "").startswith("image/"):
                file_id = str(doc.get("file_id") or "")
                mime = str(doc.get("mime_type") or mime)
        if not file_id:
            return "", mime

        r = requests.get(f"{self.base}/getFile", params={"file_id": file_id}, timeout=10)
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            return "", mime
        file_path = str(body.get("result", {}).get("file_path") or "")
        if not file_path:
            return "", mime
        r2 = requests.get(f"{self.file_base}/{file_path}", timeout=30)
        r2.raise_for_status()
        return base64.b64encode(r2.content).decode("ascii"), mime

    def _api(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.orchestrator_url}{path}"
        if method == "GET":
            r = requests.get(url, timeout=40)
        else:
            r = requests.post(url, json=payload or {}, timeout=90)
        r.raise_for_status()
        return r.json()

    def _create_session(self, chat_id: int, user_info: Dict[str, Any]) -> str:
        alias = (
            str(user_info.get("first_name") or "").strip()
            or str(user_info.get("username") or "").strip()
            or f"chat-{chat_id}"
        )
        created = self._api(
            "POST",
            "/v1/sessions",
            {
                "student_alias": alias,
                "topic": None,
                "grade_level": None,
                "goal": "Math/Physics tutoring",
            },
        )
        session_id = str(created.get("session_id"))
        self.storage.bind_chat_session(chat_id, session_id)
        return session_id

    def _ensure_session(self, chat_id: int, user_info: Dict[str, Any]) -> str:
        existing = self.storage.get_chat_session(chat_id)
        if existing:
            return existing
        return self._create_session(chat_id, user_info)

    def handle_message(self, msg: Dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        user = msg.get("from") or {}
        chat_id = int(chat.get("id") or 0)
        text = str(msg.get("text") or "")
        caption = str(msg.get("caption") or "")
        if not chat_id:
            return

        low = text.strip().lower()
        if low.startswith("/new_session"):
            session_id = self._create_session(chat_id, user)
            self.send(chat_id, f"Session ready: `{session_id}`\nSend a problem text or image.")
            return

        if low.startswith("/state"):
            sid = self.storage.get_chat_session(chat_id)
            if not sid:
                self.send(chat_id, "No active session. Send /new_session first.")
                return
            state = self._api("GET", f"/v1/sessions/{sid}/state")
            setpoints = state.get("setpoints", {})
            stress = state.get("stress", {})
            self.send(
                chat_id,
                "State\n"
                f"- stress_ai: {stress.get('stress_ai', 0):.3f}\n"
                f"- stress_viktor: {stress.get('stress_viktor', 0):.3f}\n"
                f"- competency: {setpoints.get('competency', 0):.3f}\n"
                f"- transfer: {setpoints.get('transfer', 0):.3f}\n"
                f"- horizon: {setpoints.get('horizon', 0):.3f}",
            )
            return

        sid = self._ensure_session(chat_id, user)
        trigger = TriggerType.MANUAL_UPLOAD.value
        user_message = text or caption
        if low.startswith("/help"):
            trigger = TriggerType.HELP_REQUEST.value
            user_message = text[len("/help"):].strip() or caption or "I need help with this step."

        image_b64, _mime = self._extract_image_base64(msg)
        payload = {
            "trigger_type": trigger,
            "problem_text": None,
            "image_base64": image_b64 or None,
            "ocr_text": None,
            "latex_text": None,
            "idle_seconds": None,
            "user_message": user_message or None,
        }
        result = self._api("POST", f"/v1/sessions/{sid}/ingest", payload)
        reply = str(result.get("tutor_message") or "(empty tutor response)")
        if result.get("status") == "uncertain":
            reply = "I may be uncertain here. Let's validate carefully.\n\n" + reply
        self.send(chat_id, reply)


def run_polling() -> None:
    settings = load_settings()
    token = settings.telegram_bot_token or os.environ.get("VFRIDAY_TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("VFRIDAY_TELEGRAM_BOT_TOKEN is required for tutor bot.")
    storage = Storage(settings.db_path, settings.audit_jsonl_path)
    bot = TutorTelegramBot(token=token, orchestrator_url=settings.orchestrator_url, storage=storage)
    offset = 0
    print("Tutor Telegram bot started.")
    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout=20)
            for upd in updates:
                offset = int(upd["update_id"]) + 1
                msg = upd.get("message") or {}
                if msg:
                    bot.handle_message(msg)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[tutor-bot] error: {exc}")
            time.sleep(1.5)


if __name__ == "__main__":
    run_polling()
