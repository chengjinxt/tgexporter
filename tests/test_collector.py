from pathlib import Path
from datetime import UTC, datetime

import pytest

from tgexporter import collector as collector_module
from tgexporter.collector import (
    TelegramCollector,
    apply_plain_wechat_markdown_links,
    choose_video_variant,
    clean_article_text,
    collect_entity_links,
    collect_entity_urls,
    collect_text,
    enrich_wechat_article_links,
    find_existing_markdown_by_title,
    is_transient_telegram_error,
    markdown_title,
    poll_timeout_after_transient_error,
    should_prompt_source_login,
    telegram_network_hint,
)
from tgexporter.markdown_renderer import MarkdownRenderer
from tgexporter.link_enricher import PageMetadata
from tgexporter.models import ArticleDraft, LinkRef
from tgexporter.software_share import GitHubRepoInfo, build_software_share, find_software_intro_title
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


def test_collector_skips_duplicate_title_already_on_disk(tmp_path: Path):
    state = StateStore(tmp_path / "state.sqlite")
    output_dir = tmp_path / "发布内容"
    date_dir = output_dir / "20260711"
    date_dir.mkdir(parents=True)
    existing = date_dir / "20260711_013_大疆 EV50 飞越珠峰 8861 米.md"
    existing.write_text(
        """---
title: "大疆 EV50 飞越珠峰 8861 米"
---

# 大疆 EV50 飞越珠峰 8861 米

已有正文
""",
        encoding="utf-8",
    )
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=state,
        renderer=MarkdownRenderer(output_dir),
        channel="TechnologyNewsSyncAssistant",
    )
    updates = [
        {
            "update_id": 110,
            "channel_post": {
                "message_id": 20,
                "date": 1783735200,
                "chat": {"id": -1001, "username": "TechnologyNewsSyncAssistant"},
                "caption": "大疆 EV50 飞越珠峰 8861 米\n重复正文",
            },
        }
    ]

    paths = collector.process_updates(updates)

    assert paths == []
    assert state.message_processed("technologynewssyncassistant", 20)
    assert sorted(path.name for path in date_dir.glob("*.md")) == [existing.name]


def test_find_existing_markdown_by_title_searches_batch_subdirs(tmp_path: Path):
    batch = tmp_path / "20260719" / "第1批"
    batch.mkdir(parents=True)
    article = batch / "old.md"
    article.write_text("# 已发布文章\n\n正文", encoding="utf-8")

    assert find_existing_markdown_by_title(tmp_path / "20260719", "已发布文章") == article
    assert markdown_title(article) == "已发布文章"


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


def test_collect_text_keeps_wechat_hidden_link_as_markdown_link():
    messages = [
        {
            "text": "据 长安街知事 报道",
            "entities": [
                {
                    "type": "text_link",
                    "offset": 2,
                    "length": 5,
                    "url": "https://mp.weixin.qq.com/s/Wp0PdV83btg8skL6ypfXHw",
                }
            ],
        }
    ]

    assert collect_text(messages) == "据 [长安街知事](https://mp.weixin.qq.com/s/Wp0PdV83btg8skL6ypfXHw) 报道"


def test_apply_plain_wechat_link_line_as_markdown_link():
    text = "正文\n\nTech 星球 (https://mp.weixin.qq.com/s/mdg66FvdwwRFsg20HHnr4g)"

    assert apply_plain_wechat_markdown_links(text).endswith(
        "[Tech 星球](https://mp.weixin.qq.com/s/mdg66FvdwwRFsg20HHnr4g)"
    )


def test_enrich_wechat_article_links_fetches_mmbiz_images(monkeypatch):
    def fake_fetch(url, proxy_url=None):
        return PageMetadata(
            title="微信文章标题",
            image_url="https://mmbiz.qpic.cn/mmbiz_jpg/example/0?wx_fmt=jpeg",
            image_urls=("https://mmbiz.qpic.cn/mmbiz_jpg/example/0?wx_fmt=jpeg",),
        )

    monkeypatch.setattr(collector_module, "fetch_page_metadata", fake_fetch)

    links = enrich_wechat_article_links("[Tech 星球](https://mp.weixin.qq.com/s/mdg66FvdwwRFsg20HHnr4g)")

    assert len(links) == 1
    assert links[0].name == "Tech 星球"
    assert links[0].image_url == "https://mmbiz.qpic.cn/mmbiz_jpg/example/0?wx_fmt=jpeg"


