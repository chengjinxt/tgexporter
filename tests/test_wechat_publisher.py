from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tgexporter import cli as cli_module
from tgexporter.cli import (
    article_account,
    batch_size_for_account,
    build_parser,
    chunk_articles,
    collect_markdown_local_assets,
    move_published_batch,
    next_batch_index,
    resolve_publish_articles,
    validate_publish_account,
    wechat_profile_dir_for_account,
)
from tgexporter.config import load_config
from tgexporter.draft_runner import DailyDraftRunner, DailyDraftTarget, DraftRunOptions
from tgexporter.wechat_publisher import (
    build_wechat_body_items,
    clean_wechat_title,
    close_wechat_editor_blocking_overlays,
    markdown_to_wechat_html,
    parse_markdown_article,
    validate_wechat_article_count,
)


class FakeDraftPublisher:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def try_auto_fill_many(
        self,
        article_paths,
        headless=False,
        login_timeout_seconds=180,
        review_timeout_seconds=0,
    ):
        self.batches.append([path.name for path in article_paths])
        return article_paths[0].with_suffix(".html")


class FakeKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press(self, key: str) -> None:
        self.pressed.append(key)


class FakeOverlayPage:
    def __init__(self, result: dict) -> None:
        self.keyboard = FakeKeyboard()
        self.result = result
        self.script = ""
        self.waits: list[int] = []

    def evaluate(self, script: str):
        self.script = script
        return self.result

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


def write_article(path, image_name=None):
    image_block = f"\n![cover]({image_name})\n" if image_name else ""
    path.write_text(f"# {path.stem}\n{image_block}\n正文", encoding="utf-8")


def test_close_wechat_editor_blocking_overlays_handles_topic_card_sticker():
    page = FakeOverlayPage({"clicked": False, "hidden": 1})

    assert close_wechat_editor_blocking_overlays(page) is True
    assert page.keyboard.pressed == ["Escape"]
    assert "topic_card_sticker" in page.script
    assert "data-codex-hidden-overlay" in page.script


def test_wechat_preview_converts_markdown_assets(tmp_path):
    image = tmp_path / "pic.jpg"
    image.write_bytes(b"img")
    article_path = tmp_path / "article.md"
    article_path.write_text(
        """---
title: "预览文章"
---

# 预览文章

正文

![图](pic.jpg)
""",
        encoding="utf-8",
    )

    article = parse_markdown_article(article_path)
    html = markdown_to_wechat_html(article)

    assert article.title == "预览文章"
    assert "<h1" in html and "预览文章</h1>" in html
    assert image.resolve().as_uri() in html


def test_wechat_publish_body_skips_duplicate_title_images_and_local_video(tmp_path):
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"img")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    article_path = tmp_path / "article.md"
    article_path.write_text(
        """---
title: "字节跳动发布 Seedream 5.0 Pro，支持多语言生成与精准编辑"
---

# 字节跳动发布 Seedream 5.0 Pro，支持多语言生成与精准编辑

字节跳动发布 Seedream 5.0 Pro，支持多语言生成与精准编辑

![视频封面](cover.jpg)

正文内容

📢 频道 👥 群组 📝 投稿

视频：[clip.mp4](clip.mp4)
""",
        encoding="utf-8",
    )

    article = parse_markdown_article(article_path)
    html = markdown_to_wechat_html(
        article,
        render_local_videos=False,
        include_title=False,
        skip_duplicate_intro=True,
        include_images=False,
    )

    assert "Seedream 5.0 Pro" not in html
    assert "<img" not in html
    assert "<video" not in html
    assert "clip.mp4" not in html
    assert "频道" not in html
    assert "群组" not in html
    assert "投稿" not in html
    assert "正文内容" in html


def test_wechat_publish_body_keeps_reference_label_even_when_label_is_in_title(tmp_path):
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"img")
    article_path = tmp_path / "article.md"
    article_path.write_text(
        """---
title: "OpenAI 发售 Codex Micro 智能体键盘，配备 13 个机械按钮"
---

# OpenAI 发售 Codex Micro 智能体键盘，配备 13 个机械按钮

![Telegram图片](cover.jpg)

OpenAI 与 Work Louder 联名推出 Codex Micro 实体控制器。

OpenAI

https://openai.com/zh-Hans-CN/supply/co-lab/work-louder/
""",
        encoding="utf-8",
    )

    article = parse_markdown_article(article_path)
    html = markdown_to_wechat_html(
        article,
        render_local_videos=False,
        include_title=False,
        skip_duplicate_intro=True,
        include_images=False,
    )
    items = build_wechat_body_items(article)

    assert "OpenAI</p>" in html
    assert "https://openai.com/zh-Hans-CN/supply/co-lab/work-louder/" in html
    assert any(item[0] == "html" and "OpenAI</p>" in str(item[1]) for item in items)
    assert any(
        item[0] == "html" and "https://openai.com/zh-Hans-CN/supply/co-lab/work-louder/" in str(item[1])
        for item in items
    )


