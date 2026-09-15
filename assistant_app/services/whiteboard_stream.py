"""Service adapter for the pinned MIT upstream renderer; no network/model calls."""

import argparse
import json
import math
from copy import deepcopy
from pathlib import Path

import cv2
import numpy as np

from assistant_app.services.whiteboard_annotations import validate_annotation
from assistant_app.vendor.srt_whiteboard.render_stream_whiteboard import RegionStreamRenderer
from assistant_app.vendor.srt_whiteboard.stream_render import Config


def prepare(source, annotation, width, height):
    if width % 2 or height % 2 or not 64 <= min(width, height) <= max(width, height) <= 2048:
        raise ValueError("输出尺寸无效")
    annotation = validate_annotation(annotation, source)
    image = cv2.imdecode(np.fromfile(source, dtype=np.uint8), cv2.IMREAD_COLOR)
    scale = min(width / image.shape[1], height / image.shape[0])
    w, h = max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale))
    x, y = (width - w) // 2, (height - h) // 2
    # Preserve original background, including white and warm paper styles.
    bg = np.median(
        np.array([image[0, 0], image[0, -1], image[-1, 0], image[-1, -1]]), axis=0
    ).astype(np.uint8)
    canvas = np.empty((height, width, 3), np.uint8)
    canvas[:] = bg
    canvas[y : y + h, x : x + w] = cv2.resize(image, (w, h))
    data = deepcopy(annotation)
    sx, sy = w / image.shape[1], h / image.shape[0]
    for element in data["elements"]:
        for rect in [element["region"], *element["reveal"]["protectedRegions"]]:
            left, top = x + round(rect["x"] * sx), y + round(rect["y"] * sy)
            left, top = min(width - 1, left), min(height - 1, top)
            right = x + round((rect["x"] + rect["width"]) * sx)
            bottom = y + round((rect["y"] + rect["height"]) * sy)
            rect.update(x=left, y=top, width=max(1, right - left), height=max(1, bottom - top))
    data["canvas"] = {"width": width, "height": height}
    cfg = Config(
        fps=24,
        grid_edge=math.gcd(width, height, 8),
        cap_long_edge=max(width, height),
        ink_path_mode=data["inkPath"],
        match_bg=False,
        target_hand_height=max(48, height // 8),
        canvas_hex="#" + "".join(f"{int(c):02x}" for c in bg[::-1]),
    )
    # Grid divides both dimensions exactly; rendering runs in a timeout-bounded child.
    return RegionStreamRenderer(canvas, data, cfg, None, False), data


def render(source, annotation, output, width, height):
    renderer, data = prepare(source, annotation, width, height)
    fps = 24
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError("白板编码器无法启动")
    cursor = 0

    def static_until(frame):
        nonlocal cursor
        while cursor < frame:
            writer.write(renderer.drawn.astype(np.uint8))
            cursor += 1

    try:
        elements = data["elements"]
        for index, element in enumerate(elements):
            start = round(element["reveal"]["startMs"] * fps / 1000)
            end = round(
                (element["reveal"]["startMs"] + element["reveal"]["durationMs"]) * fps / 1000
            )
            static_until(start)
            allowed = renderer._allowed_mask(element, elements[index + 1 :])
            ink = max(1, round((end - start) * 2 / 3))
            color = end - start - ink
            if not allowed.any():
                static_until(end)
                continue
            if data["inkPath"] == "skeleton":
                strokes = renderer._region_skeleton_strokes(allowed)
                samples, lifts = [], set()
                for stroke in strokes:
                    if samples:
                        lifts.add(len(samples))
                    samples.extend(stroke)
                renderer._lay_ink(writer, ink, samples, lifts, allowed)
            else:
                path = renderer._region_grid_path(allowed)
                if path:
                    samples, lifts, cells = renderer._grid_plan(path)
                    renderer._lay_ink_grid(writer, ink, samples, lifts, cells, path, allowed)
                else:
                    renderer._lay_ink(writer, ink, [], set(), allowed)
            renderer._wash_contour(writer, color, allowed)
            cursor = end
        # Do NOT reveal unannotated pixels at the end (upstream did a full-image swap).
        static_until(round(data["sceneDurationMs"] * fps / 1000))
    finally:
        writer.release()
    if not Path(output).is_file() or Path(output).stat().st_size < 100:
        raise RuntimeError("未生成有效白板影片")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("annotation")
    parser.add_argument("output")
    parser.add_argument("width", type=int)
    parser.add_argument("height", type=int)
    args = parser.parse_args()
    render(
        args.source,
        json.loads(Path(args.annotation).read_text(encoding="utf-8")),
        args.output,
        args.width,
        args.height,
    )
