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
        return cls(token.strip(), int(owner), Path(os.environ.get("DATA_DIR", "data")), url)
