import asyncio
import json
import logging
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .api import APIError, number, title
from .config import ConfigError
from .media import MediaError
from .store import StateError
from .translations import group_translations


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
    return (encoded[:2400 * 2].decode("utf-16-le", errors="ignore") + "…" +
            encoded[-599 * 2:].decode("utf-16-le", errors="ignore"))


def shortened(text, limit):
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    tail = limit // 3
    return (encoded[:(limit - tail - 1) * 2].decode("utf-16-le", errors="ignore") + "…" +
            encoded[-tail * 2:].decode("utf-16-le", errors="ignore"))


def series_entry(item, index):
    year = str(item.get("year") or "?")[:20]
    kind = str(item.get("typeTitle") or item.get("type") or "?")[:80]
    return f"{index + 1}. {menu_title(item)}\n{year} · {kind}"


def episode_number(item):
    value = item.get("episodeFull")
    if value in (None, ""):
        value = item.get("episodeInt")
    return str(value if value not in (None, "") else "?")[:60]


def episode_label(item):
    return f"{item.get('episodeType') or 'tv'} · {episode_number(item)}"


def translation_label(item):
    return f"{item.get('typeLang') or item.get('type') or '?'} · {item.get('authorsSummary') or item.get('title') or 'Без названия'}"


def latest_episode(episodes):
    """Choose an availability marker without treating an API id as chronological."""
    if not episodes:
        return None
    numbered = [item for item in episodes if number(item.get("episodeInt") or item.get("episodeFull"))]
    if numbered:
        return max(numbered, key=lambda item: number(item.get("episodeInt") or item.get("episodeFull")))
    return episodes[-1]


def next_episode(episodes, watched_id=None, watched_number=None):
    """Return the first current episode after durable viewing progress."""
    watched_id = int(watched_id) if str(watched_id or "").isdigit() else None
    if watched_id is not None:
        for index, item in enumerate(episodes):
            if item.get("id") == watched_id:
                return episodes[index + 1] if index + 1 < len(episodes) else None
    watched_value = number(watched_number)
    if watched_value:
        candidates = [item for item in episodes
                      if number(item.get("episodeInt") or item.get("episodeFull")) > watched_value]
        if candidates:
            return min(candidates, key=lambda item: number(item.get("episodeInt") or item.get("episodeFull")))
        return None
    return episodes[0] if episodes else None


def watchlist_entry(item, index):
    watched = item.get("last_watched_episode_number") or "0"
    available = item.get("last_available_episode_number") or item.get("available_episode_number") or "?"
    enabled = " · уведомления" if item.get("notifications_enabled") else ""
    return f"{index + 1}. {item.get('title') or 'Без названия'}\nпросмотрено {watched}, доступно {available}{enabled}"


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
    user_id: int | None = None

    def pages(self):
        pages = []
        start, used = 0, 0
        for i, item in enumerate(self.items):
            if self.stage == "series":
                size = text_units(series_entry(item, i)) + 2
            elif self.stage in ("watching", "notifications"):
                size = text_units(watchlist_entry(item, i)) + 2
            else:
                size = 0
            if i > start and (i - start >= PAGE_SIZE or used + size > 3600):
                pages.append(range(start, i))
                start, used = i, 0
            used += size
        if self.items:
            pages.append(range(start, len(self.items)))
        return pages


