"""Local raster-to-whiteboard renderer, no network or model dependencies.

Baseline: one scene per image, geometric contour order, then a colour reveal.
This is not semantic object segmentation or the upstream Skills implementation.
Run in a child process so CPU encoding cannot block worker lease heartbeats.
"""

import argparse
import math
from pathlib import Path

import cv2
import numpy as np


def prepare_image(path, width, height):
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("无法读取白板图片")
    scale = min(width / image.shape[1], height / image.shape[0])
    resized = cv2.resize(
        image, (max(1, round(image.shape[1] * scale)), max(1, round(image.shape[0] * scale)))
    )
    canvas = np.full((height, width, 3), 255, dtype=np.uint8)
    x, y = (width - resized.shape[1]) // 2, (height - resized.shape[0]) // 2
    canvas[y : y + resized.shape[0], x : x + resized.shape[1]] = resized
    return canvas


def stroke_segments(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 60, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    # Draw larger shapes first, then detail. No claim of narrative/object ordering.
    contours = sorted(contours, key=lambda item: cv2.arcLength(item, False), reverse=True)[:6000]
    segments = []
    cumulative = []
    total = 0.0
    for contour in contours:
        points = cv2.approxPolyDP(contour, 0.8, False).reshape(-1, 2)
        for start, end in zip(points[:-1], points[1:], strict=True):
            distance = float(np.linalg.norm(end - start))
            if distance:
                total += distance
                segments.append((tuple(map(int, start)), tuple(map(int, end))))
                cumulative.append(total)
    return segments, np.asarray(cumulative)


def frames(image, duration, fps=24):
    if not 0.5 <= duration <= 360 or not 1 <= fps <= 30:
        raise ValueError("白板时长或帧率超出限制")
    segments, cumulative = stroke_segments(image)
    total = cumulative[-1] if len(cumulative) else 0
    canvas = np.full_like(image, 255)
    frame_count = math.ceil(duration * fps)
    cursor = 0
    for index in range(frame_count):
        progress = index / max(1, frame_count - 1)
        ink = min(1.0, progress / 0.68)
        stop = int(np.searchsorted(cumulative, total * ink, side="right"))
        for start, end in segments[cursor:stop]:
            cv2.line(canvas, start, end, (40, 40, 40), 1, cv2.LINE_AA)
        cursor = stop
        if progress > 0.68:
            amount = min(1.0, (progress - 0.68) / 0.22)
            yield cv2.addWeighted(canvas, 1 - amount, image, amount, 0)
        else:
            frame = canvas.copy()
            if cursor:
                cv2.circle(frame, segments[cursor - 1][1], 3, (45, 110, 200), -1)
            yield frame


def render(source, output, duration, width, height, fps=24):
    if width % 2 or height % 2 or not 64 <= min(width, height) <= max(width, height) <= 2048:
        raise ValueError("白板输出尺寸无效")
    image = prepare_image(source, width, height)
    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError("白板编码器无法启动")
    try:
        for frame in frames(image, duration, fps):
            writer.write(frame)
    finally:
        writer.release()
    if not Path(output).is_file() or Path(output).stat().st_size < 100:
        raise RuntimeError("白板编码未生成有效文件")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("duration", type=float)
    parser.add_argument("width", type=int)
    parser.add_argument("height", type=int)
    args = parser.parse_args()
    render(args.source, args.output, args.duration, args.width, args.height)
