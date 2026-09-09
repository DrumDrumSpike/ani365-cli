"""One-time switch of a bot from Telegram's cloud Bot API to the local server."""
import asyncio
import logging

from .api import APIError, Telegram
from .config import Config, ConfigError
from .http import HTTPClient


async def main():
    config = Config.from_env()
    await Telegram(HTTPClient(), config.bot_token).call("logOut")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (APIError, ConfigError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        raise SystemExit(1) from None
    print("Bot was logged out from the cloud Bot API and can now use the local server.")
