from __future__ import annotations
import os
import logging
from pydantic import BaseModel


class Settings(BaseModel):
    telegram_bot_token: str
    telegram_channel_id: int | None = None

    # OpenAI — подключим позже
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini-2024-08-06"

    # секретная методология
    literature_urls: str | None = None

    # Прокси — используем ТОЛЬКО для Bybit-запросов
    proxy_url: str | None = None

    timezone: str = os.getenv("TZ", "Europe/Madrid")

    @classmethod
    def load(cls) -> "Settings":
        # Переменные уже загружены в окружение через python-dotenv
        def _parse_int_env(name: str):
            val = os.getenv(name)
            if not val:
                return None
            s = val.strip()
            # Частая ошибка: двойной дефис у channel id
            if s.startswith("--"):
                s = s[1:]
            try:
                return int(s)
            except ValueError:
                logging.getLogger("config").warning(
                    "Invalid int for %s=%r; ignoring.", name, val
                )
                return None

        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_channel_id=_parse_int_env("TELEGRAM_CHANNEL_ID"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini-2024-08-06"),
            literature_urls=os.getenv("LITERATURE_URLS"),
            proxy_url=os.getenv("PROXY_URL"),
            timezone=os.getenv("TZ", "Europe/Madrid"),
        )
