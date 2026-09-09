"""Viewing types available for an episode, derived from Anime365 metadata."""
from dataclasses import dataclass, field


LANGUAGES = {
    "ru": "Русский", "en": "Английский", "ja": "Японский", "jp": "Японский",
    "uk": "Украинский", "de": "Немецкий", "fr": "Французский", "es": "Испанский",
    "it": "Итальянский", "zh": "Китайский", "ko": "Корейский",
}


def viewing_type(item):
    raw_type = str(item.get("type") or "").strip().lower()
    inferred_kind, inferred_language = "", ""
    for prefix, kind in (("subtitles", "sub"), ("sub", "sub"), ("voice", "voice")):
        if raw_type.startswith(prefix):
            inferred_kind, inferred_language = kind, raw_type[len(prefix):]
            break
    kind = str(item.get("typeKind") or inferred_kind or raw_type or "unknown").strip().lower()
    if kind == "subtitles":
        kind = "sub"
    language = str(item.get("typeLang") or inferred_language).strip().lower()
    # RAW is a viewing type of its own; the API reports its original audio as ja.
    return kind, "" if kind == "raw" else language


@dataclass
class TranslationGroup:
    kind: str
    language: str
    translations: list = field(default_factory=list)

    @property
    def label(self):
        if self.kind == "raw":
            return "Оригинал (RAW)"
        kind = {"sub": "Субтитры", "voice": "Озвучка", "unknown": "Другой тип"}.get(self.kind, self.kind)
        language = LANGUAGES.get(self.language, self.language.upper())
        return f"{kind} · {language}" if language else kind

    @property
    def prompt(self):
        return {"sub": "Выбери субтитры", "voice": "Выбери озвучку",
                "raw": "Выбери версию оригинала"}.get(self.kind, "Выбери перевод")

    @property
    def selection_label(self):
        return {"sub": "Субтитры", "voice": "Озвучка", "raw": "Версия"}.get(self.kind, "Перевод")


def group_translations(translations):
    groups = {}
    for item in translations:
        key = viewing_type(item)
        if key not in groups:
            groups[key] = TranslationGroup(*key)
        groups[key].translations.append(item)
    preferred = [("sub", "ru"), ("voice", "ru"), ("sub", "en"), ("voice", "en"), ("raw", "")]

    def rank(group):
        key = group.kind, group.language
        return (preferred.index(key) if key in preferred else len(preferred), group.label)

    return sorted(groups.values(), key=rank)
