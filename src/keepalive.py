"""Cloudflare Worker keep-alive ping."""
from __future__ import annotations

import logging

import httpx


def ping(url: str, timeout_s: float = 10.0) -> bool:
    """Best-effort ping. Returns True on 2xx, False otherwise. Never raises."""
    if not url:
        return False
    try:
        with httpx.Client(timeout=timeout_s) as client:
            r = client.get(url)
        ok = 200 <= r.status_code < 300
        logging.info("Cloudflare ping %s -> %s", url, r.status_code)
        return ok
    except Exception as e:
        logging.warning("Cloudflare ping failed: %s", e)
        return False
