from __future__ import annotations

from datetime import date

from tgexporter.tech_digest import TechCandidate
from tgexporter.tech_digest_media import TechDigestMediaCollector


def tech_candidate(*, images=(), videos=()):
    return TechCandidate(
        source_key="verge",
        source_name="The Verge",
        title="A new chip launches",
        url="https://theverge.com/story",
        ranking_method="most_popular",
        ranking_position=1,
        image_urls=tuple(images),
        video_urls=tuple(videos),
    )


class RecordingDownloader:
    def __init__(self, failed_urls=()):
        self.failed_urls = set(failed_urls)
        self.urls = []

    def __call__(self, url, destination, referer, max_bytes):
        self.urls.append(url)
        if url in self.failed_urls:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"downloaded-media")
        return True


def test_media_collector_prefers_original_video_and_image_before_search(tmp_path):
    downloader = RecordingDownloader()
    search_queries = []
    collector = TechDigestMediaCollector(
        output_dir=tmp_path,
        downloader=downloader,
        image_search=lambda query: search_queries.append(query) or [],
        image_validator=lambda path: True,
    )
    item = tech_candidate(
        images=("https://cdn.example.test/hero.jpg",),
        videos=("https://cdn.example.test/report.mp4",),
    )

    media = collector.collect(item, date(2026, 9, 26), index=1, title="新芯片发布")

    assert [asset.kind for asset in media] == ["image", "video"]
    assert [asset.source for asset in media] == ["original_article_image", "original_article_video"]
    assert downloader.urls == [
        "https://cdn.example.test/report.mp4",
        "https://cdn.example.test/hero.jpg",
    ]
    assert search_queries == []
    assert all(asset.path.exists() for asset in media)


def test_media_collector_uses_google_result_when_original_media_is_missing(tmp_path):
    downloader = RecordingDownloader()
    collector = TechDigestMediaCollector(
        output_dir=tmp_path,
        downloader=downloader,
        image_search=lambda query: ["https://images.example.test/search-result.jpg"],
        image_validator=lambda path: True,
    )

    media = collector.collect(tech_candidate(), date(2026, 9, 26), index=2, title="新芯片发布")

    assert len(media) == 1
    assert media[0].kind == "image"
    assert media[0].source == "google_image_search"
    assert downloader.urls == ["https://images.example.test/search-result.jpg"]


def test_media_collector_uses_original_video_frame_before_google_search(tmp_path):
    search_queries = []

    def create_cover(video_path, image_path, title):
        image_path.write_bytes(b"video-cover")
        return True

    collector = TechDigestMediaCollector(
        output_dir=tmp_path,
        downloader=RecordingDownloader(),
        image_search=lambda query: search_queries.append(query) or [],
        image_validator=lambda path: True,
        video_cover_creator=create_cover,
    )
    item = tech_candidate(videos=("https://cdn.example.test/report.mp4",))

    media = collector.collect(item, date(2026, 9, 26), index=3, title="新芯片发布")

    assert [asset.kind for asset in media] == ["image", "video"]
    assert media[0].source == "original_video_cover"
    assert search_queries == []


def test_media_collector_generates_a_local_cover_when_search_also_fails(tmp_path):
    collector = TechDigestMediaCollector(
        output_dir=tmp_path,
        downloader=RecordingDownloader(),
        image_search=lambda query: [],
        image_validator=lambda path: True,
    )

    media = collector.collect(tech_candidate(), date(2026, 9, 26), index=3, title="新芯片发布")

    assert len(media) == 1
    assert media[0].kind == "image"
    assert media[0].source == "generated_placeholder"
    assert media[0].path.exists()
