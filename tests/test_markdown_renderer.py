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
    assert "- Example：https://example.com" in text

