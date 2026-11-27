"""Calibrate Shikimori scores against one person's own scale.

Dropped titles stay a separate signal. They never become a fake low score and
never change genre weights. Pair feedback is also not an input here.
"""
from __future__ import annotations


SCORED_STATUSES = frozenset({"completed", "watching", "rewatching", "on_hold"})
SIGNAL_WEIGHT = {
    "top_tier": 2.5,
    "stands_out": 2.0,
    "above_typical": 1.0,
    "typical": 0.25,
    "below_typical": -0.75,
    "stands_out_low": -1.5,
}
STRONG_SIGNALS = frozenset({"top_tier", "stands_out", "above_typical"})
LOW_SIGNALS = frozenset({"below_typical", "stands_out_low"})
LIKED_LIMIT = 36
BELOW_LIMIT = 12
DROPPED_LIMIT = 15
WATCHING_LIMIT = 10
TYPICAL_LIMIT = 8
MOOD_MATCH_LIMIT = 60
MOOD_DROPPED_LIMIT = 20


def _score(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 10 else None


def _tags(anime):
    """Split Shikimori genres, themes and demographics. Untyped tags stay genres."""
    genres, themes, ids = [], [], []
    for genre in (anime or {}).get("genres") or ():
        if not isinstance(genre, dict):
            continue
        name = str(genre.get("name") or genre.get("russian") or "").strip()[:80]
        identifier = str(genre.get("id") or "").strip()
        kind = str(genre.get("kind") or "genre").strip().casefold()
        if identifier.isdigit() and identifier not in ids:
            ids.append(identifier)
        if not name:
            continue
        target = themes if kind in {"theme", "demographic"} else genres
        if name not in target:
            target.append(name)
    return genres, themes, ids


def score_distribution(scores):
    """Median and spread of completed scores. Spread is at least one point.

    Unrated titles (``None`` or Shikimori's zero) and values outside 1–10 are
    left out. Sorting happens only after that filter: ``None`` cannot be
    ordered against an integer score.
    """
    values = []
    for score in scores or ():
        normalized = _score(score)
        if normalized is not None:
            values.append(normalized)
    values.sort()
    if not values:
        return {"median": None, "spread": None, "count": 0, "p25": None, "p75": None, "p90": None,
                "scores": []}
    count = len(values)
    if count % 2:
        median = float(values[count // 2])
    else:
        median = (values[count // 2 - 1] + values[count // 2]) / 2
    deviations = sorted(abs(score - median) for score in values)
    mad = deviations[count // 2] if count % 2 else (
        deviations[count // 2 - 1] + deviations[count // 2]) / 2

    def percentile(fraction):
        index = min(count - 1, max(0, round((count - 1) * fraction)))
        return float(values[index])

    return {"median": median, "spread": float(max(1.0, mad)), "count": count,
            "p25": percentile(0.25), "p75": percentile(0.75), "p90": percentile(0.90),
            "scores": values}


def relative_signal(score, distribution):
    """Map an absolute score onto this user's typical range.

    A 7 is a weak, ordinary signal when the person usually scores 6–7, and a
    clearly low signal when they usually score 9–10.
    """
    score = _score(score)
    median = None if not distribution else distribution.get("median")
    if score is None or median is None:
        return None
    spread = distribution.get("spread") or 1.0
    delta = score - median
    z = delta / spread
    # 9 and 10 are the top of the personal scale even when the median is already 8.
    if score >= 9:
        label = "top_tier"
    elif z >= 1.5:
        label = "stands_out"
    elif z >= 0.75:
        label = "above_typical"
    elif z > -0.75:
        label = "typical"
    elif z > -1.5:
        label = "below_typical"
    else:
        label = "stands_out_low"
    scores = list((distribution or {}).get("scores") or ())
    if scores:
        percentile = round(100 * sum(1 for value in scores if value <= score) / len(scores))
    else:
        percentile = None
    return {"score": score, "delta": round(delta, 2), "z": round(z, 2), "label": label,
            "percentile": percentile, "weight": SIGNAL_WEIGHT[label]}


def taste_profile(rates, metadata_by_id=None):
    """Build a compact profile. Genre weights ignore dropped titles."""
    if not isinstance(metadata_by_id, dict):
        metadata_by_id = {}
    completed_scores = [rate.get("score") for rate in rates or () if isinstance(rate, dict)
                        and str(rate.get("status") or "") == "completed"]
    distribution = score_distribution(completed_scores)
    weights = {}
    titles, dropped = [], []
    for rate in rates or ():
        if not isinstance(rate, dict):
            continue
        status = str(rate.get("status") or "")
        if status not in SCORED_STATUSES | {"dropped"}:
            continue
        identifier = str(rate.get("external_anime_id") or "").strip()
        anime = metadata_by_id.get(identifier)
        if not isinstance(anime, dict):
            anime = {}
        genres, themes, genre_ids = _tags(anime)
        score = _score(rate.get("score"))
        title = str(rate.get("title") or anime.get("title") or "").strip()[:180] or "Без названия"
        updated = rate.get("updated_at", rate.get("rate_updated_at"))
        try:
            updated = float(updated) if updated is not None else 0.0
        except (TypeError, ValueError):
            updated = 0.0
        franchise = str(anime.get("franchise") or "").strip()[:120]
        base = {"id": identifier, "title": title, "status": status, "score": score,
                "genres": genres, "themes": themes, "genre_ids": genre_ids,
                "franchise": franchise, "updated_at": updated}
        if status == "dropped":
            dropped.append(base)
            continue
        signal = relative_signal(score, distribution)
        entry = {**base, "signal": None if signal is None else signal["label"],
                 "delta": None if signal is None else signal["delta"],
                 "percentile": None if signal is None else signal.get("percentile")}
        titles.append(entry)
        if signal is None:
            continue
        for genre_id in genre_ids:
            weights[genre_id] = round(weights.get(genre_id, 0.0) + signal["weight"], 3)
    return {"scale": distribution, "titles": titles, "dropped": dropped, "genre_weights": weights}


def genre_weights(rates, metadata_by_id=None):
    """Genre affinity from relative scores only. Dropped titles do not apply."""
    return taste_profile(rates, metadata_by_id)["genre_weights"]


def top_genres(weights, limit=4):
    ranked = sorted(((genre_id, weight) for genre_id, weight in (weights or {}).items() if weight > 0),
                    key=lambda item: (-item[1], item[0]))
    return [genre_id for genre_id, _weight in ranked[:max(0, int(limit))]]


def source_genre_ids(weights, limit=4):
    """Genres and themes worth searching. Award-winning and demographics are not sources."""
    from .mood import AWARD_WINNING_ID, DEMOGRAPHIC_IDS
    blocked = DEMOGRAPHIC_IDS | {AWARD_WINNING_ID}
    usable = {genre_id: weight for genre_id, weight in (weights or {}).items()
              if str(genre_id) not in blocked}
    return top_genres(usable, limit)


def _recency(entry):
    try:
        return float(entry.get("updated_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _strength(entry):
    delta = entry.get("delta")
    delta = delta if isinstance(delta, (int, float)) else -999
    score = entry.get("score")
    score = score if isinstance(score, (int, float)) else -1
    return (delta, score, _recency(entry))


def _matches(entry, match_ids):
    if not match_ids:
        return True
    return bool({str(value) for value in entry.get("genre_ids") or ()} & match_ids)


def collapse_franchises(entries):
    """One line per franchise. A single title stays as itself."""
    groups = {}
    loose = []
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        franchise = str(entry.get("franchise") or "").strip().casefold()
        if not franchise:
            loose.append(entry)
            continue
        groups.setdefault(franchise, []).append(entry)
    result = list(loose)
    for rows in groups.values():
        if len(rows) == 1:
            result.append(rows[0])
            continue
        best = max(rows, key=_strength)
        scores = [row["score"] for row in rows if isinstance(row.get("score"), int)]
        genres, themes, ids = [], [], []
        for row in rows:
            for name in row.get("genres") or ():
                if name not in genres:
                    genres.append(name)
            for name in row.get("themes") or ():
                if name not in themes:
                    themes.append(name)
            for genre_id in row.get("genre_ids") or ():
                if genre_id not in ids:
                    ids.append(genre_id)
        result.append({
            **best,
            "count": len(rows),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "genres": genres[:8],
            "themes": themes[:8],
            "genre_ids": ids,
            "updated_at": max(_recency(row) for row in rows),
            "delta": best.get("delta"),
        })
    return result


def _take(entries, limit):
    ordered = sorted(collapse_franchises(entries), key=_strength, reverse=True)
    return ordered[:max(0, int(limit))]


def prompt_sections(profile, match_ids=()):
    """Pick what the model should see. Order is signal, then recency, never title.

    A mood id set is applied to the whole list first. The small caps happen after that.
    """
    match_ids = {str(value) for value in match_ids or ()}
    titles = [row for row in (profile or {}).get("titles") or () if _matches(row, match_ids)]
    dropped = [row for row in (profile or {}).get("dropped") or () if _matches(row, match_ids)]
    if match_ids:
        return {
            "mode": "mood",
            "matched": _take(titles, MOOD_MATCH_LIMIT),
            "dropped": _take(dropped, MOOD_DROPPED_LIMIT),
        }
    liked = [row for row in titles if row.get("signal") in STRONG_SIGNALS]
    typical = [row for row in titles if row.get("signal") == "typical"]
    below = [row for row in titles if row.get("signal") in LOW_SIGNALS]
    watching = [row for row in titles if row.get("status") in {"watching", "rewatching", "on_hold"}
                and row.get("signal") is None]
    unrated = [row for row in titles if row.get("status") == "completed" and row.get("score") is None]
    return {
        "mode": "taste",
        "liked": _take(liked, LIKED_LIMIT),
        "typical": _take(typical, TYPICAL_LIMIT),
        "below": _take(below, BELOW_LIMIT),
        "watching": _take(watching, WATCHING_LIMIT),
        "unrated": _take(unrated, TYPICAL_LIMIT),
        "dropped": _take(dropped, DROPPED_LIMIT),
    }


def seed_title_ids(profile, match_ids=(), limit=8):
    """Ids of the strongest titles to ask Shikimori for similar anime."""
    match_ids = {str(value) for value in match_ids or ()}
    rows = [row for row in (profile or {}).get("titles") or ()
            if row.get("status") != "dropped" and _matches(row, match_ids)]
    if not match_ids:
        rows = [row for row in rows if row.get("signal") in STRONG_SIGNALS]
    rows.sort(key=_strength, reverse=True)
    seeds = []
    for row in rows:
        identifier = str(row.get("id") or "").strip()
        if identifier.isdigit() and identifier not in seeds:
            seeds.append(identifier)
        if len(seeds) >= limit:
            break
    return seeds
