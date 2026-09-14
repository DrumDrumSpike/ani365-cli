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
    mini_app_url: str = ""
    web_cookie_secure: bool = True
    playback_completion_threshold: float = 0.9
    download_workers: int = 2
    download_ttl: int = 24 * 60 * 60
    shikimori_client_id: str = ""
    shikimori_client_secret: str = ""
    shikimori_redirect_uri: str = ""
    shikimori_import_interval: int = 12 * 60 * 60

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
        mini_app_url = os.environ.get("MINI_APP_URL", "").strip().rstrip("/")
        if mini_app_url:
            mini = urlsplit(mini_app_url)
            if (mini.scheme != "https" or not mini.hostname or mini.username or mini.query
                    or mini.fragment):
                raise ConfigError("MINI_APP_URL must be an HTTPS URL without credentials or query")
        threshold = os.environ.get("PLAYBACK_COMPLETION_THRESHOLD", "0.9")
        try:
            completion_threshold = float(threshold)
        except ValueError:
            raise ConfigError("PLAYBACK_COMPLETION_THRESHOLD must be a number from 0 to 1") from None
        if not 0 < completion_threshold <= 1:
            raise ConfigError("PLAYBACK_COMPLETION_THRESHOLD must be a number from 0 to 1")
        cookie_secure = os.environ.get("WEB_COOKIE_SECURE", "true").strip().lower()
        if cookie_secure not in ("1", "true", "yes", "0", "false", "no"):
            raise ConfigError("WEB_COOKIE_SECURE must be true or false")
        try:
            download_workers = int(os.environ.get("DOWNLOAD_WORKERS", "2"))
            download_ttl = int(os.environ.get("DOWNLOAD_TTL", str(24 * 60 * 60)))
        except ValueError:
            raise ConfigError("DOWNLOAD_WORKERS and DOWNLOAD_TTL must be positive integers") from None
        if download_workers <= 0 or download_workers > 8 or download_ttl <= 0:
            raise ConfigError("DOWNLOAD_WORKERS and DOWNLOAD_TTL must be positive integers")
        shikimori_id = os.environ.get("SHIKIMORI_CLIENT_ID", "").strip()
        shikimori_secret = os.environ.get("SHIKIMORI_CLIENT_SECRET", "").strip()
        shikimori_redirect = os.environ.get("SHIKIMORI_REDIRECT_URI", "").strip()
        if any((shikimori_id, shikimori_secret, shikimori_redirect)) and not all(
                (shikimori_id, shikimori_secret, shikimori_redirect)):
            raise ConfigError("Set all SHIKIMORI_CLIENT_ID, SHIKIMORI_CLIENT_SECRET and SHIKIMORI_REDIRECT_URI")
        if shikimori_redirect:
            parsed_redirect = urlsplit(shikimori_redirect)
            if (parsed_redirect.scheme != "https" or not parsed_redirect.hostname
                    or parsed_redirect.username or parsed_redirect.query or parsed_redirect.fragment):
                raise ConfigError("SHIKIMORI_REDIRECT_URI must be an HTTPS URL without query")
        try:
            shikimori_import_interval = int(os.environ.get("SHIKIMORI_IMPORT_INTERVAL", str(12 * 60 * 60)))
        except ValueError:
            raise ConfigError("SHIKIMORI_IMPORT_INTERVAL must be a positive number of seconds") from None
        if shikimori_import_interval < 15 * 60:
            raise ConfigError("SHIKIMORI_IMPORT_INTERVAL must be at least 900 seconds")
        return cls(token.strip(), int(owner), Path(os.environ.get("DATA_DIR", "data")), url,
                   telegram_url, Path(os.environ.get("MEDIA_DIR", "/jobs")), watch_check_interval,
                   mini_app_url, cookie_secure in ("1", "true", "yes"), completion_threshold,
                   download_workers, download_ttl, shikimori_id, shikimori_secret, shikimori_redirect,
                   shikimori_import_interval)
