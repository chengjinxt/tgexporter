from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from .filename import media_filename
from .image_filter import is_suitable_article_image
from .image_search import find_bing_image_urls, find_google_image_urls, find_google_image_urls_via_browser
from .link_enricher import build_opener
from .models import MediaAsset
from .placeholder_image import write_placeholder_png
from .tech_digest import TechCandidate
from .video_cover import create_video_cover

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 49 * 1024 * 1024
IMAGE_ACCEPT_HEADER = "image/jpeg,image/png,image/webp;q=0.8,*/*;q=0.5"
VIDEO_ACCEPT_HEADER = "video/*,application/octet-stream;q=0.8,*/*;q=0.5"


class TechDigestMediaCollector:
    def __init__(
        self,
        *,
        output_dir: Path,
        proxy_url: str | None = None,
        profile_dir: Path | None = None,
        downloader: Callable[[str, Path, str | None, int], bool] | None = None,
        image_search: Callable[[str], Iterable[str]] | None = None,
        image_validator: Callable[[Path], bool] | None = None,
        video_cover_creator: Callable[[Path, Path, str], bool] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.proxy_url = proxy_url
        self.profile_dir = profile_dir
        self.downloader = downloader or self._download
        self.image_search = image_search or self._search_images
        self.image_validator = image_validator or is_suitable_article_image
        self.video_cover_creator = video_cover_creator or create_video_cover

    def collect(
        self,
        candidate: TechCandidate,
        run_date: date,
        index: int,
        title: str,
    ) -> tuple[MediaAsset, ...]:
        date_key = run_date.strftime("%Y%m%d")
        date_dir = self.output_dir / date_key
        date_dir.mkdir(parents=True, exist_ok=True)

        video = self._download_first_video(candidate, date_dir, date_key, index, title)
        image = self._download_first_image(
            candidate.image_urls,
            candidate,
            date_dir,
            date_key,
            index,
            title,
            source="original_article_image",
        )
        if image is None and video is not None:
            filename = media_filename(date_key, index, "PIC", 1, title, ".jpg")
            path = date_dir / filename
            if self.video_cover_creator(video.path, path, title):
                image = MediaAsset(
                    kind="image",
                    filename=filename,
                    path=path,
                    source="original_video_cover",
                    title=f"视频封面：{title}",
                )
        if image is None:
            image = self._download_first_image(
                self.image_search(f"{candidate.title} {candidate.source_name}"),
                candidate,
                date_dir,
                date_key,
                index,
                title,
                source="google_image_search",
                referer=None,
            )
        if image is None:
            filename = media_filename(date_key, index, "PIC", 1, title, ".png")
            path = date_dir / filename
            write_placeholder_png(path, title=title)
            image = MediaAsset(
                kind="image",
                filename=filename,
                path=path,
                source="generated_placeholder",
                title=title,
            )

        media = [image]
        if video is not None:
            media.append(video)
        return tuple(media)

    def _download_first_video(
        self,
        candidate: TechCandidate,
        date_dir: Path,
        date_key: str,
        index: int,
        title: str,
    ) -> MediaAsset | None:
        for url in candidate.video_urls:
            extension = _video_extension(url)
            if extension is None:
                continue
            filename = media_filename(date_key, index, "VID", 1, title, extension)
            path = date_dir / filename
            if not self.downloader(url, path, candidate.url, MAX_VIDEO_BYTES):
                path.unlink(missing_ok=True)
                continue
            return MediaAsset(
                kind="video",
                filename=filename,
                path=path,
                source="original_article_video",
                title=title,
            )
        return None

    def _download_first_image(
        self,
        urls: Iterable[str],
        candidate: TechCandidate,
        date_dir: Path,
        date_key: str,
        index: int,
        title: str,
        *,
        source: str,
        referer: str | None = "source",
    ) -> MediaAsset | None:
        for url in urls:
            extension = _image_extension(url)
            filename = media_filename(date_key, index, "PIC", 1, title, extension)
            path = date_dir / filename
            actual_referer = candidate.url if referer == "source" else referer
            if not self.downloader(url, path, actual_referer, MAX_IMAGE_BYTES):
                path.unlink(missing_ok=True)
                continue
            normalized_path = normalize_downloaded_image(path)
            if normalized_path is None:
                path.unlink(missing_ok=True)
                continue
            path = normalized_path
            filename = path.name
            if not self.image_validator(path):
                path.unlink(missing_ok=True)
                continue
            return MediaAsset(
                kind="image",
                filename=filename,
                path=path,
                source=source,
                title=title,
            )
        return None

    def _search_images(self, query: str) -> list[str]:
        urls = find_google_image_urls(query, proxy_url=self.proxy_url)
        if not urls:
            urls = find_google_image_urls_via_browser(query, profile_dir=self.profile_dir)
        if not urls:
            urls = find_bing_image_urls(query)
        return urls

    def _download(self, url: str, destination: Path, referer: str | None, max_bytes: int) -> bool:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            # Prefer formats that Telegram and WeChat accept directly. Some CDNs return WebP
            # even when the URL ends in .jpg, so downloaded files are normalized below too.
            "Accept": VIDEO_ACCEPT_HEADER if _video_extension(url) else IMAGE_ACCEPT_HEADER,
        }
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, headers=headers)
        try:
            with build_opener(self.proxy_url).open(request, timeout=30) as response:
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length > max_bytes:
                    return False
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("wb") as output:
                    copied = _copy_limited(response, output, max_bytes)
                if not copied:
                    destination.unlink(missing_ok=True)
                return copied
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            destination.unlink(missing_ok=True)
            return False


def normalize_downloaded_image(path: Path) -> Path | None:
    """Convert WebP/AVIF responses to a real JPEG with a matching extension."""
    actual_format = detect_downloaded_image_format(path)
    if actual_format not in {"WEBP", "AVIF"}:
        return path

    try:
        from PIL import Image, ImageOps

        target = path.with_suffix(".jpg")
        temporary = target.with_name(f".{target.stem}.normalized.jpg")
        with Image.open(path) as source:
            normalized = ImageOps.exif_transpose(source)
            if normalized.mode in {"RGBA", "LA"} or "transparency" in normalized.info:
                rgba = normalized.convert("RGBA")
                image = Image.new("RGB", rgba.size, "white")
                image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image = normalized.convert("RGB")
            image.save(temporary, format="JPEG", quality=92, optimize=True, progressive=True)
        temporary.replace(target)
        if target != path:
            path.unlink(missing_ok=True)
        return target
    except Exception as exc:
        temporary_path = locals().get("temporary")
        if isinstance(temporary_path, Path):
            temporary_path.unlink(missing_ok=True)
        print(f"Downloaded {actual_format} image normalization failed for {path}: {exc}", flush=True)
        return None


def detect_downloaded_image_format(path: Path) -> str | None:
    try:
        with path.open("rb") as file:
            header = file.read(32)
    except OSError:
        return None
    if header.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "WEBP"
    if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"}:
        return "AVIF"
    return None


def _copy_limited(source, destination, max_bytes: int) -> bool:
    total = 0
    while True:
        chunk = source.read(min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            return total > 0
        total += len(chunk)
        if total > max_bytes:
            return False
        destination.write(chunk)


def _image_extension(url: str) -> str:
    extension = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return extension if extension in {".jpg", ".jpeg", ".png", ".gif", ".webp"} else ".jpg"


def _video_extension(url: str) -> str | None:
    extension = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return extension if extension in {".mp4", ".webm", ".mov", ".m4v"} else None
