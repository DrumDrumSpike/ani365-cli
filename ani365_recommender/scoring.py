"""Deterministic, explainable genre-based recommendation scoring."""
from __future__ import annotations


MIN_RATED_COMPLETED = 5
RECOMMENDABLE_KINDS = frozenset({"tv", "movie"})


def _genre_rows(anime):
    for genre in anime.get("genres", ()) if isinstance(anime, dict) else ():
        if not isinstance(genre, dict):
            continue
        identifier = str(genre.get("id") or "").strip()
        name = str(genre.get("name") or "").strip()
        if identifier and name:
            yield identifier, name


def rated_completed(profile):
    """Only finished, explicitly rated titles teach the first-version model."""
    return [rate for rate in profile.get("rates", ()) if rate.get("status") == "completed"
            and isinstance(rate.get("score"), int) and 1 <= rate["score"] <= 10]


def preference_rates(profile):
    """Include dropped titles as an explicit strong negative signal.

    A dropped title often has no Shikimori score at all.  Treating it as one
    gives its genres a predictable penalty without letting it satisfy the
    minimum amount of positive rating history.
    """
    result = [dict(rate) for rate in rated_completed(profile)]
    result.extend({**rate, "score": 1} for rate in profile.get("rates", ())
                  if rate.get("status") == "dropped")
    return result


def genre_taste(profile, anime_by_id):
    """Return signed genre weights; low ratings deliberately subtract affinity."""
    taste = {}
    for rate in preference_rates(profile):
        weight = int(rate["score"]) - 5
        if weight == 0:
            continue
        anime = anime_by_id.get(str(rate.get("external_anime_id") or ""), {})
        for identifier, name in _genre_rows(anime):
            previous = taste.get(identifier, {"name": name, "weight": 0})
            taste[identifier] = {"name": name, "weight": previous["weight"] + weight}
    return taste


def top_genres(taste, limit=3):
    return [identifier for identifier, value in sorted(
        taste.items(), key=lambda item: (-item[1]["weight"], item[1]["name"]))
            if value["weight"] > 0][:max(0, int(limit))]


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _franchise(anime):
    value = anime.get("franchise") if isinstance(anime, dict) else None
    return str(value or "").strip().casefold() or None


def rank(profile, anime_by_id, candidates, resolved, limit=36):
    """Rank only Anime365-confirmed public candidates and explain each card."""
    if len(rated_completed(profile)) < MIN_RATED_COMPLETED:
        return []
    taste = genre_taste(profile, anime_by_id)
    seen_shikimori = {str(item.get("external_anime_id") or "") for item in profile.get("rates", ())}
    seen_franchises = {_franchise(anime_by_id.get(identifier)) for identifier in seen_shikimori}
    seen_franchises.discard(None)
    excluded_series = {int(value) for value in profile.get("excluded_anime365_series_ids", ())}
    resolved_by_id = {str(item.get("shikimori_anime_id") or ""): item for item in resolved}
    ranked = []
    for candidate in candidates:
        identifier = str(candidate.get("id") or "").strip()
        match = resolved_by_id.get(identifier)
        if not identifier or identifier in seen_shikimori or not match:
            continue
        if str(candidate.get("kind") or "").strip().casefold() not in RECOMMENDABLE_KINDS:
            continue
        franchise = _franchise(candidate)
        if franchise in seen_franchises:
            continue
        if int(match["anime365_series_id"]) in excluded_series:
            continue
        matches = [(taste[genre_id]["weight"], taste[genre_id]["name"])
                   for genre_id, _name in _genre_rows(candidate) if genre_id in taste]
        affinity = sum(weight for weight, _name in matches)
        if affinity <= 0:
            continue
        public_score = min(10.0, max(0.0, _number(candidate.get("score"))))
        total = affinity * 10 + public_score
        labels = [name for _weight, name in sorted(matches, reverse=True)[:2]]
        ranked.append({
            "anime365_series_id": int(match["anime365_series_id"]),
            "shikimori_anime_id": identifier,
            "_franchise": franchise,
            "score": round(total, 3),
            "title": match["title"], "year": match.get("year"),
            "series_type": match.get("series_type"), "poster_url": match.get("poster_url"),
            "reason": "Совпадает с любимыми жанрами: " + ", ".join(labels),
        })
    unique, series_ids, franchises = [], set(), set()
    for item in sorted(ranked, key=lambda row: (-row["score"], row["title"].casefold(),
                                                 row["anime365_series_id"])):
        franchise = item["_franchise"]
        if item["anime365_series_id"] not in series_ids and (not franchise or franchise not in franchises):
            unique.append({key: value for key, value in item.items() if key != "_franchise"})
            series_ids.add(item["anime365_series_id"])
            if franchise:
                franchises.add(franchise)
        if len(unique) >= max(1, min(50, int(limit))):
            break
    return unique
