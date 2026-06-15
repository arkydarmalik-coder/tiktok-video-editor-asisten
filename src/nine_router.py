"""9router OpenAI-compatible chat client (httpx, no SDK dep)."""
from __future__ import annotations

from typing import Iterable, List, Dict, Optional

import httpx


SYSTEM_PROMPT = (
    "You are the TikTok Video Editor Assistant. You help users plan vertical "
    "(1080x1920) short-form videos, pick transition styles, write hook scripts, "
    "and suggest caption + hashtag combos. Be concise (max 80 words per reply) "
    "and propose one concrete next action."
)


class NineRouterClient:
    def __init__(self, base_url: str, api_key: str, model: str = "utama", timeout_s: float = 60.0):
        if not api_key:
            raise RuntimeError("NINEROUTER_API_KEY is empty; set it in HF Space secrets.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=self.timeout_s) as client:
            r = client.post(url, json=payload, headers=self._headers())
        if r.status_code >= 400:
            raise RuntimeError(f"9router {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Malformed 9router response: {data}") from e

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
    ) -> Iterable[str]:
        """Server-Sent Events style streaming; yields text chunks."""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
            "temperature": temperature,
            "stream": True,
        }
        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", url, json=payload, headers=self._headers()) as r:
                if r.status_code >= 400:
                    raise RuntimeError(f"9router {r.status_code}: {r.read().decode()[:300]}")
                buf = ""
                for chunk in r.iter_text():
                    buf += chunk
                    for line in buf.split("\n"):
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            import json
                            obj = json.loads(data)
                            delta = obj["choices"][0]["delta"].get("content") or ""
                            if delta:
                                yield delta
                        except Exception:
                            continue
                    buf = ""
