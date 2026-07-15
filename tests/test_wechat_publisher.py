import pytest

from tgexporter.cli import build_parser
from tgexporter.wechat_publisher import (
    build_wechat_body_items,
    clean_wechat_title,
    markdown_to_wechat_html,
    parse_markdown_article,
    validate_wechat_article_count,
)


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
    assert "<h1>预览文章</h1>" in html
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
    assert "正文内容" in html


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


def test_wechat_publish_accepts_multiple_article_paths():
    args = build_parser().parse_args(["publish-wechat", "--article", "main.md", "sub.md", "--auto-fill"])

    assert [path.name for path in args.article] == ["main.md", "sub.md"]
    assert args.auto_fill is True


def test_listen_accepts_channel_override():
    args = build_parser().parse_args(["listen", "--channel", "TechnologyNewsSyncAssistant", "--once"])

    assert args.channel == "TechnologyNewsSyncAssistant"
    assert args.once is True


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
