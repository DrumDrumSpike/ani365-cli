"""Calibrate Shikimori scores against one person's own scale.

Dropped titles stay a separate signal. They never become a fake low score and
never change genre weights. Pair feedback is also not an input here.
"""
from __future__ import annotations


SCORED_STATUSES = frozenset({"completed", "watching", "rewatching", "on_hold"})
SIGNAL_WEIGHT = {
    "stands_out": 2.0,
    "above_typical": 1.0,
    "typical": 0.25,
    "below_typical": -0.75,
    "stands_out_low": -1.5,
}


def _score(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 10 else None


def _genres(anime):
    result = []
    for genre in (anime or {}).get("genres") or ():
        if not isinstance(genre, dict):
            continue
        name = str(genre.get("name") or "").strip()
        identifier = str(genre.get("id") or "").strip()
        if name:
            result.append({"id": identifier, "name": name[:80]})
    return result


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
        return {"median": None, "spread": None, "count": 0}
    count = len(values)
    if count % 2:
        median = float(values[count // 2])
    else:
        median = (values[count // 2 - 1] + values[count // 2]) / 2
    deviations = sorted(abs(score - median) for score in values)
    mad = deviations[count // 2] if count % 2 else (
        deviations[count // 2 - 1] + deviations[count // 2]) / 2
    return {"median": median, "spread": float(max(1.0, mad)), "count": count}


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
    if z >= 1.5:
        label = "stands_out"
    elif z >= 0.75:
        label = "above_typical"
    elif z > -0.75:
        label = "typical"
    elif z > -1.5:
        label = "below_typical"
    else:
        label = "stands_out_low"
    return {"score": score, "delta": round(delta, 2), "z": round(z, 2), "label": label,
            "weight": SIGNAL_WEIGHT[label]}


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
        genres = _genres(anime)
        score = _score(rate.get("score"))
        title = str(rate.get("title") or anime.get("title") or "").strip()[:180] or "Без названия"
        if status == "dropped":
            dropped.append({"id": identifier, "title": title, "status": "dropped", "score": score,
                            "genres": [genre["name"] for genre in genres]})
            continue
        signal = relative_signal(score, distribution)
        entry = {"id": identifier, "title": title, "status": status, "score": score,
                 "signal": None if signal is None else signal["label"],
                 "delta": None if signal is None else signal["delta"],
                 "genres": [genre["name"] for genre in genres]}
        titles.append(entry)
        if signal is None:
            continue
        for genre in genres:
            if not genre["id"]:
                continue
            weights[genre["id"]] = round(weights.get(genre["id"], 0.0) + signal["weight"], 3)
    return {"scale": distribution, "titles": titles, "dropped": dropped, "genre_weights": weights}


def genre_weights(rates, metadata_by_id=None):
    """Genre affinity from relative scores only. Dropped titles do not apply."""
    return taste_profile(rates, metadata_by_id)["genre_weights"]


def top_genres(weights, limit=4):
    ranked = sorted(((genre_id, weight) for genre_id, weight in (weights or {}).items() if weight > 0),
                    key=lambda item: (-item[1], item[0]))
    return [genre_id for genre_id, _weight in ranked[:max(0, int(limit))]]