def test_clean_article_text_removes_title_reference_and_channel_promo():
    text = """测试标题

正文第一段

BleepingComputer

🌸 在花频道 · 茶馆水群 · 投稿通道

📢 频道 👥 群组 📝 投稿
"""

    assert clean_article_text(text, "测试标题", ["BleepingComputer"]) == "正文第一段"


def test_software_share_uses_product_title_and_filters_promo(monkeypatch):
    text = """#在线工具 #前端

🏷 Sticker Forge - 把文字和图片变成可撕开的 3D 贴纸

🌐 在线体验

Sticker Forge 可以把文字或上传的图片转换成带有真实撕裂效果的 3D 贴纸。

📮投稿    📢频道    💬吹水    🌐网站

Sticker Forge - 把文字和图片变成可撕开的 3D 贴纸

https://github.com/CatsJuice/sticker-forge

在线体验

https://sticker.oooo.so/

网站

https://geekshare.org/
"""

    def fake_fetch(url, proxy_url=None):
        return GitHubRepoInfo(
            owner="CatsJuice",
            repo="sticker-forge",
            full_name="CatsJuice/sticker-forge",
            html_url="https://github.com/CatsJuice/sticker-forge",
            description="A tactile WebGL sticker maker.",
            homepage="https://sticker.oooo.so/",
            language="JavaScript",
            stars=123,
            readme=(
                "![Demo](docs/demo.png)\n"
                "- Rich text sticker editor\n"
                "- Image uploads\n"
                "- Interactive peel physics\n"
                "sticker-forge.es.js"
            ),
            default_branch="dev",
        )

    monkeypatch.setattr(collector_module, "build_software_share", build_software_share)
    monkeypatch.setattr("tgexporter.software_share.fetch_github_repo_info", fake_fetch)

    share = build_software_share(text, [], proxy_url=None)

    assert share is not None
    assert share.title == "Sticker Forge - 把文字和图片变成可撕开的 3D 贴纸"
    assert "它能解决什么问题？" in share.text
    assert "富文本" in share.text
    assert "sticker-forge.es.js" not in share.text
    assert "投稿" not in share.text
    assert all("geekshare.org" not in link.url for link in share.links)
    assert share.image_urls == ["https://raw.githubusercontent.com/CatsJuice/sticker-forge/dev/docs/demo.png"]
    assert share.demo_urls == ["https://sticker.oooo.so/"]


def test_collector_poll_once_for_date_skips_old_updates(tmp_path: Path):
    old_ts = int(datetime(2026, 7, 28, 1, 0, tzinfo=UTC).timestamp())
    today_ts = int(datetime(2026, 7, 29, 1, 0, tzinfo=UTC).timestamp())

    class DatedBotClient(FakeBotClient):
        def get_updates(self, offset=None, timeout=30, allowed_updates=None):
            return [
                {
                    "update_id": 201,
                    "channel_post": {
                        "message_id": 21,
                        "date": old_ts,
                        "chat": {"id": -1001, "username": "TechnologyNewsSyncAssistant"},
                        "text": "旧消息不应生成",
                    },
                },
                {
                    "update_id": 202,
                    "channel_post": {
                        "message_id": 22,
                        "date": today_ts,
                        "chat": {"id": -1001, "username": "TechnologyNewsSyncAssistant"},
                        "text": "当天消息应该生成",
                    },
                },
            ]

    state = StateStore(tmp_path / "state.sqlite")
    renderer = MarkdownRenderer(tmp_path / "发布内容")
    collector = TelegramCollector(
        client=DatedBotClient(),
        state=state,
        renderer=renderer,
        channel="TechnologyNewsSyncAssistant",
    )

    paths = collector.poll_once_for_date("20260729", timeout=1)

    assert len(paths) == 1
    assert paths[0].parent.name == "20260729"
    assert not (tmp_path / "发布内容" / "20260728").exists()
    assert state.get_last_update_id() == 202


def test_software_share_intro_title_skips_hash_tags():
    assert find_software_intro_title("#运维 #建站 #SSH\n\n🖥 Navop - 数据库和 SSH 一体化桌面工作台") == (
        "Navop - 数据库和 SSH 一体化桌面工作台"
    )


