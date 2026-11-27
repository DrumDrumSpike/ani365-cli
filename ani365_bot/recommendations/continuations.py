"""Deterministic sequel hints. These never occupy an LLM recommendation slot."""
from __future__ import annotations


WATCHED_STATUSES = frozenset({"completed", "watching", "rewatching"})
# A sequel already on the list, including planned and dropped, is not "new".
BLOCKING_STATUSES = frozenset({"planned", "watching", "rewatching", "completed", "on_hold", "dropped"})
ALLOWED_KINDS = frozenset({"tv", "movie", "ona", "ova"})


def find_continuations(rates, anime_by_id):
    """Return direct sequels of watched titles that are not already listed.

    Only a Shikimori ``sequel`` relation counts. Spin-offs and side stories stay
    out of this block, and the caller's LLM pool is a separate list.
    """
    by_id = {}
    for rate in rates or ():
        if isinstance(rate, dict):
            by_id[str(rate.get("external_anime_id") or "")] = rate
    found, seen = [], set()
    lookup = anime_by_id if isinstance(anime_by_id, dict) else {}
    for rate in rates or ():
        if not isinstance(rate, dict) or str(rate.get("status") or "") not in WATCHED_STATUSES:
            continue
        source_id = str(rate.get("external_anime_id") or "")
        anime = lookup.get(source_id)
        if not isinstance(anime, dict):
            anime = {}
        after_title = str(rate.get("title") or anime.get("title") or "").strip()[:180] or "Просмотренный тайтл"
        related = anime.get("related")
        if not isinstance(related, (list, tuple)):
            continue
        for relation in related:
            if not isinstance(relation, dict) or str(relation.get("relation") or "") != "sequel":
                continue
            sequel = relation.get("anime") if isinstance(relation.get("anime"), dict) else None
            if not sequel:
                continue
            sequel_id = str(sequel.get("id") or "").strip()
            if not sequel_id.isdigit() or sequel_id in seen:
                continue
            existing = by_id.get(sequel_id)
            if existing and str(existing.get("status") or "") in BLOCKING_STATUSES:
                continue
            kind = str(sequel.get("kind") or "").strip().casefold()
            if kind and kind not in ALLOWED_KINDS:
                continue
            try:
                year = int(sequel.get("year") or 0) or None
            except (TypeError, ValueError):
                year = None
            seen.add(sequel_id)
            found.append({
                "shikimori_anime_id": sequel_id,
                "mal_id": str(sequel.get("mal_id") or "").strip() or None,
                "title": str(sequel.get("title") or "").strip()[:180] or "Без названия",
                "year": year,
                "kind": kind or None,
                "after_title": after_title,
                "after_shikimori_anime_id": source_id,
            })
    found.sort(key=lambda item: (-(item["year"] or 0), item["title"].casefold(), item["shikimori_anime_id"]))
    return found
