"""Conservative provider timestamp matching. Never invent precise alignment."""

import re
import unicodedata


def letters(text):
    return "".join(
        (c.lower() if c.isascii() else c) for c in text if unicodedata.category(c)[0] in {"L", "N"}
    )


def match_cues(text, records, duration_ms):
    """Keep the original script/punctuation, use only matching provider boundaries."""
    if not isinstance(records, list) or not records or len(records) > 10000:
        return []
    if any(not isinstance(record, dict) for record in records):
        return []
    if not duration_ms or duration_ms <= 0:
        return []
    original = letters(text)
    if not original or original != "".join(letters(str(r.get("text", ""))) for r in records):
        return []
    positions = [i for i, c in enumerate(text) if unicodedata.category(c)[0] in {"L", "N"}]
    count, cursor, previous = 0, 0, 0
    cues = []
    for record in records:
        length = len(letters(str(record.get("text", ""))))
        if not length:
            continue
        start, end = record.get("startMs"), record.get("endMs")
        if (
            type(start) is not int
            or type(end) is not int
            or start < previous
            or end <= start
            or end > duration_ms + 100
        ):
            return []
        end = min(end, duration_ms)
        if end <= start:
            return []
        count += length
        boundary = positions[count] if count < len(positions) else len(text)
        fragment = text[cursor:boundary]
        cursor, previous = boundary, end
        # Merge short word boundaries but preserve pauses and natural sentence ends.
        if (
            cues
            and len(cues[-1]["text"] + fragment) <= 32
            and start - cues[-1]["endMs"] < 350
            and not re.search(r"[。！？!?；;]\s*$", cues[-1]["text"])
        ):
            cues[-1]["text"] += fragment
            cues[-1]["endMs"] = end
        else:
            cues.append({"id": len(cues) + 1, "startMs": start, "endMs": end, "text": fragment})
    if len(cues) > 300 or any(len(c["text"]) > 500 for c in cues):
        return []
    return cues


def timed_cues(speech, duration_ms):
    timing = getattr(speech, "timing", None) or {}
    cues = match_cues(speech.speech_text, timing.get("cues"), duration_ms)
    if not cues or timing.get("source") not in {"edge", "minimax"}:
        return [], "estimated"
    return cues, "provider"
