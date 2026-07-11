from tgexporter.wechat_publisher import markdown_to_wechat_html, parse_markdown_article


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

