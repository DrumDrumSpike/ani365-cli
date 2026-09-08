import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

from .api import APIError, title
from .config import ConfigError

LOG = logging.getLogger(__name__)
PAGE_SIZE = 8
MENU_TTL = 15 * 60
NOTICE_TTL = 60


def text_units(text):
    return len(text.encode("utf-16-le")) // 2


def menu_title(item):
    name = title(item)
    encoded = name.encode("utf-16-le")
    if len(encoded) <= 3000 * 2:
        return name
    # Only pathological titles need shortening; preserve the season at the end.
    return (encoded[:2400 * 2].decode("utf-16-le", errors="ignore") + "…" +
            encoded[-599 * 2:].decode("utf-16-le", errors="ignore"))


def series_entry(item, index):
    year = str(item.get("year") or "?")[:20]
    kind = str(item.get("typeTitle") or item.get("type") or "?")[:80]
    return f"{index + 1}. {menu_title(item)}\n{year} · {kind}"


def episode_label(item):
    return f"{item.get('episodeType') or 'tv'} · {item.get('episodeFull') or item.get('episodeInt') or '?'}"


def translation_label(item):
    return f"{item.get('typeLang') or item.get('type') or 'sub'} · {item.get('authorsSummary') or item.get('title') or 'Без названия'}"


@dataclass
class Session:
    stage: str
    items: list
    selected: dict = field(default_factory=dict)
    nonce: str = field(default_factory=lambda: secrets.token_hex(4))
    touched: float = field(default_factory=time.time)
    page: int = 0
    message_id: int | None = None
    history: list = field(default_factory=list)

    def pages(self):
        pages = []
        start, used = 0, 0
        for i, item in enumerate(self.items):
            size = text_units(series_entry(item, i)) + 2 if self.stage == "series" else 0
            if i > start and (i - start >= PAGE_SIZE or used + size > 3600):
                pages.append(range(start, i))
                start, used = i, 0
            used += size
        if self.items:
            pages.append(range(start, len(self.items)))
        return pages