def test_wechat_body_items_keep_multiple_local_images_in_order(tmp_path):
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"1")
    second.write_bytes(b"2")
    article_path = tmp_path / "article.md"
    article_path.write_text(
        """---
title: "多图文章"
---

# 多图文章

开头段落

![第一张](first.jpg)

中间段落

![第二张](second.jpg)
""",
        encoding="utf-8",
    )

    article = parse_markdown_article(article_path)
    items = build_wechat_body_items(article)

    assert [kind for kind, _ in items] == ["html", "image", "html", "image"]
    assert items[1][1] == first
    assert items[3][1] == second
    assert "多图文章" not in str(items[0][1])


def test_wechat_markdown_links_render_as_clickable_anchors(tmp_path):
    article_path = tmp_path / "article.md"
    article_path.write_text(
        """---
title: "公众号链接文章"
---

# 公众号链接文章

据 [长安街知事](https://mp.weixin.qq.com/s/Wp0PdV83btg8skL6ypfXHw) 报道，正文内容。
""",
        encoding="utf-8",
    )

    article = parse_markdown_article(article_path)
    html = markdown_to_wechat_html(article, include_title=False)
    items = build_wechat_body_items(article)

    assert 'href="https://mp.weixin.qq.com/s/Wp0PdV83btg8skL6ypfXHw"' in html
    assert "长安街知事</a>" in html
    assert 'href="https://mp.weixin.qq.com/s/Wp0PdV83btg8skL6ypfXHw"' in str(items[0][1])
    assert "长安街知事</a>" in str(items[0][1])


def test_wechat_publish_accepts_multiple_article_paths():
    args = build_parser().parse_args(["publish-wechat", "--article", "main.md", "sub.md", "--auto-fill"])

    assert [path.name for path in args.article] == ["main.md", "sub.md"]
    assert args.auto_fill is True


def test_wechat_publish_accepts_article_dir(tmp_path):
    second = tmp_path / "002-second.md"
    first = tmp_path / "001-first.md"
    mk_article = tmp_path / "003-third.mk"
    ignored = tmp_path / "cover.jpg"
    nested = tmp_path / "nested"
    nested.mkdir()
    nested_article = nested / "000-nested.md"
    second.write_text("# second", encoding="utf-8")
    first.write_text("# first", encoding="utf-8")
    mk_article.write_text("# third", encoding="utf-8")
    nested_article.write_text("# nested", encoding="utf-8")
    ignored.write_bytes(b"img")
    args = build_parser().parse_args(["publish-wechat", "--article-dir", str(tmp_path), "--auto-fill"])

    articles = resolve_publish_articles(args.article, args.article_dir)

    assert [path.name for path in articles] == ["001-first.md", "002-second.md", "003-third.mk"]


def test_wechat_publish_accepts_movie_account():
    args = build_parser().parse_args(["publish-wechat", "--account", "movie4k", "--article-dir", "movies"])

    assert args.account == "movie4k"


def test_publish_account_validation_rejects_mixed_movie_and_default_articles(tmp_path):
    movie = tmp_path / "movie.md"
    movie.write_text(
        """---
title: "电影"
route: "movie4k"
account: "movie4k"
---

# 电影
""",
        encoding="utf-8",
    )
    tech = tmp_path / "tech.md"
    tech.write_text("# 科技", encoding="utf-8")

    assert article_account(movie) == "movie4k"
    validate_publish_account([movie], "movie4k")
    with pytest.raises(RuntimeError, match="Article account mismatch"):
        validate_publish_account([tech], "movie4k")
    with pytest.raises(RuntimeError, match="Article account mismatch"):
        validate_publish_account([movie], "default")


def test_movie_account_uses_dedicated_profile_and_batch_size(tmp_path):
    config = load_config(tmp_path)

    assert wechat_profile_dir_for_account(config, "movie4k") == tmp_path / "runtime" / "wechat-profile-movie4k"
    assert batch_size_for_account(config, "movie4k") == 2