def test_software_share_builds_title_from_repo_when_intro_has_only_function(monkeypatch):
    text = """#运维 #建站 #SSH

🖥 数据库、SSH、SFTP、端口转发、终端、远程桌面、监控与 AI 一体化的原生桌面工作台

https://github.com/feigeCode/navop
"""

    def fake_fetch(url, proxy_url=None):
        return GitHubRepoInfo(
            owner="feigeCode",
            repo="navop",
            full_name="feigeCode/navop",
            html_url="https://github.com/feigeCode/navop",
            description="Unified workspace for databases and servers.",
        )

    monkeypatch.setattr("tgexporter.software_share.fetch_github_repo_info", fake_fetch)

    share = build_software_share(text, [], proxy_url=None)

    assert share is not None
    assert share.title == "Navop - 数据库、SSH、SFTP、端口转发、终端、远程桌面、监控与 AI 一体化的原生桌面工作台"


def test_software_share_filters_ai_meta_and_uses_chinese_readme_blob(monkeypatch):
    text = """🧩 buildby - 检测桌面应用是用什么技术构建的

为你撰写了一篇非常适合在微信公众号发布的推文。文章排版结构清晰、语言生动活泼。

buildby 是一个开源的命令行工具，可以检测 macOS 和 Windows 上的桌面应用技术栈。

https://github.com/wavever/buildby/blob/master/README.zh-CN.md
"""

    def fake_fetch(url, proxy_url=None):
        return GitHubRepoInfo(
            owner="wavever",
            repo="buildby",
            full_name="wavever/buildby",
            html_url="https://github.com/wavever/buildby",
            description="Detect what desktop apps are built with.",
            language="TypeScript",
            stars=88,
            readme="English README should be replaced.",
        )

    def fake_blob(url, proxy_url=None):
        return """# buildby

- 支持检测 Electron、Tauri、Flutter、Qt、Win32 等桌面应用技术栈。
- 单应用查询时会展示开发者、签名状态和公证状态。

```bash
npm i -g @wavever/buildby
buildby --all
```
"""

    monkeypatch.setattr("tgexporter.software_share.fetch_github_repo_info", fake_fetch)
    monkeypatch.setattr("tgexporter.software_share.fetch_github_blob_text", fake_blob)

    share = build_software_share(text, [], proxy_url=None)

    assert share is not None
    assert share.title == "buildby - 检测桌面应用是用什么技术构建的"
    assert "为你撰写" not in share.text
    assert "Electron、Tauri、Flutter" in share.text
    assert "npm i -g @wavever/buildby" in share.text
    assert [link.url for link in share.links] == ["https://github.com/wavever/buildby"]


def test_download_link_images_uses_first_picture_index(tmp_path: Path, monkeypatch):
    def fake_download(url, destination, proxy_url=None, referer=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"image")
        return True

    monkeypatch.setattr(collector_module, "download_web_file", fake_download)
    monkeypatch.setattr(collector_module, "is_suitable_article_image", lambda path: True)
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=StateStore(tmp_path / "state.sqlite"),
        renderer=MarkdownRenderer(tmp_path / "发布内容"),
        channel="TechnologyNewsSyncAssistant",
    )
    article = ArticleDraft(
        source="telegram",
        channel="technologynewssyncassistant",
        message_ids=[1],
        grouped_id=None,
        published_at=datetime(2026, 7, 16, tzinfo=UTC),
        date_key="20260716",
        daily_index=7,
        title="链接配图文章",
        text="正文",
        links=[LinkRef(name="Example", url="https://example.com/a", image_url="https://example.com/cover.jpg")],
    )
    date_dir = tmp_path / "发布内容" / "20260716"

    assets = collector._download_link_images(article, date_dir)

    assert len(assets) == 1
    assert assets[0].filename == "20260716_007_PIC_001_链接配图文章.jpg"
    assert assets[0].path.exists()


