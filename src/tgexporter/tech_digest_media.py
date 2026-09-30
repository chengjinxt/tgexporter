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
            "Accept": "image/avif,image/webp,image/png,image/jpeg,video/*;q=0.9,*/*;q=0.5",
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
