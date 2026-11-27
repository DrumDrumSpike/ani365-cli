"""Accept only 0–2 distinct ids from the candidate list that was sent to the model."""
from __future__ import annotations

import json


class ParseError(ValueError):
    """The model output is not a usable subset of the candidate ids."""


def _candidate_id(raw_id):
    if isinstance(raw_id, bool) or isinstance(raw_id, float) or raw_id is None:
        raise ParseError("id is not a candidate")
    if isinstance(raw_id, int):
        identifier = str(raw_id)
    elif isinstance(raw_id, str):
        identifier = raw_id.strip()
    else:
        raise ParseError("id is not a candidate")
    if not identifier.isdigit() or int(identifier) <= 0:
        raise ParseError("id is not a candidate")
    return identifier


def parse_picks(payload, candidate_ids):
    """Return ``[{"id", "reason"}, ...]`` or raise ``ParseError``.

    An empty list is valid: the model is allowed to recommend nothing. Any id
    outside the candidate set, a duplicate, or more than two picks rejects the
    whole response rather than silently dropping entries.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ParseError("malformed JSON") from None
    if not isinstance(payload, dict):
        raise ParseError("response is not an object")
    picks = payload.get("picks")
    if not isinstance(picks, list) or len(picks) > 2:
        raise ParseError("picks must be a list of at most 2")
    allowed = {str(value) for value in candidate_ids}
    seen, result = set(), []
    for item in picks:
        if not isinstance(item, dict):
            raise ParseError("pick is not an object")
        identifier = _candidate_id(item.get("id"))
        if identifier not in allowed or identifier in seen:
            raise ParseError("id is not a distinct candidate")
        reason = item.get("reason")
        if not isinstance(reason, str):
            raise ParseError("missing reason")
        reason = " ".join(reason.split())
        if not reason or len(reason) > 400:
            raise ParseError("reason length")
        seen.add(identifier)
        result.append({"id": identifier, "reason": reason})
    return result
