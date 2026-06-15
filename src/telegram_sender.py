"""Telegram bot sender (sends video / photo / text to a chat)."""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Optional

import httpx


class TelegramClient:
    """Minimal Bot API client: send text, photo, video, document."""

    def __init__(self, bot_token: str, chat_id: str, timeout_s: float = 120.0):
        if not bot_token or not chat_id:
            raise RuntimeError("Telegram bot_token and chat_id are required")
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = chat_id
        self.timeout_s = timeout_s

    def _post(self, method: str, **fields) -> dict:
        url = f"{self.base}/{method}"
        with httpx.Client(timeout=self.timeout_s) as client:
            r = client.post(url, data={k: v for k, v in fields.items() if v is not None})
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram {method} failed {r.status_code}: {r.text[:300]}")
        return r.json()

    def send_text(self, text: str, parse_mode: Optional[str] = "Markdown") -> dict:
        return self._post("sendMessage", chat_id=self.chat_id, text=text, parse_mode=parse_mode)

    def send_file(self, file_path: Path, caption: Optional[str] = None) -> dict:
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(file_path)
        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or "application/octet-stream"

        if mime.startswith("image/"):
            method = "sendPhoto"
            field = "photo"
        elif mime.startswith("video/"):
            method = "sendVideo"
            field = "video"
        else:
            method = "sendDocument"
            field = "document"

        url = f"{self.base}/{method}"
        with httpx.Client(timeout=self.timeout_s) as client:
            with open(file_path, "rb") as f:
                files = {field: (file_path.name, f, mime)}
                data = {"chat_id": self.chat_id}
                if caption:
                    data["caption"] = caption[:1024]
                r = client.post(url, data=data, files=files)
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram {method} failed {r.status_code}: {r.text[:300]}")
        return r.json()
