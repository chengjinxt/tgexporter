from __future__ import annotations

import tomllib
from pathlib import Path

from tgexporter.cli import build_parser
from tgexporter.image_support import verify_webp_support


ROOT = Path(__file__).resolve().parents[1]


def test_webp_codec_self_check_round_trips_an_image():
    result = verify_webp_support()

    assert result == {"pillow_webp": True, "decoded_size": [2, 2]}


def test_image_format_check_command_is_registered():
    args = build_parser().parse_args(["image-format-check"])

    assert args.command == "image-format-check"


def test_packaging_explicitly_includes_pillow_webp_support():
    spec = (ROOT / "tgexporter.spec").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_script = (ROOT / "scripts" / "build_exe.ps1").read_text(encoding="utf-8")
    dependencies = project["project"]["dependencies"]

    assert 'collect_all("PIL")' in spec
    assert '"PIL.WebPImagePlugin"' in spec
    assert any(dependency.startswith("Pillow>=") for dependency in dependencies)
    assert any(dependency.startswith("numpy>=") for dependency in dependencies)
    assert any(dependency.startswith("opencv-python-headless>=") for dependency in dependencies)
    assert "& $Exe image-format-check" in build_script
