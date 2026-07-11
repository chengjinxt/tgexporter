from __future__ import annotations

import struct
import zlib
from pathlib import Path


def write_placeholder_png(path: Path, width: int = 1200, height: int = 630) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

