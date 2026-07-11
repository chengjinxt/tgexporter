from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tgexporter.models import ArticleDraft, LinkRef
from tgexporter.state import StateStore, domain_from_url


def test_domain_from_url_normalizes_www_prefix():
    assert domain_from_url("https://www.bloomberg.com/news/articles/a") == "bloomberg.com"


def test_state_records_reference_domain_stats(tmp_path: Path):
    state = StateStore(tmp_path / "state.sqlite")
    article = ArticleDraft(
        source="telegram",
        channel="TechnologyNewsSyncAssistant",
        message_ids=[1],
        grouped_id=None,
        published_at=datetime(2026, 7, 11, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        date_key="20260711",
        daily_index=1,
        title="测试",
        text="正文",
        links=[
            LinkRef(name="Bloomberg", url="https://www.bloomberg.com/news/a"),
            LinkRef(name="OpenAI", url="https://openai.com/index/a"),
        ],
    )

    state.record_article(article, tmp_path / "article.md")

    assert state.domain_stats() == [("bloomberg.com", 1), ("openai.com", 1)]