def test_download_link_images_tries_next_candidate_when_first_is_unsuitable(tmp_path: Path, monkeypatch):
    referers: list[str | None] = []

    def fake_download(url, destination, proxy_url=None, referer=None):
        referers.append(referer)
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"good" if "article" in url else b"bad")
        return True

    monkeypatch.setattr(collector_module, "download_web_file", fake_download)
    monkeypatch.setattr(collector_module, "is_suitable_article_image", lambda path: Path(path).read_bytes() == b"good")
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=StateStore(tmp_path / "state.sqlite"),
        renderer=MarkdownRenderer(tmp_path / "发布内容"),
        channel="TechnologyNewsSyncAssistant",
    )
    article = ArticleDraft(
        source="telegram",
        channel="technologynewssyncassistant",
        message_ids=[1],
        grouped_id=None,
        published_at=datetime(2026, 7, 16, tzinfo=UTC),
        date_key="20260716",
        daily_index=8,
        title="链接多候选配图文章",
        text="正文",
        links=[
            LinkRef(
                name="QbitAI",
                url="https://www.qbitai.com/2026/07/447873.html",
                image_url="https://www.qbitai.com/logo.png",
                image_urls=("https://www.qbitai.com/logo.png", "https://i.qbitai.com/article.png"),
            )
        ],
    )
    date_dir = tmp_path / "发布内容" / "20260716"

    assets = collector._download_link_images(article, date_dir)

    assert len(assets) == 1
    assert assets[0].filename == "20260716_008_PIC_001_链接多候选配图文章.png"
    assert assets[0].path.read_bytes() == b"good"
    assert referers == [
        "https://www.qbitai.com/2026/07/447873.html",
        "https://www.qbitai.com/2026/07/447873.html",
    ]


def test_capture_link_image_uses_web_screenshot_when_link_images_fail(tmp_path: Path, monkeypatch):
    def fake_capture(url, destination, profile_dir=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"screenshot")
        return True

    monkeypatch.setattr(collector_module, "capture_source_image", fake_capture)
    monkeypatch.setattr(collector_module, "is_suitable_article_image", lambda path: True)
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=StateStore(tmp_path / "state.sqlite"),
        renderer=MarkdownRenderer(tmp_path / "发布内容"),
        channel="TechnologyNewsSyncAssistant",
    )
    article = ArticleDraft(
        source="telegram",
        channel="technologynewssyncassistant",
        message_ids=[1],
        grouped_id=None,
        published_at=datetime(2026, 7, 16, tzinfo=UTC),
        date_key="20260716",
        daily_index=9,
        title="正文截图配图文章",
        text="正文",
        links=[LinkRef(name="Sohu", url="https://www.sohu.com/a/1050184362_120988576")],
    )

    assets = collector._capture_link_image(article, tmp_path / "发布内容" / "20260716")

    assert len(assets) == 1
    assert assets[0].source == "web_capture"
    assert assets[0].filename == "20260716_009_PIC_001_正文截图配图文章.png"


def test_download_google_image_tries_source_enriched_queries(tmp_path: Path, monkeypatch):
    queries: list[str] = []

    def fake_find_google_image_urls(query, proxy_url=None):
        queries.append(query)
        if "Bloomberg" in query:
            return ["https://images.example.com/openai-modal.jpg"]
        return []

    def fake_download(url, destination, proxy_url=None, referer=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"google-image")
        return True

    monkeypatch.setattr(collector_module, "find_google_image_urls", fake_find_google_image_urls)
    monkeypatch.setattr(collector_module, "find_google_image_urls_via_browser", lambda query, profile_dir=None: [])
    monkeypatch.setattr(collector_module, "find_bing_image_urls", lambda query, required_terms=None: [])
    monkeypatch.setattr(collector_module, "download_web_file", fake_download)
    monkeypatch.setattr(collector_module, "is_suitable_article_image", lambda path: True)
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=StateStore(tmp_path / "state.sqlite"),
        renderer=MarkdownRenderer(tmp_path / "发布内容"),
        channel="TechnologyNewsSyncAssistant",
    )
    article = ArticleDraft(
        source="telegram",
        channel="technologynewssyncassistant",
        message_ids=[1],
        grouped_id=None,
        published_at=datetime(2026, 7, 29, tzinfo=UTC),
        date_key="20260729",
        daily_index=5,
        title="OpenAI 失控 AI 代理再入侵第二家公司客户账户",
        text="OpenAI 代理侵入 Hugging Face 后又被曝入侵 Modal 客户。",
        links=[
            LinkRef(
                name="Bloomberg",
                url="https://www.bloomberg.com/news/articles/2026-07-28/openai-rogue-agent-hacked-account-at-a-second-firm-reuters-says",
            )
        ],
    )

    assets = collector._download_google_image(article, tmp_path / "发布内容" / "20260729")

    assert len(assets) == 1
    assert assets[0].source == "google_image_search"
    assert assets[0].path.read_bytes() == b"google-image"
    assert queries[:2] == [
        "OpenAI 失控 AI 代理再入侵第二家公司客户账户",
        "OpenAI 失控 AI 代理再入侵第二家公司客户账户 Bloomberg",
    ]


