"""Conservative Shikimori-to-Anime365 candidate selection.

Only an exact MyAnimeList identifier is a verified match.  Title, year and
kind make the manual choice practical, but never create a mapping themselves.
"""
from __future__ import annotations

import re
import unicodedata


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def normalise_title(value):
    """Make equivalent punctuation/case variants comparable without transliteration."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).split())


def shikimori_titles(anime, fallback=""):
    """Return distinct usable titles, ordered from the most useful API fields."""
    values = []
    if isinstance(anime, dict):
        for key in ("russian", "name", "english", "japanese", "synonyms"):
            values.extend(_strings(anime.get(key)))
    values.append(fallback)
    result, seen = [], set()
    for value in values:
        text, key = str(value or "").strip(), normalise_title(value)
        if len(key) < 2 or key in seen:
            continue
        result.append(text[:300])
        seen.add(key)
        if len(result) == 6:
            break
    return result


def shikimori_mal_ids(anime, fallback_id=None):
    """Get the MAL bridge exposed by Shikimori.

    The public Shikimori anime endpoint commonly uses its ``id`` as the MAL
    identifier and leaves ``mal_id`` empty.  Keep both values for compatibility
    with records that expose an explicit ``mal_id``.
    """
    values = []
    if isinstance(anime, dict):
        values.extend((anime.get("mal_id"), anime.get("malId"), anime.get("id")))
    values.append(fallback_id)
    result = set()
    for value in values:
        text = str(value or "").strip()
        if text.isdigit() and int(text) > 0:
            result.add(text)
    return result


def _year(value):
    match = re.search(r"\b(19\d{2}|20\d{2}|21\d{2})\b", str(value or ""))
    return int(match.group(1)) if match else None


def _title_match(source, candidate):
    source, candidate = normalise_title(source), normalise_title(candidate)
    if not source or not candidate:
        return 0, ""
    if source == candidate:
        return 5000, "точное название"
    if min(len(source), len(candidate)) >= 5 and (source in candidate or candidate in source):
        return 3000, "название совпадает"
    source_words, candidate_words = set(source.split()), set(candidate.split())
    overlap = len(source_words & candidate_words)
    if overlap and overlap / max(len(source_words), len(candidate_words)) >= 0.6:
        return 1200 + overlap * 100, "похожее название"
    return 0, ""


def rank_anime365_candidates(anime, rows, *, fallback_title="", fallback_mal_id=None):
    """Deduplicate results from several Anime365 title searches and rank them."""
    source_titles = shikimori_titles(anime, fallback_title)
    mal_ids = shikimori_mal_ids(anime, fallback_mal_id)
    source_year = _year(anime.get("aired_on")) if isinstance(anime, dict) else None
    source_kind = str(anime.get("kind") or "").casefold() if isinstance(anime, dict) else ""
    candidates = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            series_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        titles = row.get("titles") if isinstance(row.get("titles"), dict) else {}
        verified = str(row.get("myAnimeListId") or "") in mal_ids
        title_score, reason = max((_title_match(source, candidate) for source in source_titles
                                   for candidate in titles.values()), default=(0, ""), key=lambda item: item[0])
        score = title_score
        if verified:
            score += 100000
            reason = "MAL ID совпал"
        row_year = _year(row.get("year"))
        if source_year and row_year == source_year:
            score += 100
            if reason and not verified:
                reason += " · год совпал"
        if source_kind and source_kind == str(row.get("type") or "").casefold():
            score += 25
        existing = candidates.get(series_id)
        if existing is None or score > existing[0]:
            candidates[series_id] = (score, reason, row, verified)
    return [{"series_id": series_id, "row": row, "verified_mal": verified,
             "match_reason": reason or "результат поиска"}
            for series_id, (score, reason, row, verified) in sorted(
                candidates.items(), key=lambda item: (-item[1][0], item[0]))]
