import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Fixed, credential-free diagnostic safe for logs."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    data_dir: Path = Path("data")
    anime_url: str = "https://smotret-anime.app/api"
    telegram_url: str = "http://telegram-bot-api:8081"
    media_dir: Path = Path("/jobs")
    watch_check_interval: int = 600

    @classmethod
    def from_env(cls):
        # Accept the names in the user's existing .env unchanged.
        token = os.environ.get("BOT_TOKEN") or os.environ.get("token_BotFather", "")
        owner = os.environ.get("OWNER_ID") or os.environ.get("TELEGRAM_ID", "")
        if not token.strip() or ":" not in token:
            raise ConfigError("Set BOT_TOKEN (or token_BotFather) in .env")
        if not owner.isdigit() or int(owner) <= 0:
            raise ConfigError("Set numeric OWNER_ID (or TELEGRAM_ID) in .env")
        url = os.environ.get("ANI365_BASE_URL", cls.anime_url).rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ConfigError("ANI365_BASE_URL must be an HTTPS API base URL")
        telegram_url = os.environ.get("TELEGRAM_BOT_API_URL", cls.telegram_url).rstrip("/")
        telegram = urlsplit(telegram_url)
        if (telegram.scheme not in ("http", "https") or not telegram.hostname or telegram.username
                or telegram.query or telegram.fragment):
            raise ConfigError("TELEGRAM_BOT_API_URL must be an HTTP(S) base URL")
        interval = os.environ.get("WATCH_CHECK_INTERVAL", "600")
        try:
            watch_check_interval = int(interval)
        except ValueError:
            raise ConfigError("WATCH_CHECK_INTERVAL must be a positive number of seconds") from None
        if watch_check_interval <= 0:
            raise ConfigError("WATCH_CHECK_INTERVAL must be a positive number of seconds")
        return cls(token.strip(), int(owner), Path(os.environ.get("DATA_DIR", "data")), url,
                   telegram_url, Path(os.environ.get("MEDIA_DIR", "/jobs")), watch_check_interval)
