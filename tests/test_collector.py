from pathlib import Path

from tgexporter.collector import (
    TelegramCollector,
    choose_video_variant,
    clean_article_text,
    collect_entity_links,
    collect_entity_urls,
    is_transient_telegram_error,
)
from tgexporter.markdown_renderer import MarkdownRenderer
from tgexporter.state import StateStore
from tgexporter.telegram_bot import TelegramBotError


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
            "text": "来源 Example",
            "caption": "新闻 News",
            "entities": [{"type": "text_link", "offset": 3, "length": 7, "url": "https://example.com/a"}],
            "caption_entities": [{"type": "text_link", "offset": 3, "length": 4, "url": "https://news.example.com/b"}],
        }
    ]

    assert collect_entity_urls(messages) == ["https://example.com/a", "https://news.example.com/b"]
    assert [item.name for item in collect_entity_links(messages)] == ["Example", "News"]


def test_clean_article_text_removes_title_reference_and_channel_promo():
    text = """测试标题

正文第一段

BleepingComputer

🌸 在花频道 · 茶馆水群 · 投稿通道

📢 频道 👥 群组 📝 投稿
"""

    assert clean_article_text(text, "测试标题", ["BleepingComputer"]) == "正文第一段"


def test_transient_telegram_error_detects_ssl_timeout():
    error = TelegramBotError(
        "Telegram API getUpdates failed: <urlopen error _ssl.c:983: The handshake operation timed out>"
    )

    assert is_transient_telegram_error(error)


def test_transient_telegram_error_does_not_retry_invalid_token():
    error = TelegramBotError("Telegram API getUpdates failed: Unauthorized: invalid token")

    assert not is_transient_telegram_error(error)
