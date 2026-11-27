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
    anime_token: str = ""
    hentai_url: str = ""
    hentai_token: str = ""
    mal_client_id: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-6.2-sol"
    openai_reasoning_effort: str = "medium"
    xai_api_key: str = ""
    xai_model: str = "grok-4.7"
    xai_reasoning_effort: str = "medium"
    recommendation_daily_limit: int = 5
    recommendation_candidate_limit: int = 36
    recommendation_llm_timeout: int = 180

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
        anime_token = os.environ.get("ANI365_TOKEN") or os.environ.get("ANI365_ACCESS_TOKEN", "")
        anime_token = anime_token.strip()
        if not anime_token or len(anime_token) > 4096 or any(char.isspace() for char in anime_token):
            raise ConfigError("Set ANI365_TOKEN to one Anime365 access token in .env")
        hentai_url = os.environ.get("HENTAI_BASE_URL", "").strip().rstrip("/")
        hentai_token = os.environ.get("HENTAI_TOKEN") or os.environ.get("HENTAI_ACCESS_TOKEN", "")
        hentai_token = hentai_token.strip()
        if hentai_url and "://" not in hentai_url:
            hentai_url = "https://" + hentai_url
        if hentai_url:
            parsed_hentai = urlsplit(hentai_url)
            if (parsed_hentai.scheme != "https" or not parsed_hentai.hostname or parsed_hentai.username
                    or parsed_hentai.query or parsed_hentai.fragment):
                raise ConfigError("HENTAI_BASE_URL must be an HTTPS API base URL")
            if parsed_hentai.path in ("", "/"):
                hentai_url += "/api"
        if bool(hentai_url) != bool(hentai_token) or (hentai_token and (
                len(hentai_token) > 4096 or any(char.isspace() for char in hentai_token))):
            raise ConfigError("Set both HENTAI_BASE_URL and HENTAI_TOKEN, or neither")
        mal_client_id = os.environ.get("MAL_CLIENT_ID", "").strip()
        if len(mal_client_id) > 512 or any(char.isspace() for char in mal_client_id):
            raise ConfigError("MAL_CLIENT_ID must be a single client identifier")
        openai_api_key = _optional_secret("OPENAI_API_KEY")
        xai_api_key = _optional_secret("XAI_API_KEY")
        openai_model = _model_id("OPENAI_MODEL", "gpt-6.2-sol")
        xai_model = _model_id("XAI_MODEL", "grok-4.7")
        openai_reasoning_effort = _reasoning_effort("OPENAI_REASONING_EFFORT")
        xai_reasoning_effort = _reasoning_effort("XAI_REASONING_EFFORT")
        if openai_reasoning_effort == "none" and _rejects_reasoning_none(openai_model):
            raise ConfigError(
                "OPENAI_REASONING_EFFORT cannot be none for "
                f"{openai_model}; use low, medium, high, xhigh, or max")
        try:
            recommendation_daily_limit = int(os.environ.get("RECOMMENDATION_DAILY_LIMIT", "5"))
            recommendation_candidate_limit = int(os.environ.get("RECOMMENDATION_CANDIDATE_LIMIT", "36"))
            recommendation_llm_timeout = int(os.environ.get("RECOMMENDATION_LLM_TIMEOUT", "180"))
        except ValueError:
            raise ConfigError(
                "RECOMMENDATION_DAILY_LIMIT, RECOMMENDATION_CANDIDATE_LIMIT and "
                "RECOMMENDATION_LLM_TIMEOUT must be integers") from None
        if not 1 <= recommendation_daily_limit <= 50:
            raise ConfigError("RECOMMENDATION_DAILY_LIMIT must be from 1 to 50")
        if not 10 <= recommendation_candidate_limit <= 40:
            raise ConfigError("RECOMMENDATION_CANDIDATE_LIMIT must be from 10 to 40")
        if not 30 <= recommendation_llm_timeout <= 300:
            raise ConfigError("RECOMMENDATION_LLM_TIMEOUT must be from 30 to 300 seconds")
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
                   shikimori_import_interval, anime_token, hentai_url, hentai_token, mal_client_id,
                   openai_api_key, openai_model, openai_reasoning_effort, xai_api_key, xai_model,
                   xai_reasoning_effort, recommendation_daily_limit, recommendation_candidate_limit,
                   recommendation_llm_timeout)


def _optional_secret(name):
    value = os.environ.get(name, "").strip()
    if len(value) > 4096 or any(char.isspace() for char in value):
        raise ConfigError(f"{name} must be a single token")
    return value


def _model_id(name, default):
    raw = os.environ.get(name)
    value = default if raw is None or not raw.strip() else raw.strip()
    if not value or len(value) > 128 or any(char.isspace() for char in value):
        raise ConfigError(f"{name} must be a single model id")
    return value


def _rejects_reasoning_none(model):
    """gpt-6.2-sol returns 400 when reasoning effort is none."""
    name = str(model or "").strip()
    return name == "gpt-6.2-sol" or name.startswith("gpt-6.2-sol-")


def _reasoning_effort(name):
    raw = os.environ.get(name)
    if raw is None:
        return "medium"
    value = raw.strip().lower()
    if value in ("", "off", "omit"):
        return ""
    if value not in {"none", "low", "medium", "high", "xhigh", "max"}:
        raise ConfigError(f"{name} must be none, low, medium, high, xhigh, max, or empty")
    return value