def test_download_google_image_uses_browser_fallback(tmp_path: Path, monkeypatch):
    browser_queries: list[str] = []

    monkeypatch.setattr(collector_module, "find_google_image_urls", lambda query, proxy_url=None: [])

    def fake_browser_search(query, profile_dir=None):
        browser_queries.append(query)
        return ["https://encrypted-tbn0.gstatic.com/images?q=tbn:modal-openai"]

    def fake_download(url, destination, proxy_url=None, referer=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"browser-google-image")
        return True

    monkeypatch.setattr(collector_module, "find_google_image_urls_via_browser", fake_browser_search)
    monkeypatch.setattr(collector_module, "download_web_file", fake_download)
    monkeypatch.setattr(collector_module, "is_suitable_article_image", lambda path: True)
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=StateStore(tmp_path / "state.sqlite"),
        renderer=MarkdownRenderer(tmp_path / "发布内容"),
        channel="TechnologyNewsSyncAssistant",
    )
    article = ArticleDraft(
        source="telegram",
        channel="technologynewssyncassistant",
        message_ids=[1],
        grouped_id=None,
        published_at=datetime(2026, 7, 29, tzinfo=UTC),
        date_key="20260729",
        daily_index=5,
        title="OpenAI 失控 AI 代理再入侵第二家公司客户账户",
        text="OpenAI 代理侵入 Hugging Face 后又被曝入侵 Modal 客户。",
        links=[],
    )

    assets = collector._download_google_image(article, tmp_path / "发布内容" / "20260729")

    assert len(assets) == 1
    assert assets[0].path.read_bytes() == b"browser-google-image"
    assert browser_queries == ["OpenAI 失控 AI 代理再入侵第二家公司客户账户"]


def test_download_google_image_uses_bing_after_google_blocked(tmp_path: Path, monkeypatch):
    bing_calls: list[tuple[str, list[str] | None]] = []

    monkeypatch.setattr(collector_module, "find_google_image_urls", lambda query, proxy_url=None: [])
    monkeypatch.setattr(collector_module, "find_google_image_urls_via_browser", lambda query, profile_dir=None: [])

    def fake_bing(query, required_terms=None):
        bing_calls.append((query, required_terms))
        return ["https://news.example.com/openai-modal.jpg"]

    def fake_download(url, destination, proxy_url=None, referer=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"bing-image")
        return True

    monkeypatch.setattr(collector_module, "find_bing_image_urls", fake_bing)
    monkeypatch.setattr(collector_module, "download_web_file", fake_download)
    monkeypatch.setattr(collector_module, "is_suitable_article_image", lambda path: True)
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=StateStore(tmp_path / "state.sqlite"),
        renderer=MarkdownRenderer(tmp_path / "发布内容"),
        channel="TechnologyNewsSyncAssistant",
    )
    article = ArticleDraft(
        source="telegram",
        channel="technologynewssyncassistant",
        message_ids=[1],
        grouped_id=None,
        published_at=datetime(2026, 7, 29, tzinfo=UTC),
        date_key="20260729",
        daily_index=5,
        title="OpenAI 失控 AI 代理再入侵第二家公司客户账户",
        text="OpenAI 代理侵入 Hugging Face 后又被曝入侵 Modal 客户。",
        links=[LinkRef(name="Bloomberg", url="https://www.bloomberg.com/news/articles/openai-rogue-agent-hacked-account")],
    )

    assets = collector._download_google_image(article, tmp_path / "发布内容" / "20260729")

    assert len(assets) == 1
    assert assets[0].path.read_bytes() == b"bing-image"
    assert bing_calls[0][0] == "OpenAI 失控 AI 代理再入侵第二家公司客户账户"
    assert {"openai", "hugging", "modal", "bloomberg"} & set(bing_calls[0][1] or [])


