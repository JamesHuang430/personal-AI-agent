"""Validated, image-bound narration annotations. Coordinates are original pixels."""

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator

Pixel = Annotated[int, Field(strict=True, ge=0, le=16000)]
Millis = Annotated[int, Field(strict=True, ge=0, le=120000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Rect(StrictModel):
    x: Pixel
    y: Pixel
    width: Annotated[int, Field(strict=True, ge=1, le=16000)]
    height: Annotated[int, Field(strict=True, ge=1, le=16000)]


class Canvas(StrictModel):
    width: Annotated[int, Field(strict=True, ge=1, le=16000)]
    height: Annotated[int, Field(strict=True, ge=1, le=16000)]


class Cue(StrictModel):
    id: Annotated[int, Field(strict=True, ge=1, le=300)]
    startMs: Millis
    endMs: Millis
    text: str = Field(min_length=1, max_length=500)


class Reveal(StrictModel):
    startMs: Millis
    durationMs: Annotated[int, Field(strict=True, ge=100, le=120000)]
    protectedRegions: list[Rect] = Field(default_factory=list, max_length=16)


class Element(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    label: str = Field(min_length=1, max_length=100)
    sequence: Annotated[int, Field(strict=True, ge=1, le=32)]
    narrativeRole: str = Field(min_length=1, max_length=300)
    cueIds: list[Annotated[int, Field(strict=True, ge=1, le=300)]] = Field(
        min_length=1, max_length=300
    )
    region: Rect
    reveal: Reveal


class Annotation(StrictModel):
    version: Literal[2] = 2
    canvas: Canvas
    sceneDurationMs: Annotated[int, Field(strict=True, ge=1000, le=120000)]
    imageSha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    subtitleAlignment: Literal["estimated", "srt", "provider", "manual"] = "estimated"
    cues: list[Cue] = Field(min_length=1, max_length=300)
    elements: list[Element] = Field(min_length=1, max_length=32)
    inkPath: Literal["grid", "skeleton"] = "grid"

    @model_validator(mode="after")
    def validate_timeline(self):
        if self.canvas.width * self.canvas.height > 16_000_000:
            raise ValueError("画布超过 1600 万像素")
        last = 0
        for index, cue in enumerate(self.cues, 1):
            if cue.id != index or cue.startMs < last or cue.endMs <= cue.startMs:
                raise ValueError("字幕编号须连续，时间不得倒置或重叠")
            if cue.endMs > self.sceneDurationMs:
                raise ValueError("字幕超出场景时长")
            last = cue.endMs
        ids = set()
        last = 0
        cue_order = 0
        for index, element in enumerate(self.elements, 1):
            if element.sequence != index or element.id in ids:
                raise ValueError("区域编号须连续，ID 不得重复")
            ids.add(element.id)
            if len(set(element.cueIds)) != len(element.cueIds):
                raise ValueError("字幕关联不能重复")
            if any(i > len(self.cues) for i in element.cueIds):
                raise ValueError("区域引用了不存在的字幕")
            if min(element.cueIds) < cue_order:
                raise ValueError("区域顺序必须遵循字幕叙事顺序")
            cue_order = min(element.cueIds)
            start = element.reveal.startMs
            end = start + element.reveal.durationMs
            cues = [self.cues[i - 1] for i in element.cueIds]
            if start < last or start < 100:
                raise ValueError("绘制区域必须串行，且开场至少留白 100ms")
            if end > self.sceneDurationMs - 500:
                raise ValueError("绘制超出时长；结尾须留出至少 500ms")
            if start < min(c.startMs for c in cues) or start >= max(c.endMs for c in cues):
                raise ValueError("区域开始时间必须在关联字幕时间内")
            last = end
            for rect in [element.region, *element.reveal.protectedRegions]:
                if (
                    rect.x + rect.width > self.canvas.width
                    or rect.y + rect.height > self.canvas.height
                ):
                    raise ValueError("区域或保护区超出原图边界")
        return self


def image_identity(path):
    path = Path(path)
    with Image.open(path) as image:
        width, height = image.size
    return {"width": width, "height": height}, hashlib.sha256(path.read_bytes()).hexdigest()


def validate_annotation(data, path):
    annotation = Annotation.model_validate(data)
    canvas, digest = image_identity(path)
    if annotation.canvas.model_dump() != canvas or annotation.imageSha256 != digest:
        raise ValueError("图片已变化或画布尺寸不匹配，请重新标注")
    return annotation.model_dump()


def estimated_cues(text, audio_ms):
    # Explicitly an estimate, never presented as word-level forced alignment.
    phrases = re.findall(r"[^。！？；，,!?;]+[。！？；，,!?;]?", text)
    chunks = [p[i : i + 22] for p in phrases for i in range(0, len(p), 22)] or [text]
    total = max(1, sum(map(len, chunks)))
    elapsed = 0
    cues = []
    for index, chunk in enumerate(chunks, 1):
        start = round(audio_ms * elapsed / total)
        elapsed += len(chunk)
        cues.append(
            {
                "id": index,
                "startMs": start,
                "endMs": round(audio_ms * elapsed / total),
                "text": chunk,
            }
        )
    return cues


def annotation_draft(path, speech_text, audio_ms, duration_ms):
    canvas, digest = image_identity(path)
    return {
        "version": 2,
        "canvas": canvas,
        "imageSha256": digest,
        "sceneDurationMs": duration_ms,
        "subtitleAlignment": "estimated",
        "cues": estimated_cues(speech_text, audio_ms),
        "elements": [],
        "inkPath": "grid",
    }


def parse_srt(text):
    if len(text) > 64000:
        raise ValueError("SRT 文件过大")

    def timestamp(value):
        h, m, s, ms = map(int, re.split(r"[:,.]", value))
        if m > 59 or s > 59:
            raise ValueError("SRT 时间格式错误")
        return ((h * 60 + m) * 60 + s) * 1000 + ms

    cues = []
    for block in re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").strip()):
        lines = block.splitlines()
        if lines and lines[0].isdigit():
            lines = lines[1:]
        match = re.fullmatch(
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[,.]\d{3})",
            lines[0] if lines else "",
        )
        if not match or len(lines) < 2:
            raise ValueError("SRT 格式错误")
        cue = Cue(
            id=len(cues) + 1,
            startMs=timestamp(match[1]),
            endMs=timestamp(match[2]),
            text=" ".join(lines[1:]),
        ).model_dump()
        if cue["endMs"] <= cue["startMs"] or (cues and cue["startMs"] < cues[-1]["endMs"]):
            raise ValueError("字幕时间不得倒置、乱序或重叠")
        cues.append(cue)
    return cues


def annotation_digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
