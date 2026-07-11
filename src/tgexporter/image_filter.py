from __future__ import annotations

import struct
from pathlib import Path


def is_suitable_article_image(path: Path) -> bool:
    size = read_image_size(path)
    if size is None:
        return path.stat().st_size >= 50_000
    width, height = size
    if width < 300 or height < 160:
        return False
    if width * height < 120_000:
        return False
    aspect = width / height
    if 0.8 <= aspect <= 1.25 and width * height < 500_000:
        return False
    return True


def read_image_size(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as file:
        header = file.read(32)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return struct.unpack(">II", header[16:24])
        if header[:3] == b"\xff\xd8\xff":
            return read_jpeg_size(file)
    return None


def read_jpeg_size(file) -> tuple[int, int] | None:
    file.seek(2)
    while True:
        marker_start = file.read(1)
        if not marker_start:
            return None
        if marker_start != b"\xff":
            continue
        marker = file.read(1)
        while marker == b"\xff":
            marker = file.read(1)
        if marker in {b"\xd8", b"\xd9"}:
            continue
        length_bytes = file.read(2)
        if len(length_bytes) != 2:
            return None
        length = struct.unpack(">H", length_bytes)[0]
        if length < 2:
            return None
        if marker in {
            b"\xc0",
            b"\xc1",
            b"\xc2",
            b"\xc3",
            b"\xc5",
            b"\xc6",
            b"\xc7",
            b"\xc9",
            b"\xca",
            b"\xcb",
            b"\xcd",
            b"\xce",
            b"\xcf",
        }:
            data = file.read(length - 2)
            if len(data) < 5:
                return None
            height, width = struct.unpack(">HH", data[1:5])
            return width, height
        file.seek(length - 2, 1)

