from pathlib import Path
import struct
import zlib

from tgexporter.image_filter import is_suitable_article_image, read_image_size
from tgexporter.placeholder_image import write_placeholder_png


def test_read_image_size_reads_png_dimensions(tmp_path: Path):
    path = tmp_path / "cover.png"
    write_placeholder_png(path, width=1200, height=630)

    assert read_image_size(path) == (1200, 630)


def test_is_suitable_article_image_accepts_landscape_cover(tmp_path: Path):
    path = tmp_path / "cover.png"
    write_checkerboard_png(path, width=1200, height=630)

    assert is_suitable_article_image(path)


def test_is_suitable_article_image_rejects_smooth_gradient(tmp_path: Path):
    path = tmp_path / "gradient.png"
    write_smooth_gradient_png(path, width=1200, height=630)

    assert not is_suitable_article_image(path)


def test_is_suitable_article_image_rejects_small_square_logo(tmp_path: Path):
    path = tmp_path / "logo.png"
    write_placeholder_png(path, width=400, height=400)

    assert not is_suitable_article_image(path)


def test_is_suitable_article_image_rejects_unreadable_fake_jpg(tmp_path: Path):
    path = tmp_path / "fake.jpg"
    path.write_bytes(b"not-a-real-image" * 5000)

    assert not is_suitable_article_image(path)


def test_is_suitable_article_image_accepts_dark_photo_with_color_detail(tmp_path: Path):
    path = tmp_path / "dark-photo.png"
    write_dark_photo_like_png(path, width=960, height=640)

    assert is_suitable_article_image(path)


def test_is_suitable_article_image_rejects_extreme_aspect_ratio_banner(tmp_path: Path):
    # 类似黑猫投诉880x180横幅广告，长宽比4.88
    path = tmp_path / "banner-ad.png"
    write_checkerboard_png(path, width=880, height=180)

    assert not is_suitable_article_image(path)


def test_is_suitable_article_image_accepts_square_company_logo_when_allowed(tmp_path: Path):
    # 400x400的公司Logo，默认会被拒绝，但在allow_logo=True或is_suitable_logo_image下可被采纳
    path = tmp_path / "company-logo.png"
    write_checkerboard_png(path, width=400, height=400)

    assert not is_suitable_article_image(path)
    assert is_suitable_article_image(path, allow_logo=True)


def write_checkerboard_png(path: Path, width: int, height: int) -> None:
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            bright = 235 if ((x // 24) + (y // 24)) % 2 else 20
            row.extend((bright, 60, 255 - bright))
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(raw, 9))
        + png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def write_smooth_gradient_png(path: Path, width: int, height: int) -> None:
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            blue = 120 + (x * 80 // max(width - 1, 1))
            green = 90 + (y * 90 // max(height - 1, 1))
            row.extend((22, green, blue))
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(raw, 9))
        + png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def write_dark_photo_like_png(path: Path, width: int, height: int) -> None:
    rows = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            field = 32 + ((x * 5 + y * 3) % 28)
            if width // 2 - 80 < x < width // 2 + 80 and height // 2 - 120 < y < height // 2 + 120:
                row.extend((170 + (x % 35), 28 + (y % 18), 32 + ((x + y) % 24)))
            elif x == y or x == width - y:
                row.extend((220, 220, 220))
            else:
                row.extend((field // 2, field + 10, field // 3))
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(raw, 9))
        + png_chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