def test_mk_wechat_conversion_uses_wechat_styled_blocks(tmp_path):
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"img")
    article_path = tmp_path / "article.mk"
    article_path.write_text(
        """---
title: "MK 示例"
---

# MK 示例

## 亮点

> 适合直接粘贴公众号。

- 支持 **加粗**
- 支持 `inline code`

```bash
tgexporter publish-wechat --article-dir ./发布内容/20260729
```

![图](cover.jpg)
""",
        encoding="utf-8",
    )

    article = parse_markdown_article(article_path)
    html = markdown_to_wechat_html(article)
    items = build_wechat_body_items(article)

    assert article.title == "MK 示例"
    assert 'style="' in html
    assert "<h2" in html and "亮点" in html
    assert "<blockquote" in html
    assert "<ul" in html and "<strong>加粗</strong>" in html
    assert "<pre" in html and "tgexporter publish-wechat" in html
    assert [kind for kind, _ in items][-1] == "image"
    assert items[-1][1] == image


def test_wechat_publish_rejects_article_and_article_dir_together(tmp_path):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["publish-wechat", "--article", "main.md", "--article-dir", str(tmp_path)])


def test_listen_accepts_channel_override():
    args = build_parser().parse_args(
        ["listen", "--channel", "TechnologyNewsSyncAssistant", "--save-dir", "E:/out", "--once"]
    )

    assert args.channel == "TechnologyNewsSyncAssistant"
    assert str(args.save_dir).replace("\\", "/") == "E:/out"
    assert args.once is True


def test_default_root_for_frozen_portable_prefers_project_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    portable = project / "dist" / "tgexporter-portable"
    portable.mkdir(parents=True)
    executable = portable / "tgexporter.exe"
    executable.write_bytes(b"exe")
    (project / "config.local.toml").write_text("[telegram]\nbot_token = \"x\"\n", encoding="utf-8")

    monkeypatch.chdir(portable)
    monkeypatch.setattr(cli_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli_module.sys, "executable", str(executable))

    assert cli_module.default_root() == project


def test_chunk_articles_uses_wechat_batch_size(tmp_path):
    articles = [tmp_path / f"{index:03d}.md" for index in range(17)]

    batches = chunk_articles(articles, 8)

    assert [len(batch) for batch in batches] == [8, 8, 1]


def test_next_batch_index_skips_existing_batch_dirs(tmp_path):
    (tmp_path / "第1批").mkdir()
    (tmp_path / "第3批").mkdir()
    (tmp_path / "草稿").mkdir()

    assert next_batch_index(tmp_path) == 4


def test_stats_domains_accepts_typo_alias():
    args = build_parser().parse_args(["stats-domainsstats-domains"])

    assert args.func == cli_module.cmd_stats_domains


def test_top_level_draft_flag_parses_without_subcommand():
    args = build_parser().parse_args(["--draft"])

    assert args.one_click_draft is True
    assert args.command is None


def test_source_login_command_parses_url_and_timeout():
    args = build_parser().parse_args(
        ["source-login", "--url", "https://x.com/SpaceXAI/status/1", "--timeout", "1"]
    )

    assert args.func == cli_module.cmd_source_login
    assert args.url == "https://x.com/SpaceXAI/status/1"
    assert args.timeout == 1


def test_move_published_batch_moves_markdown_and_referenced_local_assets(tmp_path):
    image = tmp_path / "001_PIC_001_含 空格 图片.jpg"
    video = tmp_path / "001_VID_001_含 空格 视频.mp4"
    unused = tmp_path / "unused.jpg"
    article = tmp_path / "001-title.md"
    image.write_bytes(b"image")
    video.write_bytes(b"video")
    unused.write_bytes(b"unused")
    article.write_text(
        """# title

![cover](001_PIC_001_含 空格 图片.jpg)

视频：[clip](001_VID_001_含 空格 视频.mp4)

[external](https://example.com/a)
""",
        encoding="utf-8",
    )

    assert collect_markdown_local_assets(article) == [image.resolve(), video.resolve()]

    moved = move_published_batch([article], tmp_path / "第1批")

    assert sorted(path.name for path in moved) == ["001-title.md", "001_PIC_001_含 空格 图片.jpg", "001_VID_001_含 空格 视频.mp4"]
    assert not article.exists()
    assert not image.exists()
    assert not video.exists()
    assert unused.exists()
    assert (tmp_path / "第1批" / "001-title.md").exists()