def test_download_browser_link_images_uses_dynamic_source_image(tmp_path: Path, monkeypatch):
    captured: list[tuple[str, str | None]] = []

    def fake_extract(url, timeout_ms=30000, profile_dir=None, limit=8):
        if "theinformation.com" in url:
            return ["https://tii.imgix.net/production/articles/17521/duv.png?auto=compress&fit=crop&w=1200"]
        raise AssertionError(f"unexpected url: {url}")

    def fake_download(url, destination, proxy_url=None, referer=None):
        captured.append((url, referer))
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"article-image")
        return True

    monkeypatch.setattr(collector_module, "extract_source_image_urls", fake_extract)
    monkeypatch.setattr(collector_module, "download_web_file", fake_download)
    monkeypatch.setattr(collector_module, "is_suitable_article_image", lambda path: True)
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=StateStore(tmp_path / "state.sqlite"),
        renderer=MarkdownRenderer(tmp_path / "发布内容"),
        channel="TechnologyNewsSyncAssistant",
    )
    article = ArticleDraft(
        source="telegram",
        channel="technologynewssyncassistant",
        message_ids=[1],
        grouped_id=None,
        published_at=datetime(2026, 7, 20, tzinfo=UTC),
        date_key="20260720",
        daily_index=1,
        title="中国开始量产国产 DUV 光刻机 今年目标生产约 5 台",
        text="正文",
        links=[
            LinkRef(
                name="The Information",
                url="https://www.theinformation.com/articles/china-starts-mass-producing-homegrown-duv-chipmaking-tools-advance-local-chip-industry",
            )
        ],
    )

    assets = collector._download_browser_link_images(article, tmp_path / "发布内容" / "20260720")

    assert len(assets) == 1
    assert assets[0].source == "web_browser_image"
    assert assets[0].filename == "20260720_001_PIC_001_中国开始量产国产 DUV 光刻机 今年目标生产约 5 台.png"
    assert captured == [
        (
            "https://tii.imgix.net/production/articles/17521/duv.png?auto=compress&fit=crop&w=1200",
            "https://www.theinformation.com/articles/china-starts-mass-producing-homegrown-duv-chipmaking-tools-advance-local-chip-industry",
        )
    ]


def test_download_browser_link_images_includes_reuters_domain(tmp_path: Path, monkeypatch):
    def fake_extract(url, timeout_ms=30000, profile_dir=None, limit=8):
        assert "reuters.com" in url
        return ["https://www.reuters.com/resizer/example/chipmaking.jpg"]

    def fake_download(url, destination, proxy_url=None, referer=None):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"reuters-image")
        return True

    monkeypatch.setattr(collector_module, "extract_source_image_urls", fake_extract)
    monkeypatch.setattr(collector_module, "download_web_file", fake_download)
    monkeypatch.setattr(collector_module, "is_suitable_article_image", lambda path: True)
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=StateStore(tmp_path / "state.sqlite"),
        renderer=MarkdownRenderer(tmp_path / "发布内容"),
        channel="TechnologyNewsSyncAssistant",
    )
    article = ArticleDraft(
        source="telegram",
        channel="technologynewssyncassistant",
        message_ids=[1],
        grouped_id=None,
        published_at=datetime(2026, 7, 20, tzinfo=UTC),
        date_key="20260720",
        daily_index=2,
        title="中国开始量产国产 DUV 光刻机 今年目标生产约 5 台",
        text="正文",
        links=[
            LinkRef(
                name="Reuters",
                url="https://www.reuters.com/world/china/china-begins-making-homegrown-duv-chipmaking-tools-information-reports-2026-07-27/",
            )
        ],
    )

    assets = collector._download_browser_link_images(article, tmp_path / "发布内容" / "20260720")

    assert len(assets) == 1
    assert assets[0].source == "web_browser_image"
    assert assets[0].filename == "20260720_002_PIC_001_中国开始量产国产 DUV 光刻机 今年目标生产约 5 台.jpg"


