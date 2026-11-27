"""Personal candidate pool: diverse years, one entry per franchise, no sequels."""
from __future__ import annotations


ALLOWED_KINDS = frozenset({"tv", "movie", "ona", "ova"})
PREQUEL_RELATIONS = frozenset({"prequel", "parent_story"})


def _relations(value):
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def is_continuation(candidate):
    """True when this title continues an earlier story the model must not start mid-way."""
    if not isinstance(candidate, dict):
        return False
    for relation in _relations(candidate.get("related")):
        if str(relation.get("relation") or "") in PREQUEL_RELATIONS:
            return True
    return False


def _public_score(candidate):
    try:
        return float(candidate.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _year(candidate):
    try:
        year = int(candidate.get("year") or 0)
    except (TypeError, ValueError):
        return 0
    return year if 1900 <= year <= 2100 else 0


def _franchise(candidate):
    return str(candidate.get("franchise") or "").strip().casefold()


def select_candidates(candidates, *, excluded_shikimori_ids, excluded_series_ids,
                      excluded_franchises, already_recommended_ids, limit=36,
                      require_resolved=False):
    """Keep a personal, year-diverse pool of at most ``limit`` verified entries.

    Sequels of unseen franchises are dropped. A franchise the user already has
    on Shikimori is dropped entirely (its next season belongs to the separate
    continuation hint). Previously recommended ids never return.
    """
    excluded_shikimori_ids = {str(value) for value in excluded_shikimori_ids or () if value is not None}
    already_recommended_ids = {str(value) for value in already_recommended_ids or () if value is not None}
    safe_series_ids = set()
    for value in excluded_series_ids or ():
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            safe_series_ids.add(number)
    excluded_series_ids = safe_series_ids
    excluded_franchises = {str(value).strip().casefold() for value in excluded_franchises or ()
                           if value is not None and str(value).strip()}
    limit = max(0, int(limit))
    prepared = []
    seen_ids, seen_series = set(), set()
    for candidate in candidates or ():
        if not isinstance(candidate, dict):
            continue
        identifier = str(candidate.get("id") or "").strip()
        kind = str(candidate.get("kind") or "").strip().casefold()
        if not identifier.isdigit() or kind not in ALLOWED_KINDS or is_continuation(candidate):
            continue
        if identifier in excluded_shikimori_ids or identifier in already_recommended_ids:
            continue
        franchise = _franchise(candidate)
        if franchise and franchise in excluded_franchises:
            continue
        series_id = candidate.get("anime365_series_id")
        if require_resolved and not series_id:
            continue
        if series_id is not None:
            try:
                series_id = int(series_id)
            except (TypeError, ValueError):
                continue
            if series_id in excluded_series_ids or series_id in seen_series:
                continue
        if identifier in seen_ids:
            continue
        seen_ids.add(identifier)
        if series_id is not None:
            seen_series.add(series_id)
        prepared.append(candidate)

    by_franchise = {}
    loose = []
    for candidate in prepared:
        franchise = _franchise(candidate)
        if not franchise:
            loose.append(candidate)
            continue
        current = by_franchise.get(franchise)
        candidate_key = (_year(candidate) == 0, _year(candidate) or 9999, str(candidate.get("id")))
        current_key = None if current is None else (_year(current) == 0, _year(current) or 9999, str(current.get("id")))
        if current is None or candidate_key < current_key:
            by_franchise[franchise] = candidate
    pool = loose + list(by_franchise.values())

    buckets = {}
    for candidate in pool:
        buckets.setdefault(_year(candidate), []).append(candidate)
    for rows in buckets.values():
        rows.sort(key=lambda candidate: (-_public_score(candidate), str(candidate.get("id") or "")))
    years = sorted((year for year in buckets if year), reverse=True)
    if 0 in buckets:
        years.append(0)
    picked = []
    while len(picked) < limit and any(buckets.get(year) for year in years):
        for year in years:
            rows = buckets.get(year) or []
            if not rows:
                continue
            picked.append(rows.pop(0))
            if len(picked) >= limit:
                break
    return picked


def user_franchises(rates, metadata_by_id):
    found = set()
    if not isinstance(metadata_by_id, dict):
        metadata_by_id = {}
    for rate in rates or ():
        if not isinstance(rate, dict):
            continue
        anime = metadata_by_id.get(str(rate.get("external_anime_id") or ""))
        if not isinstance(anime, dict):
            anime = {}
        franchise = str(anime.get("franchise") or "").strip().casefold()
        if franchise:
            found.add(franchise)
    return found
