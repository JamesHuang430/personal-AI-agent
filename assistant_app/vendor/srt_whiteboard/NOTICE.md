# SRT whiteboard rendering core

Source: https://github.com/geeklee/srt-whiteboard-animation
Pinned commit: 696a7243c0e6ffb6827676e539c2ca5ebae2bf6b
Copyright (c) 2026 江哥是老登啊. MIT License (included).

Vendored from the previously reviewed source snapshot. Only rendering primitives and
RegionStreamRenderer are retained; standalone CLI, environment setup, single-image
renderer and transcoding/merging paths are omitted. Imports are package-relative.
Local changes: fix blank grid region argument count; service adapter owns input
validation, exact frame scheduling, canvas sizing, and avoids final full-image leaks.
No branded hand asset is included; procedural pen is used.
