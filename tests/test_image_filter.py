from pathlib import Path

from tgexporter.image_filter import is_suitable_article_image, read_image_size
from tgexporter.placeholder_image import write_placeholder_png


def test_read_image_size_reads_png_dimensions(tmp_path: Path):
    path = tmp_path / "cover.png"
    write_placeholder_png(path, width=1200, height=630)

    assert read_image_size(path) == (1200, 630)


def test_is_suitable_article_image_accepts_landscape_cover(tmp_path: Path):
    path = tmp_path / "cover.png"
    write_placeholder_png(path, width=1200, height=630)

    assert is_suitable_article_image(path)


def test_is_suitable_article_image_rejects_small_square_logo(tmp_path: Path):
    path = tmp_path / "logo.png"
    write_placeholder_png(path, width=400, height=400)

    assert not is_suitable_article_image(path)
