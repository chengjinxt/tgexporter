from __future__ import annotations

import argparse
import json
import tempfile
import urllib.request
import zipfile
from pathlib import Path


MODEL_PACKAGES = {
    ("en", "zh"): (
        "translate-en_zh-1_9",
        "https://argos-net.com/v1/translate-en_zh-1_9.argosmodel",
    ),
    ("zh", "en"): (
        "translate-zh_en-1_9",
        "https://argos-net.com/v1/translate-zh_en-1_9.argosmodel",
    ),
}
REQUIRED_PAIRS = frozenset(MODEL_PACKAGES)


def prepare_models(model_dir: Path) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    missing = {
        pair
        for pair, (directory, _) in MODEL_PACKAGES.items()
        if not model_is_ready(model_dir / directory, pair)
    }
    if not missing:
        print(f"Local translation models are ready: {model_dir}")
        return

    for pair in sorted(missing):
        _, url = MODEL_PACKAGES[pair]
        print(f"Downloading local translation model {pair[0]}->{pair[1]}...")
        with tempfile.NamedTemporaryFile(suffix=".argosmodel", delete=False) as temporary:
            archive_path = Path(temporary.name)
        try:
            urllib.request.urlretrieve(url, archive_path)
            extract_model_archive(archive_path, model_dir)
        finally:
            archive_path.unlink(missing_ok=True)

    remaining = {
        pair
        for pair, (directory, _) in MODEL_PACKAGES.items()
        if not model_is_ready(model_dir / directory, pair)
    }
    if remaining:
        pairs = ", ".join(f"{source}->{target}" for source, target in sorted(remaining))
        raise SystemExit(f"Local translation model preparation is incomplete: {pairs}")
    print(f"Local translation models are ready: {model_dir}")


def model_is_ready(package_dir: Path, pair: tuple[str, str]) -> bool:
    if not package_dir.is_dir():
        return False
    metadata_path = package_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        (metadata.get("from_code"), metadata.get("to_code")) == pair
        and (package_dir / "model" / "config.json").is_file()
        and (package_dir / "model" / "model.bin").is_file()
        and (package_dir / "sentencepiece.model").is_file()
    )


def extract_model_archive(archive_path: Path, model_dir: Path) -> None:
    destination = model_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if destination != target and destination not in target.parents:
                raise SystemExit(f"Unsafe path in translation model archive: {member.filename}")
        archive.extractall(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare bundled English/Chinese Argos models.")
    parser.add_argument("--output", type=Path, required=True, help="Model directory copied into the portable build.")
    args = parser.parse_args()
    prepare_models(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