class Bot:
    """Telegram UI and per-user state; Store owns durable SQL state."""

    def __init__(self, config, store, telegram, anime, media=None):
        self.config, self.store = config, store
        self.telegram, self.anime = telegram, anime
        self.media = media
        self.sessions = {}
        self._locks = defaultdict(asyncio.Lock)
        self._update_tasks = set()
        # v1 had a global owner marker. Preserve a pending owner auth flow on migration.
        if self.store.get("awaiting_token") == "1" and not self.store.awaiting_token(config.owner_id):
            self.store.set_awaiting_token(config.owner_id, True)

    # Compatibility for integrations and prior tests that address the owner's session directly.
    @property
    def session(self):
        return self.sessions.get(self.config.owner_id)

    @session.setter
    def session(self, value):
        if value is None:
            self.sessions.pop(self.config.owner_id, None)
            return
        value.user_id = self.config.owner_id
        self.sessions[self.config.owner_id] = value

    @property
    def draining(self):
        return (self.config.data_dir / "drain").exists()

    def _user(self, user_id):
        return self.config.owner_id if user_id is None else int(user_id)

    def _session(self, user_id):
        return self.sessions.get(self._user(user_id))

    def _put_session(self, user_id, session):
        user_id = self._user(user_id)
        session.user_id = user_id
        self.sessions[user_id] = session
        return session

    def heartbeat(self):
        status = {"at": time.time(), "busy": bool(self.sessions), "draining": self.draining}
        temporary = self.config.data_dir / "status.tmp"
        temporary.write_text(json.dumps(status))
        temporary.replace(self.config.data_dir / "status.json")

    async def busy_heartbeat(self):
        while True:
            self.heartbeat()
            await asyncio.sleep(30)

    async def cleanup(self, all_messages=False, user_id=None):
        for chat_id, message_id, created in self.store.due(all_messages, chat_id=user_id):
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

    async def notice(self, text, ttl=NOTICE_TTL, user_id=None):
        user_id = self._user(user_id)
        message = await self.telegram.call("sendMessage", chat_id=user_id, text=text[:4000])
        self.store.track(user_id, message["message_id"], ttl)

    async def reset(self, user_id=None):
        user_id = self._user(user_id)
        self.sessions.pop(user_id, None)
        # A reset is chat-scoped: it cannot delete another user's working menu or notifications.
        await self.cleanup(all_messages=True, user_id=user_id)

    def _set_awaiting_token(self, user_id, enabled):
        user_id = self._user(user_id)
        self.store.set_awaiting_token(user_id, enabled)
        if user_id == self.config.owner_id:
            # A harmless compatibility marker for a database that is later opened by v1.
            self.store.set("awaiting_token", "1" if enabled else "0")

    def _awaiting_token(self, user_id):
        user_id = self._user(user_id)
        return self.store.awaiting_token(user_id) or (
            user_id == self.config.owner_id and self.store.get("awaiting_token") == "1")

    async def request_auth(self, user_id=None):
        user_id = self._user(user_id)
        self._set_awaiting_token(user_id, True)
        await self.notice("Отправь токен доступа Anime365 отдельным сообщением. "
                          "Я удалю сообщение сразу и проверю токен. Отмена: /cancel.",
                          MENU_TTL, user_id)

    def _private_user(self, callback, message):
        source = callback or message or {}
        source_user = source.get("from", {}).get("id")
        chat = (callback.get("message", {}) if callback else source).get("chat", {})
        if not isinstance(source_user, int) or source_user <= 0:
            return None
        if chat.get("type") != "private" or chat.get("id") != source_user:
            return None
        return source_user

    async def handle(self, update):
        callback = update.get("callback_query")
        message = update.get("message")
        user_id = self._private_user(callback, message)
        # Reject outside the allowlist before persistence/API calls and do not echo user content.
        if user_id is None or not self.store.is_allowed(user_id, self.config.owner_id):
            return
        async with self._locks[user_id]:
            try:
                if callback:
                    await self.on_callback(callback, user_id)
                elif message:
                    await self.on_message(message, user_id)
            except (APIError, MediaError, StateError) as exc:
                LOG.warning("Request failed (code %s)", getattr(exc, "code", 0))
                await self.reset(user_id)
                try:
                    await self.notice(str(exc) + "\nНапиши название заново или используй /start.",
                                      user_id=user_id)
                except APIError:
                    LOG.warning("Unable to deliver error notice")

    @staticmethod
    def _command(text):
        return text.split(maxsplit=1)[0].split("@", 1)[0] if text.startswith("/") else ""

    @staticmethod
    def _argument(text):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) == 2 else ""

    async def _admin_command(self, user_id, command, text):
        if command not in ("/allow", "/revoke", "/users"):
            return False
        if user_id != self.config.owner_id:
            await self.notice("Эта команда доступна только владельцу бота.", user_id=user_id)
            return True
        if command == "/users":
            users = self.store.list_allowed_users(self.config.owner_id)
            ids = [str(row["user_id"] if isinstance(row, dict) else row) for row in users]
            await self.notice("Разрешённые пользователи:\n" + "\n".join(ids), MENU_TTL, user_id)
            return True
        argument = self._argument(text)
        if not argument.isdigit() or int(argument) <= 0:
            await self.notice(f"Использование: {command} <telegram_user_id>", user_id=user_id)
            return True
        target = int(argument)
        if command == "/allow":
            added = self.store.add_allowed_user(target, owner_id=self.config.owner_id)
            await self.notice("Пользователь добавлен." if added else "Пользователь уже имеет доступ.",
                              user_id=user_id)
        else:
            removed = self.store.revoke_allowed_user(target, self.config.owner_id)
            if target == self.config.owner_id:
                response = "Владелец всегда имеет доступ и не может быть удалён."
            else:
                response = "Доступ пользователя отозван." if removed else "Пользователя нет в allowlist."
            await self.notice(response, user_id=user_id)
        return True

    async def on_message(self, message, user_id=None):
        user_id = self._user(user_id)
        self.store.track(user_id, message["message_id"], created=message.get("date"))
        # Delete token-bearing incoming messages before calling Anime365.
        await self.cleanup(user_id=user_id)
        text = str(message.get("text") or "").strip()
        command = self._command(text)
        if await self._admin_command(user_id, command, text):
            return
        if command in ("/cancel", "/logout"):
            await self.reset(user_id)
            self._set_awaiting_token(user_id, False)
            if command == "/logout":
                self.store.forget_token(user_id)
                await self.notice("Токен Anime365 удалён из базы. Для подключения: /start.", user_id=user_id)
            else:
                await self.notice("Отменено. Для нового поиска напиши название аниме.", user_id=user_id)
            return
        if self.draining:
            await self.notice("Готовится обновление бота. Попробуй через минуту.", user_id=user_id)
            return
        if command in ("/start", "/auth", "/help"):
            await self.reset(user_id)
            if command == "/auth" or not self.store.token(user_id):
                await self.request_auth(user_id)
            else:
                self._set_awaiting_token(user_id, False)
                help_text = (
                    "Напиши название аниме, затем выбери тайтл, серию, тип просмотра, перевод и качество.\n"
                    "После выбора качества я соберу и отправлю MKV.\n"
                    "/watching — список «Смотрю»; /notifications — настройки уведомлений.\n"
                    "/auth — заменить токен; /logout — удалить токен; /cancel — очистить меню.")
                if user_id == self.config.owner_id:
                    help_text += "\n/allow <id>, /revoke <id>, /users — управление allowlist."
                await self.notice(help_text, MENU_TTL, user_id)
            return
        if command == "/watching":
            if not self.store.token(user_id):
                await self.request_auth(user_id)
                return
            await self.show_watchlist(user_id)
            return
        if command == "/notifications":
            if not self.store.token(user_id):
                await self.request_auth(user_id)
                return
            await self.show_watchlist(user_id, notifications=True)
            return
        if command:
            await self.notice("Неизвестная команда. Справка: /help.", user_id=user_id)
            return
        if self._awaiting_token(user_id):
            await self.reset(user_id)
            if not text or len(text) > 4096 or any(char.isspace() for char in text):
                await self.notice("Нужен один токен Anime365 без пробелов. Пришли его текстовым сообщением.",
                                  user_id=user_id)
                return
            await self.anime.validate(text)
            self.store.save_token(user_id, text)
            self._set_awaiting_token(user_id, False)
            await self.notice("Токен проверен и сохранён. Напиши название аниме.", MENU_TTL, user_id)
            return
        if not self.store.token(user_id):
            await self.reset(user_id)
            await self.request_auth(user_id)
            return
        await self.reset(user_id)
        if not text or len(text) > 200:
            await self.notice("Пришли название аниме текстом, до 200 символов.", user_id=user_id)
            return
        rows = await self.anime.search(text)
        if not rows:
            await self.notice("Ничего не найдено. Попробуй другое название.", user_id=user_id)
            return
        self._put_session(user_id, Session("series", rows))
        await self.render(user_id)

    def _callback(self, session, action, value=0):
        return f"{session.nonce}:{action}:{value}"

    def _persistent_callback(self, action, series_id, episode_id=0):
        return f"w:{action}:{int(series_id)}:{int(episode_id)}"

    def _series_actions_menu(self, session):
        series = session.selected["series"]
        watch = self.store.get_watchlist(session.user_id, int(series["id"]))
        facts = []
        if series.get("year"):
            facts.append(str(series["year"]))
        if series.get("typeTitle") or series.get("type"):
            facts.append(str(series.get("typeTitle") or series.get("type")))
        text = menu_title(series) + ("\n" + " · ".join(facts) if facts else "") + "\n\nЧто сделать?"
        buttons = [[{"text": "Смотреть", "callback_data": self._callback(session, "open")}]]
        if watch:
            mode = {"any": "любая серия", "subtitles": "русские субтитры", "voice": "русская озвучка"}.get(
                watch.get("notification_mode"), "настроить")
            notification = "Уведомления: " + (mode if watch.get("notifications_enabled") else "выключены")
            buttons.extend([
                [{"text": "Убрать из «Смотрю»", "callback_data": self._callback(session, "remove")}],
                [{"text": notification, "callback_data": self._callback(session, "notify")}],
            ])
        else:
            buttons.extend([
                [{"text": "Добавить в «Смотрю»", "callback_data": self._callback(session, "add")}],
                [{"text": "Включить уведомления", "callback_data": self._callback(session, "notify")}],
            ])
        buttons.append([{"text": "Назад", "callback_data": self._callback(session, "back")}])
        buttons.append([{"text": "Отмена", "callback_data": self._callback(session, "cancel")}])
        return text, {"inline_keyboard": buttons}

    def _saved_actions_menu(self, session):
        watch = session.selected["watch"]
        title_text = str(watch.get("title") or "Без названия")
        watched = watch.get("last_watched_episode_number") or "0"
        available = watch.get("last_available_episode_number") or "?"
        text = f"{title_text}\n\nПросмотрено: {watched}\nДоступно: {available}"
        mode = {"any": "любая серия", "subtitles": "русские субтитры", "voice": "русская озвучка"}.get(
            watch.get("notification_mode"), "настроить")
        notification = "Уведомления: " + (mode if watch.get("notifications_enabled") else "выключены")
        buttons = [
            [{"text": "Продолжить", "callback_data": self._callback(session, "continue")}],
            [{"text": "Все серии", "callback_data": self._callback(session, "open")}],
            [{"text": notification, "callback_data": self._callback(session, "notify")}],
            [{"text": "Удалить из «Смотрю»", "callback_data": self._callback(session, "remove")}],
            [{"text": "К списку «Смотрю»", "callback_data": self._callback(session, "watching")}],
            [{"text": "Отмена", "callback_data": self._callback(session, "cancel")}],
        ]
        return text, {"inline_keyboard": buttons}

    def _notification_modes_menu(self, session):
        watch = session.selected["watch"]
        current = {"any": "любая новая серия", "subtitles": "русские субтитры", "voice": "русская озвучка"}.get(
            watch.get("notification_mode"), "неизвестный режим")
        status = current if watch.get("notifications_enabled") else "выключены"
        text = f"{watch.get('title') or 'Без названия'}\n\nТекущие уведомления: {status}\nВыбери режим."
        buttons = [
            [{"text": "Любая новая серия", "callback_data": self._callback(session, "mode", "any")}],
            [{"text": "Русские субтитры", "callback_data": self._callback(session, "mode", "subtitles")}],
            [{"text": "Русская озвучка", "callback_data": self._callback(session, "mode", "voice")}],
            [{"text": "Отключить уведомления", "callback_data": self._callback(session, "disable")}],
            [{"text": "Назад", "callback_data": self._callback(session, "back")}],
        ]
        return text, {"inline_keyboard": buttons}

    def menu(self, user_id=None):
        session = self._session(user_id)
        if session.stage == "series_actions":
            return self._series_actions_menu(session)
        if session.stage == "saved_actions":
            return self._saved_actions_menu(session)
        if session.stage == "notification_modes":
            return self._notification_modes_menu(session)
        headings = {"series": "Выбери аниме", "episodes": "Выбери серию",
                    "translation_types": "Выбери тип просмотра",
                    "translations": "Выбери перевод", "qualities": "Выбери качество",
                    "watching": "Сейчас смотрю", "notifications": "Уведомления"}
        heading = headings[session.stage]
        group = session.selected.get("translation_types")
        if group:
            if session.stage == "translations":
                heading = group.prompt
            heading = f"{group.label}\n{heading}"
        if "series" in session.selected:
            heading = f"{menu_title(session.selected['series'])}\n{heading}"
        pages = session.pages()
        text = f"{heading} · {session.page + 1}/{len(pages)}"
        if session.stage == "series":
            text += "\nНажми кнопку с номером нужного аниме.\n\n"
            text += "\n\n".join(series_entry(session.items[i], i) for i in pages[session.page])
        elif session.stage in ("watching", "notifications"):
            text += "\n\n" + "\n\n".join(watchlist_entry(session.items[i], i) for i in pages[session.page])
        buttons = []
        for i in pages[session.page]:
            item = session.items[i]
            if session.stage == "series":
                if not buttons or len(buttons[-1]) == 4:
                    buttons.append([])
                buttons[-1].append({"text": str(i + 1), "callback_data": self._callback(session, "pick", i)})
                continue
            if session.stage == "episodes":
                label = episode_label(item)
            elif session.stage == "translation_types":
                label = item.label
            elif session.stage == "translations":
                label = translation_label(item)
            elif session.stage == "qualities":
                label = f"{item}p"
            else:
                label = str(item.get("title") or "Без названия")
            buttons.append([{"text": shortened(label, 90), "callback_data": self._callback(session, "pick", i)}])
        navigation = []
        if session.page:
            navigation.append({"text": "←", "callback_data": self._callback(session, "page", session.page - 1)})
        if session.page + 1 < len(pages):
            navigation.append({"text": "→", "callback_data": self._callback(session, "page", session.page + 1)})
        if navigation:
            buttons.append(navigation)
        controls = [{"text": "Отмена", "callback_data": self._callback(session, "cancel")}]
        if session.history:
            controls.insert(0, {"text": "Назад", "callback_data": self._callback(session, "back")})
        buttons.append(controls)
        return text, {"inline_keyboard": buttons}

    async def render(self, user_id=None):
        user_id = self._user(user_id)
        session = self._session(user_id)
        session.nonce = secrets.token_hex(4)
        session.touched = time.time()
        text, keyboard = self.menu(user_id)
        if session.message_id:
            await self.telegram.call("editMessageText", chat_id=user_id, message_id=session.message_id,
                                     text=text, reply_markup=keyboard)
        else:
            result = await self.telegram.call("sendMessage", chat_id=user_id, text=text, reply_markup=keyboard)
            session.message_id = result["message_id"]
        self.store.track(user_id, session.message_id, MENU_TTL)

    def _transition(self, session, stage, items, selected_key=None, selected_value=None):
        session.history.append((session.stage, session.items, session.selected.copy(), session.page))
        if selected_key is not None:
            session.selected[selected_key] = selected_value
        session.stage, session.items, session.page = stage, items, 0

    def _series_record(self, series):
        return {
            "series_id": int(series["id"]),
            "title": menu_title(series),
            "year": str(series.get("year") or "")[:20] or None,
            "series_type": str(series.get("typeTitle") or series.get("type") or "")[:80] or None,
        }

    def _save_series(self, user_id, series):
        row = self._series_record(series)
        return self.store.add_watchlist(user_id, row["series_id"], row["title"], year=row["year"],
                                        series_type=row["series_type"])

    async def _open_current_series(self, user_id):
        session = self._session(user_id)
        series = session.selected["series"]
        rows = await self.anime.episodes(int(series["id"]))
        if not rows:
            await self.notice("У этого аниме нет доступных серий. Выбери другое.", user_id=user_id)
            return
        watch = self.store.get_watchlist(user_id, int(series["id"]))
        if watch:
            current = latest_episode(rows)
            self.store.update_available(user_id, int(series["id"]), current.get("id"), episode_number(current))
            session.selected["watch"] = self.store.get_watchlist(user_id, int(series["id"])) or watch
        self._transition(session, "episodes", rows)
        await self.render(user_id)

    async def _select_saved(self, user_id, item):
        session = self._session(user_id)
        record = self.store.get_watchlist(user_id, int(item["series_id"]))
        if not record:
            await self.notice("Этот аниме уже удалён из списка.", user_id=user_id)
            return
        rows = await self.anime.episodes(int(record["series_id"]))
        if rows:
            current = latest_episode(rows)
            self.store.update_available(user_id, int(record["series_id"]), current.get("id"), episode_number(current))
            record = self.store.get_watchlist(user_id, int(record["series_id"])) or record
        series = {"id": int(record["series_id"]), "titles": {"ru": record["title"]},
                  "year": record.get("year"), "typeTitle": record.get("series_type")}
        self._transition(session, "saved_actions", [], "series", series)
        session.selected["watch"] = record
        session.selected["available_episodes"] = rows
        await self.render(user_id)

    async def show_watchlist(self, user_id=None, notifications=False, message_id=None):
        user_id = self._user(user_id)
        if message_id is None:
            # A command starts a new durable-list view, so retire any prior temporary menu.
            await self.reset(user_id)
        records = self.store.list_watchlist(user_id)
        if not records:
            if message_id is not None:
                await self.reset(user_id)
            await self.notice("Список «Смотрю» пока пуст. Найди аниме и добавь его кнопкой.", user_id=user_id)
            return
        # This is an episodes lookup only; /watching never searches Anime365's catalog.
        for record in records:
            try:
                rows = await self.anime.episodes(int(record["series_id"]))
            except APIError:
                continue
            if rows:
                current = latest_episode(rows)
                self.store.update_available(user_id, int(record["series_id"]), current.get("id"), episode_number(current))
                record["last_available_episode_id"] = current.get("id")
                record["last_available_episode_number"] = episode_number(current)
        session = Session("notifications" if notifications else "watching", records, user_id=user_id)
        session.message_id = message_id
        self._put_session(user_id, session)
        await self.render(user_id)

    async def _configure_notifications(self, user_id, mode):
        session = self._session(user_id)
        watch = session.selected.get("watch") or self._save_series(user_id, session.selected["series"])
        series_id = int(watch["series_id"])
        if mode is None:
            self.store.configure_notifications(user_id, series_id, False,
                                               mode=watch.get("notification_mode", "any"), episodes=())
        else:
            episodes = await self.anime.episodes(series_id)
            # Current ids are the durable baseline; existing episodes produce no new notification.
            self.store.configure_notifications(user_id, series_id, True, mode=mode, episodes=episodes)
        watch = self.store.get_watchlist(user_id, series_id)
        session.selected["watch"] = watch
        session.stage, session.items, session.page = "saved_actions", [], 0
        await self.render(user_id)

    async def _continue(self, user_id, session=None, episodes=None):
        session = session or self._session(user_id)
        watch = session.selected["watch"]
        episodes = episodes if episodes is not None else await self.anime.episodes(int(watch["series_id"]))
        item = next_episode(episodes, watch.get("last_watched_episode_id"),
                            watch.get("last_watched_episode_number"))
        if not item:
            await self.notice("Следующая серия пока недоступна.", user_id=user_id)
            return
        if session.stage != "episodes":
            self._transition(session, "episodes", episodes)
        await self.choose(item, user_id)

    async def _remove_current_series(self, user_id):
        session = self._session(user_id)
        series_id = int(session.selected["series"]["id"])
        self.store.remove_watchlist(user_id, series_id)
        if session.stage == "series_actions":
            await self.render(user_id)
            return
        await self.show_watchlist(user_id, message_id=session.message_id)

    async def _on_persistent_callback(self, callback, user_id):
        parts = str(callback.get("data", "")).split(":")
        if len(parts) != 4 or parts[0] != "w" or not parts[2].isdigit() or not parts[3].isdigit():
            return False
        action, series_id, episode_id = parts[1], int(parts[2]), int(parts[3])
        watch = self.store.get_watchlist(user_id, series_id)
        if not watch:
            await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"],
                                     text="Эта кнопка больше недоступна.")
            return True
        await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
        series = {"id": series_id, "titles": {"ru": watch["title"]}, "year": watch.get("year"),
                  "typeTitle": watch.get("series_type")}
        if action == "d":
            self.store.configure_notifications(user_id, series_id, False,
                                               mode=watch.get("notification_mode", "any"), episodes=())
            return True
        rows = await self.anime.episodes(series_id)
        if action == "o":
            session = self._put_session(user_id, Session("saved_actions", [],
                                                          {"series": series, "watch": watch,
                                                           "available_episodes": rows}))
            if rows:
                current = latest_episode(rows)
                self.store.update_available(user_id, series_id, current.get("id"), episode_number(current))
                session.selected["watch"] = self.store.get_watchlist(user_id, series_id) or watch
            await self.render(user_id)
            return True
        if action == "n":
            session = self._put_session(user_id, Session("saved_actions", [], {"series": series, "watch": watch}))
            await self._continue(user_id, session, rows)
            return True
        if action == "e":
            item = next((row for row in rows if row.get("id") == episode_id), None)
            if not item:
                await self.notice("Эта серия больше недоступна.", user_id=user_id)
                return True
            session = self._put_session(user_id, Session("episodes", rows, {"series": series, "watch": watch}))
            await self.choose(item, user_id)
            return True
        return True

    async def on_callback(self, callback, user_id=None):
        user_id = self._user(user_id)
        if await self._on_persistent_callback(callback, user_id):
            return
        session = self._session(user_id)
        parts = str(callback.get("data", "")).split(":")
        if (not session or session.user_id != user_id or time.time() - session.touched >= MENU_TTL
                or len(parts) != 3 or parts[0] != session.nonce
                or callback.get("message", {}).get("message_id") != session.message_id):
            await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"],
                                     text="Меню устарело. Напиши название заново.")
            return
        await self.telegram.call("answerCallbackQuery", callback_query_id=callback["id"])
        action, value = parts[1], parts[2]
        if action == "cancel":
            await self.reset(user_id)
            return
        if action == "back" and session.history:
            session.stage, session.items, session.selected, session.page = session.history.pop()
            await self.render(user_id)
            return
        if action == "open" and session.stage in ("series_actions", "saved_actions"):
            await self._open_current_series(user_id)
            return
        if action == "add" and session.stage == "series_actions":
            session.selected["watch"] = self._save_series(user_id, session.selected["series"])
            await self.render(user_id)
            return
        if action == "remove" and session.stage in ("series_actions", "saved_actions"):
            await self._remove_current_series(user_id)
            return
        if action == "notify" and session.stage in ("series_actions", "saved_actions"):
            session.selected["watch"] = session.selected.get("watch") or self._save_series(user_id, session.selected["series"])
            self._transition(session, "notification_modes", [])
            await self.render(user_id)
            return
        if action == "mode" and session.stage == "notification_modes" and value in ("any", "subtitles", "voice"):
            await self._configure_notifications(user_id, value)
            return
        if action == "disable" and session.stage == "notification_modes":
            await self._configure_notifications(user_id, None)
            return
        if action == "continue" and session.stage == "saved_actions":
            await self._continue(user_id)
            return
        if action == "watching" and session.stage == "saved_actions":
            await self.show_watchlist(user_id, message_id=session.message_id)
            return
        try:
            index = int(value)
        except ValueError:
            return
        if action == "page" and 0 <= index < len(session.pages()):
            session.page = index
            await self.render(user_id)
        elif action == "pick" and 0 <= index < len(session.items):
            await self.choose(session.items[index], user_id)

    async def _send_selected_video(self, user_id, session, quality):
        selected = session.selected
        group = selected["translation_types"]
        if self.media is None:
            raise MediaError("Скачивание на этом экземпляре бота не настроено.")
        summary = (f"{shortened(menu_title(selected['series']), 500)}\n"
                   f"Серия: {episode_label(selected['episodes'])}\n"
                   f"Тип просмотра: {group.label}\n"
                   f"{group.selection_label}: {translation_label(selected['translations'])[:200]}\n"
                   f"Качество: {quality}p · Формат: MKV")
        await self.telegram.call("editMessageText", chat_id=user_id, message_id=session.message_id,
                                 text=summary + "\n\nСкачиваю и собираю файл…",
                                 reply_markup={"inline_keyboard": []})
        session.touched = time.time()
        token = self.store.token(user_id)
        if not token:
            raise APIError("Сначала подключи Anime365: /start.")
        heartbeat = asyncio.create_task(self.busy_heartbeat())
        try:
            source = await self.anime.media_source(selected["translations"]["id"], quality, token)
            filename = self.media.filename(menu_title(selected["series"]), episode_label(selected["episodes"]), quality)
            async with self.media.prepare(source, filename, group.kind == "sub", group.language) as path:
                try:
                    await self.telegram.call("editMessageText", chat_id=user_id, message_id=session.message_id,
                                             text=summary + "\n\nОтправляю файл…",
                                             reply_markup={"inline_keyboard": []})
                except APIError as exc:
                    LOG.warning("Upload status update failed (code %s); continuing", exc.code)
                series_id = int(selected["series"]["id"])
                watch = self.store.get_watchlist(user_id, series_id)
                markup = None
                if watch:
                    markup = {"inline_keyboard": [[
                        {"text": "Следующая серия", "callback_data": self._persistent_callback("n", series_id)},
                        {"text": "К списку «Смотрю»", "callback_data": self._persistent_callback("o", series_id)},
                    ]]}
                document_params = {"chat_id": user_id, "document": path.as_uri(),
                                   "caption": shortened(summary, 1024)}
                if markup:
                    document_params["reply_markup"] = markup
                await self.telegram.call("sendDocument", **document_params)
                # Progress changes only after Telegram accepted the document.
                if watch:
                    self.store.update_progress(user_id, series_id, selected["episodes"]["id"],
                                               episode_number(selected["episodes"]))
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        await self.reset(user_id)

    async def choose(self, item, user_id=None):
        user_id = self._user(user_id)
        session = self._session(user_id)
        if session.stage == "qualities":
            await self._send_selected_video(user_id, session, item)
            return
        if session.stage == "series":
            self._transition(session, "series_actions", [], "series", item)
            await self.render(user_id)
            return
        if session.stage in ("watching", "notifications"):
            await self._select_saved(user_id, item)
            return
        if session.stage == "episodes":
            rows, stage = group_translations(await self.anime.translations(item["id"])), "translation_types"
            empty = "Для этой серии нет доступных переводов или оригинала. Выбери другую."
        elif session.stage == "translation_types":
            rows, stage = item.translations, "translations"
            empty = "Для этого типа просмотра нет доступных вариантов. Выбери другой."
        else:
            token = self.store.token(user_id)
            if not token:
                raise APIError("Сначала подключи Anime365: /start.")
            rows, stage = await self.anime.available_qualities(item["id"], token), "qualities"
            empty = "Для перевода не найдены доступные разрешения. Попробуй другой перевод."
        if not rows:
            await self.notice(empty, user_id=user_id)
            return
        self._transition(session, stage, rows, session.stage, item)
        await self.render(user_id)

    async def send_watch_notification(self, notification):
        """Watcher callback; notifications are persistent and deliberately not Store.track()ed."""
        user_id, series_id = int(notification["user_id"]), int(notification["series_id"])
        # Watcher performs this check too. Keep it at the delivery boundary for a
        # revoke that races with a queued outbox row or a direct caller.
        if not self.store.is_allowed(user_id, self.config.owner_id):
            return False
        mode = notification.get("mode")
        suffix = {"any": "теперь доступна.", "subtitles": "теперь доступна с русскими субтитрами.",
                  "voice": "теперь доступна с русской озвучкой."}.get(mode, "теперь доступна.")
        episode = {"id": int(notification["episode_id"]),
                   "episodeFull": notification.get("episode_number")}
        text = (f"Новая серия\n{notification.get('title') or 'Без названия'}\n"
                f"Серия {episode_number(episode)} {suffix}")
        keyboard = {"inline_keyboard": [
            [{"text": "Смотреть серию", "callback_data": self._persistent_callback("e", series_id, episode["id"])}],
            [{"text": "Открыть аниме", "callback_data": self._persistent_callback("o", series_id)}],
            [{"text": "Отключить уведомления", "callback_data": self._persistent_callback("d", series_id)}],
        ]}
        await self.telegram.call("sendMessage", chat_id=user_id, text=text[:4000], reply_markup=keyboard)
        return True

    async def _background_handle(self, update):
        try:
            await self.handle(update)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Update objects can contain tokens/messages, so retain no diagnostic from them.
            LOG.error("Unexpected update handling error; update discarded")

    def _start_update_task(self, update):
        task = asyncio.create_task(self._background_handle(update))
        self._update_tasks.add(task)
        task.add_done_callback(self._update_tasks.discard)

    async def _stop_update_tasks(self):
        tasks = list(self._update_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        # Validate credentials before reporting healthy. Never change a pre-existing webhook.
        await self.telegram.call("getMe")
        webhook = await self.telegram.call("getWebhookInfo")
        if webhook.get("url"):
            raise ConfigError("A webhook is already configured for this bot; remove it before using polling")
        await self.cleanup(all_messages=True)
        LOG.info("Bot started; MKV downloads are enabled")
        try:
            while True:
                for user_id, session in list(self.sessions.items()):
                    if time.time() - session.touched >= MENU_TTL:
                        await self.reset(user_id)
                await self.cleanup()
                self.heartbeat()
                offset = int(self.store.get("offset", "0"))
                try:
                    updates = await self.telegram.call("getUpdates", offset=offset, timeout=20, limit=20,
                                                       allowed_updates=["message", "callback_query"])
                    for update in updates:
                        # Persist the offset before dispatching so a poison update cannot stop all users.
                        self.store.set("offset", update["update_id"] + 1)
                        self._start_update_task(update)
                        self.heartbeat()
                    if updates:
                        await asyncio.sleep(0)
                except APIError as exc:
                    if exc.code in (401, 409):
                        raise ConfigError("Telegram token rejected or another polling instance is running") from None
                    LOG.warning("Telegram polling failed (code %s); retrying", exc.code)
                    await asyncio.sleep(min(60, max(3, exc.retry_after)))
        finally:
            await self._stop_update_tasks()
