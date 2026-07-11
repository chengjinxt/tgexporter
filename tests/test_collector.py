from pathlib import Path

from tgexporter.collector import TelegramCollector, choose_video_variant, collect_entity_urls
from tgexporter.markdown_renderer import MarkdownRenderer
from tgexporter.state import StateStore


class FakeBotClient:
    def get_updates(self, offset=None, timeout=30, allowed_updates=None):
        return []

    def get_file(self, file_id):
        return {"file_id": file_id, "file_path": f"photos/{file_id}.jpg"}

    def download_file(self, file_path, destination):
        Path(destination).write_bytes(b"image")


def test_collector_renders_channel_photo_post(tmp_path: Path):
    state = StateStore(tmp_path / "state.sqlite")
    renderer = MarkdownRenderer(tmp_path / "发布内容")
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=state,
        renderer=renderer,
        channel="TechnologyNewsSyncAssistant",
    )
    updates = [
        {
            "update_id": 100,
            "channel_post": {
                "message_id": 7,
                "date": 1783735200,
                "chat": {"id": -1001, "username": "TechnologyNewsSyncAssistant"},
                "caption": "OpenClaw 原生移动端上线 iOS 与 Android\n正文内容",
                "photo": [{"file_id": "small", "file_size": 1}, {"file_id": "big", "file_size": 20}],
            },
        }
    ]

    paths = collector.process_updates(updates)

    assert len(paths) == 1
    assert paths[0].name == "20260711_001_OpenClaw 原生移动端上线 iOS 与 Android.md"
    assert (paths[0].parent / "20260711_001_PIC_001_OpenClaw 原生移动端上线 iOS 与 Android.jpg").exists()
    assert state.message_processed("technologynewssyncassistant", 7)


def test_collector_groups_media_album(tmp_path: Path):
    state = StateStore(tmp_path / "state.sqlite")
    renderer = MarkdownRenderer(tmp_path / "发布内容")
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=state,
        renderer=renderer,
        channel="TechnologyNewsSyncAssistant",
    )
    updates = [
        {
            "update_id": 101,
            "channel_post": {
                "message_id": 8,
                "date": 1783735200,
                "media_group_id": "album-1",
                "chat": {"id": -1001, "username": "TechnologyNewsSyncAssistant"},
                "caption": "相册文章",
                "photo": [{"file_id": "a", "file_size": 1}],
            },
        },
        {
            "update_id": 102,
            "channel_post": {
                "message_id": 9,
                "date": 1783735201,
                "media_group_id": "album-1",
                "chat": {"id": -1001, "username": "TechnologyNewsSyncAssistant"},
                "photo": [{"file_id": "b", "file_size": 1}],
            },
        },
    ]

    paths = collector.process_updates(updates)

    assert len(paths) == 1
    assert "相册文章" in paths[0].read_text(encoding="utf-8")
    assert (paths[0].parent / "20260711_001_PIC_001_相册文章.jpg").exists()
    assert (paths[0].parent / "20260711_001_PIC_002_相册文章.jpg").exists()


def test_choose_video_variant_prefers_downloadable_h264():
    video = {
        "file_id": "original",
        "file_size": 122_000_000,
        "width": 3840,
        "height": 2160,
        "qualities": [
            {"file_id": "av1-720", "file_size": 6_000_000, "width": 1280, "height": 720, "codec": "av01"},
            {"file_id": "h264-480", "file_size": 4_000_000, "width": 852, "height": 480, "codec": "h264"},
            {"file_id": "h264-1080", "file_size": 18_000_000, "width": 1920, "height": 1080, "codec": "h264"},
            {"file_id": "h264-too-big", "file_size": 24_000_000, "width": 1920, "height": 1080, "codec": "h264"},
        ],
    }

    assert choose_video_variant(video)["file_id"] == "h264-1080"


def test_collect_entity_urls_reads_hidden_text_links():
    messages = [
        {
            "entities": [{"type": "text_link", "url": "https://example.com/a"}],
            "caption_entities": [{"type": "text_link", "url": "https://news.example.com/b"}],
        }
    ]

    assert collect_entity_urls(messages) == ["https://example.com/a", "https://news.example.com/b"]