class Bot:
    def __init__(self, config, store, telegram, anime):
        self.config, self.store = config, store
        self.telegram, self.anime = telegram, anime
        self.session = None

    @property
    def draining(self):
        return (self.config.data_dir / "drain").exists()

    def heartbeat(self):
        status = {"at": time.time(), "busy": self.session is not None, "draining": self.draining}
        temporary = self.config.data_dir / "status.tmp"
        temporary.write_text(json.dumps(status))
        temporary.replace(self.config.data_dir / "status.json")

    async def cleanup(self, all_messages=False):
        for chat_id, message_id, created in self.store.due(all_messages):
            if time.time() - created >= 48 * 3600:
                LOG.warning("An old service message is outside Telegram's deletion window")
                self.store.untrack(chat_id, message_id)
                continue
            try:
                await self.telegram.call("deleteMessage", chat_id=chat_id, message_id=message_id)
            except APIError as exc:
                if exc.missing:
                    self.store.untrack(chat_id, message_id)
                else:
                    self.store.retry_delete(chat_id, message_id, max(30, exc.retry_after))
                    LOG.warning("Service message deletion deferred (code %s)", exc.code)
                if exc.code == 429 or exc.code == 0:
                    break
            else:
                self.store.untrack(chat_id, message_id)

    async def notice(self, text, ttl=NOTICE_TTL):
        message = await self.telegram.call("sendMessage", chat_id=self.config.owner_id, text=text[:4000])
        self.store.track(self.config.owner_id, message["message_id"], ttl)

    async def reset(self):
        self.session = None
        await self.cleanup(all_messages=True)

    async def request_auth(self):
        self.store.set("awaiting_token", "1")
        await self.notice("Отправь токен доступа Anime365 отдельным сообщением. "
                          "Я удалю сообщение сразу и проверю токен. Отмена: /cancel.", MENU_TTL)

    async def handle(self, update):
        callback = update.get("callback_query")
        message = update.get("message")
        # Reject everything outside the owner's private chat BEFORE persistence/API calls.
        source = callback or message or {}
        chat = (callback.get("message", {}) if callback else source).get("chat", {})
        if source.get("from", {}).get("id") != self.config.owner_id:
            return
        if chat.get("type") != "private" or chat.get("id") != self.config.owner_id:
            return
        try:
            if callback:
                await self.on_callback(callback)
            elif message:
                await self.on_message(message)
        except APIError as exc:
            # Never log updates, exception tracebacks, requests or remote response bodies.
            LOG.warning("Request failed (code %s)", exc.code)
            await self.reset()
            try:
                await self.notice(str(exc) + "\nНапиши название заново или используй /start.")
            except APIError:
                LOG.warning("Unable to deliver error notice")

    async def on_message(self, message):
        self.store.track(self.config.owner_id, message["message_id"], created=message.get("date"))
        # Delete token-bearing incoming messages before calling Anime365.
        await self.cleanup()
        text = str(message.get("text") or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0] if text.startswith("/") else ""
        if command in ("/cancel", "/logout"):
            await self.reset()
            self.store.set("awaiting_token", "0")
            if command == "/logout":
                self.store.forget_token(self.config.owner_id)
                await self.notice("Токен Anime365 удалён из базы. Для подключения: /start.")
            else:
                await self.notice("Отменено. Для нового поиска напиши название аниме.")
            return
        if self.draining:
            await self.notice("Готовится обновление бота. Попробуй через минуту.")
            return
        if command in ("/start", "/auth", "/help"):
            await self.reset()
            if command == "/auth" or not self.store.token(self.config.owner_id):
                await self.request_auth()
            else:
                self.store.set("awaiting_token", "0")
                await self.notice("Напиши название аниме, затем выбери серию, субтитры и качество.\n"
                                  "Скачивание и отправка MKV пока не подключены.\n"
                                  "/auth — заменить токен; /logout — удалить токен; /cancel — очистить меню.", MENU_TTL)
            return
        if command:
            await self.notice("Неизвестная команда. Справка: /help.")
            return
        if self.store.get("awaiting_token") == "1":
            await self.reset()
            if not text or len(text) > 4096 or any(c.isspace() for c in text):
                await self.notice("Нужен один токен Anime365 без пробелов. Пришли его текстовым сообщением.")
                return
            await self.anime.validate(text)
            self.store.save_token(self.config.owner_id, text)
            self.store.set("awaiting_token", "0")
            await self.notice("Токен проверен и сохранён. Напиши название аниме.", MENU_TTL)
            return
        if not self.store.token(self.config.owner_id):
            await self.reset()
            await self.request_auth()
            return
        await self.reset()
        if not text or len(text) > 200:
            await self.notice("Пришли название аниме текстом, до 200 символов.")
            return
        rows = await self.anime.search(text)
        if not rows:
            await self.notice("Ничего не найдено. Попробуй другое название.")
            return
        self.session = Session("series", rows)
        await self.render()

    def menu(self):
        session = self.session
        headings = {"series": "Выбери аниме", "episodes": "Выбери серию",
                    "translations": "Выбери субтитры", "qualities": "Выбери качество"}
        heading = headings[session.stage]
        if "series" in session.selected:
            heading = f"{menu_title(session.selected['series'])}\n{heading}"
        pages = session.pages()
        text = f"{heading} · {session.page + 1}/{len(pages)}"
        if session.stage == "series":
            text += "\nНажми кнопку с номером нужного аниме.\n\n"
            text += "\n\n".join(series_entry(session.items[i], i) for i in pages[session.page])
        buttons = []
        for i in pages[session.page]:
            item = session.items[i]
            if session.stage == "series":
                if not buttons or len(buttons[-1]) == 4:
                    buttons.append([])
                buttons[-1].append({"text": str(i + 1), "callback_data": f"{session.nonce}:pick:{i}"})
                continue
            elif session.stage == "episodes":
                label = episode_label(item)
            elif session.stage == "translations":
                label = translation_label(item)
            else:
                label = f"{item}p"
            buttons.append([{"text": label[:90], "callback_data": f"{session.nonce}:pick:{i}"}])
        navigation = []
        if session.page:
            navigation.append({"text": "←", "callback_data": f"{session.nonce}:page:{session.page - 1}"})
        if session.page + 1 < len(pages):
            navigation.append({"text": "→", "callback_data": f"{session.nonce}:page:{session.page + 1}"})
        if navigation:
            buttons.append(navigation)
        controls = [{"text": "Отмена", "callback_data": f"{session.nonce}:cancel:0"}]
        if session.history:
            controls.insert(0, {"text": "Назад", "callback_data": f"{session.nonce}:back:0"})
        buttons.append(controls)
        return text, {"inline_keyboard": buttons}

    async def render(self):
        session = self.session
        # Rotate the revision even on page turns to reject queued double taps.
        session.nonce = secrets.token_hex(4)
        session.touched = time.time()
        text, keyboard = self.menu()
        if session.message_id:
            await self.telegram.call("editMessageText", chat_id=self.config.owner_id,
                                     message_id=session.message_id, text=text, reply_markup=keyboard)
        else:
            result = await self.telegram.call("sendMessage", chat_id=self.config.owner_id,
                                              text=text, reply_markup=keyboard)
            session.message_id = result["message_id"]
        self.store.track(self.config.owner_id, session.message_id, MENU_TTL)

    async def on_callback(self, callback):
        session = self.session
        parts = str(callback.get("data", "")).split(":")
        if (not session or time.time() - session.touched >= MENU_TTL or len(parts) != 3
                or parts[0] != session.nonce
                or callback.get("message", {}).get("message_id") != session.message_id):
            await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"],
                                     text="Меню устарело. Напиши название заново.")
            return
        await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
        action = parts[1]
        if action == "cancel":
            await self.reset()
            return
        if action == "back" and session.history:
            session.stage, session.items, session.selected, session.page = session.history.pop()
            await self.render()
            return
        try:
            index = int(parts[2])
        except ValueError:
            return
        if action == "page" and 0 <= index < len(session.pages()):
            session.page = index
            await self.render()
        elif action == "pick" and 0 <= index < len(session.items):
            await self.choose(session.items[index])

    async def choose(self, item):
        session = self.session
        if session.stage == "qualities":
            selected = session.selected
            text = (f"{menu_title(selected['series'])}\n"
                    f"Серия: {episode_label(selected['episodes'])}\n"
                    f"Субтитры: {translation_label(selected['translations'])[:200]}\n"
                    f"Качество: {item}p · Формат: MKV\n\n"
                    "Выбор завершён. Скачивание и отправка файла пока не подключены.\n"
                    "Это сообщение исчезнет через минуту.")
            await self.reset()
            await self.notice(text)
            return
        if session.stage == "series":
            rows, stage = await self.anime.episodes(item["id"]), "episodes"
            empty = "У этого аниме нет доступных серий. Выбери другое."
        elif session.stage == "episodes":
            rows, stage = await self.anime.translations(item["id"]), "translations"
            empty = "Для этой серии нет доступных субтитров. Выбери другую."
        else:
            token = self.store.token(self.config.owner_id)
            if not token:
                raise APIError("Сначала подключи Anime365: /start.")
            rows, stage = await self.anime.available_qualities(item["id"], token), "qualities"
            empty = "Для перевода не найдены доступные разрешения. Попробуй другой перевод."
        if not rows:
            await self.notice(empty)
            return
        session.history.append((session.stage, session.items, session.selected.copy(), session.page))
        session.selected[session.stage] = item
        session.stage, session.items, session.page = stage, rows, 0
        await self.render()

    async def run(self):
        # Validate credentials before reporting healthy. Never change a pre-existing webhook.
        await self.telegram.call("getMe")
        webhook = await self.telegram.call("getWebhookInfo")
        if webhook.get("url"):
            raise ConfigError("A webhook is already configured for this bot; remove it before using polling")
        await self.cleanup(all_messages=True)
        LOG.info("Bot started; media downloads are disabled")
        while True:
            if self.session and time.time() - self.session.touched >= MENU_TTL:
                await self.reset()
            await self.cleanup()
            self.heartbeat()
            offset = int(self.store.get("offset", "0"))
            try:
                updates = await self.telegram.call("getUpdates", offset=offset, timeout=20, limit=20,
                                                    allowed_updates=["message", "callback_query"])
                for update in updates:
                    try:
                        await self.handle(update)
                    except Exception:
                        # Advance past poison updates, but never emit their potentially secret content.
                        LOG.error("Unexpected update handling error; update discarded")
                        self.session = None
                    self.store.set("offset", update["update_id"] + 1)
                    self.heartbeat()
            except APIError as exc:
                if exc.code in (401, 409):
                    raise ConfigError("Telegram token rejected or another polling instance is running") from None
                LOG.warning("Telegram polling failed (code %s); retrying", exc.code)
                await asyncio.sleep(min(60, max(3, exc.retry_after)))
