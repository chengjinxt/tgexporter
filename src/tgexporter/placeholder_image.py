from __future__ import annotations

import struct
import zlib
from pathlib import Path

BRAND_TEXT = "firemail 科技频道"


def write_placeholder_png(path: Path, width: int = 1200, height: int = 630, title: str | None = None) -> None:
    try:
        write_pillow_cover(path, width=width, height=height, title=title or "科技资讯")
    except Exception:
        write_raw_png(path, width=width, height=height)


def write_pillow_cover(path: Path, width: int, height: int, title: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), (18, 31, 45))
    draw = ImageDraw.Draw(image, "RGBA")

    for y in range(height):
        ratio = y / max(height - 1, 1)
        color = (
            int(14 + ratio * 18),
            int(35 + ratio * 62),
            int(58 + ratio * 72),
            255,
        )
        draw.line((0, y, width, y), fill=color)

    step = max(42, width // 18)
    for x in range(-step, width + height, step):
        draw.line((x, 0, x - height, height), fill=(90, 210, 220, 58), width=max(1, width // 420))
    for index in range(9):
        x = int(width * (0.08 + index * 0.105))
        y = int(height * (0.18 + (index % 3) * 0.19))
        draw.rounded_rectangle(
            (x, y, x + width * 0.07, y + height * 0.07),
            radius=max(2, width // 120),
            outline=(150, 235, 255, 70),
            width=max(1, width // 360),
        )

    panel_top = int(height * 0.42)
    draw.rectangle((0, panel_top, width, height), fill=(0, 0, 0, 122))
    draw.rectangle((0, panel_top, width, panel_top + max(4, height // 75)), fill=(36, 214, 188, 190))

    margin = max(18, int(min(width, height) * 0.055))
    brand_font = load_font(max(18, int(min(width, height) * 0.062)))
    title_font = load_font(max(28, int(min(width, height) * 0.105)))
    draw.text(
        (margin, margin),
        BRAND_TEXT,
        font=brand_font,
        fill=(255, 255, 255, 255),
        stroke_width=max(1, width // 620),
        stroke_fill=(0, 0, 0, 180),
    )

    title_lines = wrap_text(title, title_font, max_width=width - margin * 2)
    title_y = panel_top + margin
    for line in title_lines[:3]:
        draw.text(
            (margin, title_y),
            line,
            font=title_font,
            fill=(255, 222, 72, 255),
            stroke_width=max(2, width // 360),
            stroke_fill=(0, 0, 0, 220),
        )
        title_y += int(title_font.size * 1.16)

    image.save(path, format="PNG", optimize=True)


def wrap_text(text: str, font, max_width: int) -> list[str]:
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        trial = current + char
        if current and text_width(trial, font) > max_width:
            lines.append(current)
            current = char
        else:
            current = trial
    if current:
        lines.append(current)
    if len(lines) <= 3:
        return lines
    shortened = lines[:2]
    last = lines[2]
    while text_width(last + "...", font) > max_width and last:
        last = last[:-1]
    shortened.append(last.rstrip() + "...")
    return shortened


def text_width(text: str, font) -> int:
    return int(font.getbbox(text)[2])


def load_font(size: int):
    from PIL import ImageFont

    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/arialbd.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def write_raw_png(path: Path, width: int = 1200, height: int = 630) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            pulse = 36 if (x // 40 + y // 28) % 2 == 0 else 0
            blue = 98 + (x * 72 // max(width - 1, 1)) + pulse // 2
            green = 74 + (y * 96 // max(height - 1, 1)) + pulse
            red = 18 + ((x + y) * 24 // max(width + height - 2, 1))
            row.extend((min(red, 255), min(green, 255), min(blue, 255)))
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
