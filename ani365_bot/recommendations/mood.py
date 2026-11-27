"""Mood is the primary recommendation constraint. Taste stays secondary.

The free-text note is a short vibe hint. It is length-limited, stripped of
control characters, and never inserted into the system prompt.
"""
from __future__ import annotations

import json
import re
import unicodedata


NOTE_LIMIT = 80
PRIMARY_POOL = 4

# Chip order is the Mini App order. "any" means the previous taste-only behaviour.
MOODS = {
    "cyberpunk": {
        "label": "Киберпанк",
        "search": "киберпанк",
        "primary": ("киберпанк", "cyberpunk"),
        "secondary": ("sci-fi", "научная фантастика", "фантастика"),
    },
    "isekai": {
        "label": "Исекай",
        "search": "исекай",
        "primary": ("исекай", "isekai"),
        "secondary": ("фэнтези", "fantasy"),
    },
    "magic": {
        "label": "Магические битвы",
        "search": "магия",
        "primary": ("магия", "magic", "махо"),
        "secondary": ("сверхъестественное", "supernatural", "фэнтези", "fantasy"),
    },
    "swords": {
        "label": "Мечники/самураи",
        "search": "самурай",
        "primary": ("самурай", "samurai"),
        "secondary": ("боевые искусства", "martial arts", "исторический", "historical"),
    },
    "dark": {
        "label": "Мрачное",
        "search": "ужасы",
        "primary": ("ужасы", "horror", "триллер", "thriller"),
        "secondary": ("психологическое", "psychological"),
    },
    "light": {
        "label": "Лёгкое/комедия",
        "search": "комедия",
        "primary": ("комедия", "comedy"),
        "secondary": ("пародия", "parody"),
    },
    "romance": {
        "label": "Романтика",
        "search": "романтика",
        "primary": ("романтика", "romance"),
        "secondary": ("сёдзё", "седзё", "shoujo"),
    },
    "psychological": {
        "label": "Психологическое",
        "search": "психологическое",
        "primary": ("психологическое", "psychological"),
        "secondary": ("триллер", "thriller", "детектив", "mystery"),
    },
    "sports": {
        "label": "Спорт",
        "search": "спорт",
        "primary": ("спорт", "sports"),
        "secondary": (),
    },
    "slice": {
        "label": "Повседневность",
        "search": "повседневность",
        "primary": ("повседневность", "slice of life"),
        "secondary": (),
    },
    "any": {
        "label": "Любое",
        "search": "",
        "primary": (),
        "secondary": (),
    },
}

MOOD_CHOICES = [{"id": key, "label": value["label"]} for key, value in MOODS.items()]
_NOTE_DROP = re.compile(r"[\x00-\x1f\x7f<>`«»]")
_TOKEN = re.compile(r"[0-9A-Za-zА-Яа-яЁё-]{4,}")
_NOTE_STOPWORDS = frozenset({
    "хочу", "что-то", "типа", "очень", "аниме", "настроение", "просто", "чтобы",
    "какое", "какая", "какой", "нибудь", "либо",
})


def _fold(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    return text.casefold().replace("ё", "е")


def sanitize_note(value):
    """Collapse a mood hint into one short line. It is not a new instruction."""
    text = _NOTE_DROP.sub(" ", unicodedata.normalize("NFKC", str(value or "")))
    return " ".join(text.split())[:NOTE_LIMIT]


def normalize_mood(mood):
    """Canonical chips, note, key, and label. Unknown chips are dropped."""
    raw_chips = []
    raw_note = ""
    if isinstance(mood, dict):
        chips = mood.get("chips")
        if isinstance(chips, (list, tuple)):
            raw_chips = chips
        elif isinstance(chips, str) and chips.strip():
            raw_chips = [chips]
        raw_note = mood.get("note") or ""
    chosen = []
    for chip in raw_chips:
        key = str(chip or "").strip().casefold()
        if key in MOODS and key != "any" and key not in chosen:
            chosen.append(key)
        if len(chosen) == 8:
            break
    if not chosen:
        chosen = ["any"]
    note = sanitize_note(raw_note)
    labels = [MOODS[key]["label"] for key in chosen]
    label = ", ".join(labels)
    if note and chosen != ["any"]:
        label = f"{label}: {note}"[:120]
    elif note and chosen == ["any"]:
        label = note[:120]
    return {
        "chips": chosen,
        "note": note,
        "key": ",".join(sorted(chosen)) + "|" + _fold(note),
        "label": label,
        "labels": labels,
        "open": chosen == ["any"] and not note,
        "json": json.dumps({"chips": chosen, "note": note}, ensure_ascii=False, separators=(",", ":")),
    }


def mood_searches(mood):
    """Shikimori search phrases that pull vibe matches the taste genres would miss."""
    mood = mood if isinstance(mood, dict) and "chips" in mood else normalize_mood(mood)
    if mood.get("open"):
        return []
    terms = []
    for chip in mood.get("chips") or ():
        term = MOODS.get(chip, {}).get("search") or ""
        if term and term not in terms:
            terms.append(term)
        if len(terms) == 3:
            break
    return terms


def _blob(candidate):
    names = []
    genres = candidate.get("genres") if isinstance(candidate.get("genres"), (list, tuple)) else ()
    for genre in genres:
        if isinstance(genre, dict):
            names.append(str(genre.get("name") or ""))
        else:
            names.append(str(genre or ""))
    return _fold(" ".join(names) + " " + str(candidate.get("title") or ""))


def _chip_score(blob, chip, note_hits):
    spec = MOODS.get(chip) or {}
    if any(needle in blob for needle in spec.get("primary") or ()):
        return 2 + note_hits
    if any(needle in blob for needle in spec.get("secondary") or ()):
        return 1 + note_hits
    return note_hits


def _note_hits(blob, note):
    hits = 0
    for token in _TOKEN.findall(_fold(note)):
        if token in _NOTE_STOPWORDS:
            continue
        if token in blob:
            hits += 1
        if hits == 2:
            break
    return hits


def apply_mood(candidates, mood):
    """Keep vibe matches. A few strong matches outrank a pile of loose ones."""
    mood = mood if isinstance(mood, dict) and "open" in mood else normalize_mood(mood)
    rows = [candidate for candidate in candidates or () if isinstance(candidate, dict)]
    if mood.get("open"):
        return [{**candidate, "_mood": 0} for candidate in rows]
    primary, secondary = [], []
    chips = [chip for chip in mood.get("chips") or () if chip != "any"]
    note = mood.get("note") or ""
    for candidate in rows:
        blob = _blob(candidate)
        note_hits = _note_hits(blob, note)
        score = max((_chip_score(blob, chip, note_hits) for chip in chips), default=note_hits)
        if score >= 2:
            primary.append({**candidate, "_mood": score})
        elif score == 1:
            secondary.append({**candidate, "_mood": score})
    chosen = primary if len(primary) >= PRIMARY_POOL else primary + secondary
    chosen.sort(key=lambda candidate: (-int(candidate.get("_mood") or 0), str(candidate.get("id") or "")))
    return chosen
