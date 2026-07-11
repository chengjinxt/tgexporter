from pathlib import Path

from tgexporter.placeholder_image import write_placeholder_png


def test_write_placeholder_png_creates_png(tmp_path: Path):
    path = tmp_path / "cover.png"

    write_placeholder_png(path, width=16, height=9)

    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
