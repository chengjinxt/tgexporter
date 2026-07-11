from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

BRAND_TEXT = "firemail 科技频道"


def create_video_cover(
    video_path: Path,
    output_path: Path,
    title: str,
    brand: str = BRAND_TEXT,
) -> bool:
    frame = select_key_frame(video_path)
    if frame is None:
        return False
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    image = draw_cover_text(image, title=title, brand=brand)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="JPEG", quality=92, optimize=True)
    return True


def select_key_frame(video_path: Path) -> np.ndarray | None:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    try:
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        if frame_count <= 0:
            return read_frame_at(cap, 0)
        candidate_indexes = candidate_frame_indexes(frame_count, fps)
        best_frame = None
        best_score = -math.inf
        for index in candidate_indexes:
            frame = read_frame_at(cap, index)
            if frame is None:
                continue
            score = frame_score(frame)
            if score > best_score:
                best_score = score
                best_frame = frame
        return best_frame
    finally:
        cap.release()


def candidate_frame_indexes(frame_count: int, fps: float) -> list[int]:
    indexes = {0}
    if fps > 0:
        for seconds in (1.0, 2.5, 5.0):
            indexes.add(min(frame_count - 1, int(seconds * fps)))
    for ratio in (0.08, 0.18, 0.32, 0.5, 0.68, 0.82):
        indexes.add(min(frame_count - 1, max(0, int(frame_count * ratio))))
    return sorted(indexes)


def read_frame_at(cap: cv2.VideoCapture, index: int) -> np.ndarray | None:
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    return frame


def frame_score(frame: np.ndarray) -> float:
    resized = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
    brightness = float(gray.mean())
    brightness_balance = 100.0 - abs(brightness - 120.0)
    channels = cv2.split(resized.astype("float"))
    rg = np.absolute(channels[2] - channels[1])
    yb = np.absolute(0.5 * (channels[2] + channels[1]) - channels[0])
    colorfulness = float(np.sqrt(rg.var() + yb.var()) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))
    return sharpness * 0.5 + colorfulness * 1.8 + brightness_balance


def draw_cover_text(image: Image.Image, title: str, brand: str = BRAND_TEXT) -> Image.Image:
    image = image.convert("RGB")
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if width >= height:
        draw.rectangle((0, int(height * 0.48), width, height), fill=(0, 0, 0, 150))
    else:
        draw.rectangle((0, 0, width, height), fill=(0, 0, 0, 95))
        draw.rectangle((0, int(height * 0.56), width, height), fill=(0, 0, 0, 135))
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)

    margin = max(32, int(min(width, height) * 0.045))
    brand_font = load_font(max(30, int(min(width, height) * 0.055)))
    title_font = load_font(max(48, int(min(width, height) * 0.09)))
    footer_font = load_font(max(26, int(min(width, height) * 0.043)))

    draw.text(
        (margin, margin),
        brand,
        font=brand_font,
        fill=(255, 255, 255),
        stroke_width=max(2, width // 500),
        stroke_fill=(0, 0, 0),
    )

    wrapped = wrap_text(title, title_font, max_width=width - margin * 2)
    title_y = int(height * (0.55 if width >= height else 0.58))
    for line in wrapped[:3]:
        draw.text(
            (margin, title_y),
            line,
            font=title_font,
            fill=(255, 224, 40),
            stroke_width=max(3, width // 360),
            stroke_fill=(0, 0, 0),
        )
        title_y += int(title_font.size * 1.16)

    draw.text(
        (margin, height - margin - footer_font.size),
        "Telegram 资讯同步",
        font=footer_font,
        fill=(255, 255, 255),
        stroke_width=max(2, width // 600),
        stroke_fill=(0, 0, 0),
    )
    return image


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
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


def text_width(text: str, font: ImageFont.FreeTypeFont) -> int:
    return int(font.getbbox(text)[2])


def load_font(size: int) -> ImageFont.FreeTypeFont:
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
