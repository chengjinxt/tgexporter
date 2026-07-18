from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tgexporter.markdown_renderer import MarkdownRenderer
from tgexporter.models import ArticleDraft, LinkRef, MediaAsset


def test_markdown_renderer_outputs_frontmatter_and_media(tmp_path: Path):
    article = ArticleDraft(
        source="telegram",
        channel="TechnologyNewsSyncAssistant",
        message_ids=[11],
        grouped_id=None,
        published_at=datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        date_key="20260711",
        daily_index=1,
        title="测试文章",
        text="正文",
        links=[LinkRef(name="Example", url="https://example.com")],
        media=[
            MediaAsset(
                kind="image",
                filename="20260711_001_PIC_001_测试文章.jpg",
                path=tmp_path / "20260711_001_PIC_001_测试文章.jpg",
                source="telegram",
            )
        ],
    )
    path = MarkdownRenderer(tmp_path).render(article)
    text = path.read_text(encoding="utf-8")
    assert path.name == "20260711_001_测试文章.md"
    assert 'title: "测试文章"' in text
    assert "![测试文章](20260711_001_PIC_001_测试文章.jpg)" in text
    assert "\nExample\n\nhttps://example.com\n" in text
    assert text.index("![测试文章]") < text.index("正文")


def test_markdown_renderer_keeps_wechat_link_in_body_only(tmp_path: Path):
    article = ArticleDraft(
        source="telegram",
        channel="TechnologyNewsSyncAssistant",
        message_ids=[12],
        grouped_id=None,
        published_at=datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        date_key="20260711",
        daily_index=2,
        title="微信来源文章",
        text="正文\n\n[Tech 星球](https://mp.weixin.qq.com/s/mdg66FvdwwRFsg20HHnr4g)",
        links=[LinkRef(name="Tech 星球", url="https://mp.weixin.qq.com/s/mdg66FvdwwRFsg20HHnr4g")],
    )

    text = MarkdownRenderer(tmp_path).to_markdown(article)

    assert "[Tech 星球](https://mp.weixin.qq.com/s/mdg66FvdwwRFsg20HHnr4g)" in text
    assert "Tech 星球\n\nhttps://mp.weixin.qq.com/s/mdg66FvdwwRFsg20HHnr4g" not in text