def test_download_browser_link_images_includes_ithome_domain(tmp_path: Path, monkeypatch):
    captured: list[tuple[str, str | None]] = []

    def fake_extract(url, timeout_ms=30000, profile_dir=None, limit=8):
        assert "ithome.com" in url
        return ["https://img.ithome.com/newsuploadfiles/2026/7/article-photo.jpg?x-bce-process=image/auto-orient,o_1/format,f_avif"]

    def fake_download(url, destination, proxy_url=None, referer=None):
        captured.append((url, referer))
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"ithome-image")
        return True

    monkeypatch.setattr(collector_module, "extract_source_image_urls", fake_extract)
    monkeypatch.setattr(collector_module, "download_web_file", fake_download)
    monkeypatch.setattr(collector_module, "is_suitable_article_image", lambda path: True)
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=StateStore(tmp_path / "state.sqlite"),
        renderer=MarkdownRenderer(tmp_path / "发布内容"),
        channel="TechnologyNewsSyncAssistant",
    )
    article = ArticleDraft(
        source="telegram",
        channel="technologynewssyncassistant",
        message_ids=[1],
        grouped_id=None,
        published_at=datetime(2026, 7, 31, tzinfo=UTC),
        date_key="20260731",
        daily_index=3,
        title="特斯拉车机更新，引入豆包和宠物模式",
        text="正文",
        links=[LinkRef(name="IT之家", url="https://www.ithome.com/0/983/943.htm")],
    )

    assets = collector._download_browser_link_images(article, tmp_path / "发布内容" / "20260731")

    assert len(assets) == 1
    assert assets[0].source == "web_browser_image"
    assert assets[0].filename == "20260731_003_PIC_001_特斯拉车机更新，引入豆包和宠物模式.jpg"
    assert captured == [
        (
            "https://img.ithome.com/newsuploadfiles/2026/7/article-photo.jpg",
            "https://www.ithome.com/0/983/943.htm",
        )
    ]


def test_internal_processing_error_is_not_silently_skipped(tmp_path: Path, monkeypatch):
    collector = TelegramCollector(
        client=FakeBotClient(),
        state=StateStore(tmp_path / "state.sqlite"),
        renderer=MarkdownRenderer(tmp_path / "发布内容"),
        channel="TechnologyNewsSyncAssistant",
    )

    def broken_process(messages):
        raise NameError("assets is not defined")

    monkeypatch.setattr(collector, "_process_message_group", broken_process)

    with pytest.raises(NameError):
        collector._process_group_safely([{"message_id": 183}])


def test_transient_telegram_error_detects_ssl_timeout():
    error = TelegramBotError(
        "Telegram API getUpdates failed: <urlopen error _ssl.c:983: The handshake operation timed out>"
    )

    assert is_transient_telegram_error(error)


def test_transient_telegram_error_detects_unexpected_eof():
    error = TelegramBotError(
        "Telegram API getUpdates failed: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred>"
    )

    assert is_transient_telegram_error(error)


def test_transient_telegram_error_does_not_retry_invalid_token():
    error = TelegramBotError("Telegram API getUpdates failed: Unauthorized: invalid token")

    assert not is_transient_telegram_error(error)


def test_poll_timeout_falls_back_to_short_poll_after_transient_error():
    assert poll_timeout_after_transient_error(30, 0) == 30
    assert poll_timeout_after_transient_error(30, 1) == 5
    assert poll_timeout_after_transient_error(3, 1) == 3


def test_telegram_network_hint_mentions_proxy_and_doctor():
    error = TelegramBotError("Telegram API getUpdates failed: <urlopen error _ssl.c:983: handshake timed out>")

    hint = telegram_network_hint(error, "http://127.0.0.1:7890")

    assert "127.0.0.1:7890" in hint
    assert "api.telegram.org" in hint
    assert "tgexporter doctor" in hint


def test_source_login_prompt_skips_public_sites():
    from tgexporter.source_sites import source_rule_for_url

    assert not should_prompt_source_login(source_rule_for_url("https://www.cls.cn/detail/2427193"))


def test_source_login_prompt_detects_login_or_subscription_sites():
    from tgexporter.source_sites import source_rule_for_url

    assert should_prompt_source_login(source_rule_for_url("https://x.com/SpaceXAI/status/1"))
    assert should_prompt_source_login(source_rule_for_url("https://www.axios.com/2026/07/20/ai-us-china-open-source-kimi"))
    assert should_prompt_source_login(source_rule_for_url("https://www.theinformation.com/articles/example"))
    assert should_prompt_source_login(source_rule_for_url("https://www.wsj.com/tech/ai/example"))