def test_wechat_article_count_limit():
    validate_wechat_article_count([object()] * 8)

    with pytest.raises(ValueError, match="at most 8"):
        validate_wechat_article_count([object()] * 9)


def test_clean_wechat_title_removes_icon_characters():
    assert clean_wechat_title("📱 华为 5G 旗舰重返海外，新机实测峰值速率突破 1100 Mbps") == (
        "华为 5G 旗舰重返海外，新机实测峰值速率突破 1100 Mbps"
    )
    assert clean_wechat_title("顶尖 AI 企业安全评级普遍偏低 榜首 Anthropic 仅获 C+") == (
        "顶尖 AI 企业安全评级普遍偏低 榜首 Anthropic 仅获 C+"
    )


def test_daily_draft_runner_publishes_full_batches_before_deadline(tmp_path):
    date_dir = tmp_path / "发布内容" / "20260728"
    date_dir.mkdir(parents=True)
    for index in range(9):
        write_article(date_dir / f"{index + 1:03d}-article.md")
    publisher = FakeDraftPublisher()
    runner = DailyDraftRunner(
        collector=object(),
        publisher=publisher,
        output_base_dir=tmp_path / "发布内容",
        options=DraftRunOptions(poll_timeout_seconds=1),
    )

    runner.publish_due_batches(datetime(2026, 7, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert len(publisher.batches) == 1
    assert len(publisher.batches[0]) == 8
    assert len(list((date_dir / "第1批").glob("*.md"))) == 8
    assert [path.name for path in date_dir.glob("*.md")] == ["009-article.md"]


def test_daily_draft_runner_flushes_pending_articles_at_23(tmp_path):
    date_dir = tmp_path / "发布内容" / "20260728"
    date_dir.mkdir(parents=True)
    image = date_dir / "001-image.jpg"
    image.write_bytes(b"image")
    write_article(date_dir / "001-article.md", image.name)
    write_article(date_dir / "002-article.md")
    publisher = FakeDraftPublisher()
    runner = DailyDraftRunner(
        collector=object(),
        publisher=publisher,
        output_base_dir=tmp_path / "发布内容",
        options=DraftRunOptions(poll_timeout_seconds=1),
    )

    runner.publish_due_batches(datetime(2026, 7, 28, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert publisher.batches == [["001-article.md", "002-article.md"]]
    assert sorted(path.name for path in (date_dir / "第1批").iterdir()) == [
        "001-article.md",
        "001-image.jpg",
        "002-article.md",
    ]
    assert list(date_dir.glob("*.md")) == []


def test_daily_draft_runner_ignores_previous_date_directories(tmp_path):
    old_dir = tmp_path / "发布内容" / "20260725"
    today_dir = tmp_path / "发布内容" / "20260728"
    old_dir.mkdir(parents=True)
    today_dir.mkdir(parents=True)
    for index in range(8):
        write_article(old_dir / f"{index + 1:03d}-old.md")
    publisher = FakeDraftPublisher()
    runner = DailyDraftRunner(
        collector=object(),
        publisher=publisher,
        output_base_dir=tmp_path / "发布内容",
        options=DraftRunOptions(poll_timeout_seconds=1),
    )

    runner.publish_due_batches(datetime(2026, 7, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert publisher.batches == []
    assert sorted(path.name for path in old_dir.glob("*.md")) == [f"{index + 1:03d}-old.md" for index in range(8)]


def test_daily_draft_runner_publishes_movie_target_in_batches_of_two(tmp_path):
    movie_dir = tmp_path / "发布内容" / "4K影视屋" / "20260801"
    movie_dir.mkdir(parents=True)
    for index in range(3):
        write_article(movie_dir / f"{index + 1:03d}-movie.md")
    publisher = FakeDraftPublisher()
    runner = DailyDraftRunner(
        collector=object(),
        publisher=FakeDraftPublisher(),
        output_base_dir=tmp_path / "发布内容",
        options=DraftRunOptions(poll_timeout_seconds=1),
        targets=[
            DailyDraftTarget(
                name="movie4k",
                article_base_dir=tmp_path / "发布内容" / "4K影视屋",
                publisher=publisher,
                batch_size=2,
            )
        ],
    )

    runner.publish_due_batches(datetime(2026, 8, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")))

    assert publisher.batches == [["001-movie.md", "002-movie.md"]]
    assert len(list((movie_dir / "第1批").glob("*.md"))) == 2
    assert [path.name for path in movie_dir.glob("*.md")] == ["003-movie.md"]
