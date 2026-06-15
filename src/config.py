"""Core app config. Reads from env (HF Space secrets or local .env)."""
from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

try:
    from dotenv import load_dotenv
    # Try .env first, then .env.local
    for candidate in (".env", ".env.local"):
        if Path(candidate).exists():
            load_dotenv(candidate, override=False)
except ImportError:
    pass


@dataclass(frozen=True)
class Settings:
    # 9router LLM
    ninerouter_base_url: str = field(
        default_factory=lambda: os.getenv(
            "NINEROUTER_BASE_URL", "https://arkydar-9router.hf.space/v1"
        )
    )
    ninerouter_api_key: str = field(
        default_factory=lambda: os.getenv("NINEROUTER_API_KEY", "")
    )
    ninerouter_model: str = field(
        default_factory=lambda: os.getenv("NINEROUTER_MODEL", "utama")
    )

    # Telegram
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", "")
    )

    # Cloudflare Worker (keep-alive)
    cloudflare_worker_url: str = field(
        default_factory=lambda: os.getenv("CLOUDFLARE_WORKER_URL", "")
    )

    # Paths
    work_dir: Path = field(
        default_factory=lambda: Path(os.getenv("WORK_DIR", "/tmp/tvea_work"))
    )
    output_dir: Path = field(
        default_factory=lambda: Path(os.getenv("OUTPUT_DIR", "/tmp/tvea_output"))
    )

    # Limits
    max_video_duration_s: int = field(
        default_factory=lambda: int(os.getenv("MAX_VIDEO_DURATION_S", "120"))
    )
    default_transition: str = field(
        default_factory=lambda: os.getenv("DEFAULT_TRANSITION", "whip_pan_right")
    )

    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    def ninerouter_enabled(self) -> bool:
        return bool(self.ninerouter_api_key)


settings = Settings()
settings.work_dir.mkdir(parents=True, exist_ok=True)
settings.output_dir.mkdir(parents=True, exist_ok=True)
