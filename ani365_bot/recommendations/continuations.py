"""Deterministic sequel hints. These never occupy an LLM recommendation slot."""
from __future__ import annotations


# Completed, or a rewatch of something already finished. Plain "watching" counts
# only when the stored episode count has reached the title's last episode.
FINISHED_STATUSES = frozenset({"completed", "rewatching"})
# A sequel already on the list, including planned and dropped, is not "new".
BLOCKING_STATUSES = frozenset({"planned", "watching", "rewatching", "completed", "on_hold", "dropped"})
ALLOWED_KINDS = frozenset({"tv", "movie", "ona", "ova"})
# Shikimori relationKind / relation_en. Only a real sequel is a continuation.
SEQUEL_RELATION = "sequel"
REJECTED_RELATIONS = frozenset({
    "prequel", "side_story", "alternative_version", "alternative_setting",
    "parent_story", "summary", "full_story", "other", "spin_off", "adaptation",
    "character",
})
KIND_RU = {"tv": "сериал", "movie": "фильм", "ona": "ONA", "ova": "OVA"}


def _relation_kind(relation):
    raw = relation.get("relation")
    if raw is None or str(raw).strip() == "":
        raw = relation.get("relation_en")
    text = str(raw or "").strip().casefold().replace("-", " ").replace("_", " ")
    text = "_".join(text.split())
    return text


def _year(value):
    try:
        year = int(value or 0)
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


def _episodes(value):
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _finished(rate, anime):
    status = str(rate.get("status") or "")
    if status in FINISHED_STATUSES:
        return True
    if status != "watching":
        return False
    watched = _episodes(rate.get("episodes"))
    total = _episodes((anime or {}).get("episodes"))
    return total > 0 and watched >= total


def _released(anime):
    status = str((anime or {}).get("status") or "").strip().casefold()
    if not status:
        return True
    if status in {"anons", "not_yet_aired"}:
        blob = " ".join(str((anime or {}).get(key) or "") for key in ("title", "status_text")).casefold()
        return "скоро" in blob
    return True


def _caption(after_title, score, kind, episodes):
    score_text = f", оценка {score}" if isinstance(score, int) and 1 <= score <= 10 else ""
    text = f"Продолжение «{after_title}» · у тебя просмотрено{score_text}"
    details = []
    if kind in KIND_RU:
        details.append(KIND_RU[kind])
    count = _episodes(episodes)
    if count:
        details.append(f"{count} эп.")
    if details:
        text += " · " + ", ".join(details)
    return text


def _sequel_ids(anime):
    found = []
    related = (anime or {}).get("related")
    if not isinstance(related, (list, tuple)):
        return found
    for relation in related:
        if not isinstance(relation, dict) or _relation_kind(relation) != SEQUEL_RELATION:
            continue
        sequel = relation.get("anime") if isinstance(relation.get("anime"), dict) else None
        sequel_id = str((sequel or {}).get("id") or "").strip()
        if sequel_id.isdigit():
            found.append(sequel_id)
    return found


def _later_listed(start_id, nodes, listed_ids):
    """True when some sequel after ``start_id`` is already on a list."""
    pending = list(_sequel_ids(nodes.get(start_id)))
    seen = {start_id}
    while pending:
        sequel_id = pending.pop()
        if sequel_id in seen:
            continue
        seen.add(sequel_id)
        if sequel_id in listed_ids:
            return True
        pending.extend(_sequel_ids(nodes.get(sequel_id)))
    return False


def find_continuations(rates, anime_by_id):
    """Return direct sequels of finished titles that are not already listed.

    Shikimori sometimes labels an earlier show as Sequel of a later prequel
    (the 1996 Kenshin TV under the 1999 OVA). A continuation has to be a
    ``sequel`` that did not air before the finished title. Prequels, side
    stories, alternative versions, spin-offs and the other non-sequel relations
    never qualify. If a later title in that sequel chain is already on any
    list, the gap is not offered either.
    """
    listed = {}
    for rate in rates or ():
        if isinstance(rate, dict):
            listed[str(rate.get("external_anime_id") or "")] = rate
    lookup = anime_by_id if isinstance(anime_by_id, dict) else {}
    nodes = {}
    for anime in lookup.values():
        if isinstance(anime, dict) and str(anime.get("id") or "").isdigit():
            nodes[str(anime["id"])] = anime
        related = (anime or {}).get("related") if isinstance(anime, dict) else None
        if not isinstance(related, (list, tuple)):
            continue
        for relation in related:
            if not isinstance(relation, dict):
                continue
            sequel = relation.get("anime")
            if isinstance(sequel, dict) and str(sequel.get("id") or "").isdigit():
                nodes.setdefault(str(sequel["id"]), sequel)
    found, seen = [], set()
    for rate in rates or ():
        if not isinstance(rate, dict):
            continue
        source_id = str(rate.get("external_anime_id") or "")
        anime = lookup.get(source_id)
        if not isinstance(anime, dict):
            anime = nodes.get(source_id) or {}
        if not _finished(rate, anime):
            continue
        after_title = str(rate.get("title") or anime.get("title") or "").strip()[:180] or "Просмотренный тайтл"
        source_year = _year(anime.get("year"))
        score = rate.get("score")
        try:
            score = int(score) if score is not None and str(score).strip() != "" else None
        except (TypeError, ValueError):
            score = None
        if score is not None and not 1 <= score <= 10:
            score = None
        related = anime.get("related")
        if not isinstance(related, (list, tuple)):
            continue
        for relation in related:
            if not isinstance(relation, dict) or _relation_kind(relation) != SEQUEL_RELATION:
                continue
            sequel = relation.get("anime") if isinstance(relation.get("anime"), dict) else None
            if not sequel:
                continue
            sequel_id = str(sequel.get("id") or "").strip()
            if not sequel_id.isdigit() or sequel_id in seen:
                continue
            existing = listed.get(sequel_id)
            if existing and str(existing.get("status") or "") in BLOCKING_STATUSES:
                continue
            kind = str(sequel.get("kind") or "").strip().casefold()
            if kind not in ALLOWED_KINDS:
                continue
            if not _released(sequel):
                continue
            sequel_year = _year(sequel.get("year"))
            if source_year and sequel_year and sequel_year < source_year:
                continue
            if _later_listed(sequel_id, nodes, set(listed)):
                continue
            seen.add(sequel_id)
            episodes = _episodes(sequel.get("episodes")) or None
            found.append({
                "shikimori_anime_id": sequel_id,
                "mal_id": str(sequel.get("mal_id") or "").strip() or None,
                "title": str(sequel.get("title") or "").strip()[:180] or "Без названия",
                "year": sequel_year,
                "kind": kind,
                "episodes": episodes,
                "after_title": after_title,
                "after_shikimori_anime_id": source_id,
                "after_score": score,
                "caption": _caption(after_title, score, kind, episodes),
            })
    found.sort(key=lambda item: (-(item["year"] or 0), item["title"].casefold(), item["shikimori_anime_id"]))
    return found
