from __future__ import annotations

import struct
from pathlib import Path

LOW_DETAIL_EDGE_THRESHOLD = 3.5
LOW_DETAIL_UNIQUE_GRAY_LEVELS = 16


def is_suitable_article_image(path: Path, allow_logo: bool = False) -> bool:
    size = read_image_size(path) or read_pillow_image_size(path)
    if size is None:
        return False
    width, height = size
    aspect = width / height

    # 若允许作为Logo使用，放宽方图面积限制，但仍排除极端细长条
    if allow_logo:
        if width < 220 or height < 120 or (width * height < 50_000):
            return False
        if aspect > 3.2 or aspect < 0.5:
            return False
        if is_low_information_image(path):
            return False
        return True

    if width < 300 or height < 160:
        return False
    if width * height < 120_000:
        return False
    # 排除极端宽高比的横幅广告条（如880x180等Banner）或极细纵向长条
    if aspect > 2.6 or aspect < 0.38:
        return False
    if 0.8 <= aspect <= 1.25 and width * height < 500_000:
        return False
    if is_low_information_image(path):
        return False
    return True


def is_suitable_logo_image(path: Path) -> bool:
    # 判定图片是否适合作为公司Logo备用配图
    return is_suitable_article_image(path, allow_logo=True)


def is_low_information_image(path: Path) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False

    try:
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((96, 96))
            rgb_pixels = list(image.getdata())
            gray = image.convert("L")
            width, height = gray.size
            pixels = list(gray.getdata())
    except OSError:
        return False

    if not pixels or len(set(pixels)) < LOW_DETAIL_UNIQUE_GRAY_LEVELS:
        return True
    if width < 2 and height < 2:
        return True

    total_delta = 0
    color_delta = 0
    comparisons = 0
    for y in range(height):
        row_offset = y * width
        for x in range(width):
            current = pixels[row_offset + x]
            current_rgb = rgb_pixels[row_offset + x]
            if x + 1 < width:
                total_delta += abs(current - pixels[row_offset + x + 1])
                color_delta += sum(abs(current_rgb[index] - rgb_pixels[row_offset + x + 1][index]) for index in range(3))
                comparisons += 1
            if y + 1 < height:
                total_delta += abs(current - pixels[row_offset + width + x])
                color_delta += sum(abs(current_rgb[index] - rgb_pixels[row_offset + width + x][index]) for index in range(3))
                comparisons += 1

    if not comparisons:
        return True
    gray_edge = total_delta / comparisons
    color_edge = color_delta / comparisons
    if gray_edge >= LOW_DETAIL_EDGE_THRESHOLD:
        return False
    channel_ranges = []
    for index in range(3):
        values = [pixel[index] for pixel in rgb_pixels]
        channel_ranges.append(max(values) - min(values))
    if len(set(rgb_pixels)) >= 256 and color_edge >= 4.5 and sum(channel_ranges) >= 120:
        return False
    return True


def read_pillow_image_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(path) as image:
            return image.size
    except OSError:
        return None


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
