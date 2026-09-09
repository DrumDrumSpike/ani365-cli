import asyncio
import logging
import os
import signal

from .api import APIError, Anime365, Telegram
from .bot import Bot
from .config import Config, ConfigError
from .store import StateError, Store
from .http import HTTPClient
from .media import MediaProcessor
from .watcher import Watcher


async def main():
    os.umask(0o077)
    config = Config.from_env()
    (config.data_dir / "status.json").unlink(missing_ok=True)
    store = Store(config.data_dir)
    try:
        # Fail before polling if the saved credential cannot be decrypted.
        store.token(config.owner_id)
        client = HTTPClient()
        media = MediaProcessor(config.media_dir)
        # No media task can still be active when this process has just started.
        media.cleanup_stale(age=0)
        bot = Bot(config, store, Telegram(client, config.bot_token, config.telegram_url),
                  Anime365(client, config.anime_url), media)
        watcher = Watcher(store, bot.anime, bot.send_watch_notification, config.watch_check_interval,
                          is_allowed=lambda user_id: store.is_allowed(user_id, config.owner_id))
        tasks = [asyncio.create_task(bot.run()), asyncio.create_task(watcher.run())]

        def stop_tasks():
            for task in tasks:
                task.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop_tasks)
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            stop_tasks()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        (config.data_dir / "status.json").unlink(missing_ok=True)
        store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except Exception as exc:
        # Print only our fixed diagnostics, never arbitrary exception bodies/URLs.
        if isinstance(exc, (ConfigError, StateError, APIError)):
            logging.getLogger(__name__).error("%s", exc)
        else:
            logging.getLogger(__name__).error("Startup/runtime failure (%s). Check configuration and connectivity.", type(exc).__name__)
        raise SystemExit(1) from None
