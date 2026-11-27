"""Compact prompt. Explanations are Russian and must cite the user's own titles."""
from __future__ import annotations


SIGNAL_RU = {
    "stands_out": "заметно выше обычного",
    "above_typical": "выше обычного",
    "typical": "обычная оценка, слабый сигнал",
    "below_typical": "ниже обычного",
    "stands_out_low": "заметно ниже обычного",
}

SYSTEM_PROMPT = (
    "Ты подбираешь аниме одному человеку. Верни JSON с ключом picks: массив из 0, 1 или 2 объектов "
    "{id, reason}. Можно и нужно вернуть пустой массив, если ни один кандидат не подходит хорошо. "
    "Не заполняй выдачу насильно. id бери только из списка кандидатов, без повторов и без id вне списка. "
    "Не предлагай сиквелы, продолжения и второй тайтл той же франшизы. "
    "reason — одно или два коротких предложения по-русски, без спойлеров. Опирайся только на тайтлы, "
    "оценки и статус «брошено» из профиля. Не выдумывай просмотры. Обычная для этого человека оценка — "
    "слабый плюс; выделяются только оценки заметно выше его шкалы. «Брошено» — отдельный сигнал, не низкая оценка жанра. "
    "Низкая оценка прошлой пары — только контекст, не запрет жанров обоих тайтлов."
)


def _clip(value, limit):
    text = " ".join(str(value or "").split())
    return text[:limit]


def _title_line(entry):
    genres = ", ".join(entry.get("genres") or [])
    if entry.get("status") == "dropped":
        score = entry.get("score")
        score_text = "без оценки" if score is None else f"оценка {score}"
        return f"{_clip(entry.get('title'), 80)} (брошено, {score_text}" + (f", {genres}" if genres else "") + ")"
    signal = SIGNAL_RU.get(entry.get("signal") or "", "без оценки")
    score = entry.get("score")
    score_text = "без оценки" if score is None else str(score)
    delta = entry.get("delta")
    delta_text = "" if delta is None else f", {delta:+g} к медиане"
    genre_text = f", {genres}" if genres else ""
    return (f"{_clip(entry.get('title'), 80)} ({entry.get('status')}, оценка {score_text}, "
            f"{signal}{delta_text}{genre_text})")


def _section(title, rows, limit):
    chosen = list(rows)[:limit]
    if not chosen:
        return ""
    return title + "\n" + "\n".join(f"- {row}" for row in chosen)


def build_user_prompt(profile, feedback, recommended_ids, candidates):
    """Serialize taste, recent pair feedback, history ids, and the verified pool."""
    scale = profile.get("scale") or {}
    median = scale.get("median")
    spread = scale.get("spread")
    if median is None:
        scale_line = "Личная шкала: ещё нет просмотренных тайтлов с оценкой."
    else:
        scale_line = (f"Личная шкала по статусу completed: медиана {median:g}, "
                      f"разброс {(spread if isinstance(spread, (int, float)) else 1):g}, "
                      f"оценок {scale.get('count', 0)}. Оценка около медианы — слабый сигнал.")
    titles = profile.get("titles") or []
    strong = [_title_line(row) for row in titles if row.get("signal") in {"stands_out", "above_typical"}]
    typical = [_title_line(row) for row in titles if row.get("signal") == "typical"]
    low = [_title_line(row) for row in titles if row.get("signal") in {"below_typical", "stands_out_low"}]
    watching = [_title_line(row) for row in titles if row.get("status") in {"watching", "rewatching", "on_hold"}
                and row.get("signal") is None]
    unrated_completed = [_title_line(row) for row in titles
                         if row.get("status") == "completed" and row.get("score") is None]
    dropped = [_title_line(row) for row in profile.get("dropped") or ()]
    feedback_lines = []
    for item in list(feedback or [])[:5]:
        rating = item.get("pair_rating")
        if item.get("pair_skipped"):
            verdict = "пропуск"
        elif rating is None:
            verdict = "без оценки пары"
        else:
            try:
                verdict = f"оценка пары {int(rating)} из 10"
            except (TypeError, ValueError):
                verdict = "без оценки пары"
        liked = ", ".join(_clip(title, 60) for title in item.get("interested_titles") or []) or "ни один не отмечен"
        feedback_lines.append(f"{item.get('source')}: {verdict}; заинтересовали: {liked}")
    history = [str(value) for value in recommended_ids if str(value).isdigit()]
    history_line = "Уже рекомендованные id (не повторять): " + (
        ", ".join(history[:80]) + ("…" if len(history) > 80 else "") if history else "нет")
    candidate_lines = []
    for candidate in candidates or ():
        if not isinstance(candidate, dict):
            continue
        genres = candidate.get("genres") if isinstance(candidate.get("genres"), (list, tuple)) else ()
        names = []
        for genre in genres:
            if isinstance(genre, dict):
                names.append(str(genre.get("name") or "").strip())
            else:
                names.append(str(genre).strip())
        names = [name for name in names if name][:4]
        year = candidate.get("year") or "?"
        kind = candidate.get("kind") or "?"
        candidate_lines.append(
            f"{candidate.get('id')}|{_clip(candidate.get('title'), 80)}|{year}|{kind}|{', '.join(names)}")
    parts = [
        scale_line,
        _section("Заметно нравится:", strong, 8),
        _section("Обычный уровень:", typical, 6),
        _section("Ниже обычного:", low, 6),
        _section("Смотрит или отложил без отдельной сильной оценки:", watching, 6),
        _section("Просмотрено без оценки (в списке, на шкалу не влияет):", unrated_completed, 6),
        _section("Брошено (флаг отдельный от оценки, жанры не штрафуются автоматически):", dropped, 8),
        _section("Недавние отзывы на пары (только контекст, веса жанров не меняются):", feedback_lines, 5),
        history_line,
        "Кандидаты, уже проверенные на Anime365, id|название|год|тип|жанры:",
        "\n".join(candidate_lines) if candidate_lines else "(пусто — верни picks: [])",
    ]
    return "\n".join(part for part in parts if part)
