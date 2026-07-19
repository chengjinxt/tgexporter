from __future__ import annotations

from pathlib import Path

from .filename import article_filename
from .link_enricher import is_wechat_article_url
from .models import ArticleDraft


class MarkdownRenderer:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    def render(self, article: ArticleDraft) -> Path:
        date_dir = self.base_dir / article.date_key
        date_dir.mkdir(parents=True, exist_ok=True)
        path = date_dir / article_filename(article.date_key, article.daily_index, article.title)
        path.write_text(self.to_markdown(article), encoding="utf-8")
        return path

    def to_markdown(self, article: ArticleDraft) -> str:
        lines: list[str] = [
            "---",
            f"title: {quote_yaml(article.title)}",
            f"date: {article.published_at.strftime('%Y-%m-%d %H:%M:%S')}",
            f"source: {article.source}",
            f"channel: {article.channel}",
        ]
        if article.message_ids:
            lines.append("message_ids:")
            for message_id in article.message_ids:
                lines.append(f"  - {message_id}")
        else:
            lines.append("message_ids: []")
        if article.source_id:
            lines.append(f"source_id: {quote_yaml(article.source_id)}")
        if article.grouped_id:
            lines.append(f"grouped_id: {article.grouped_id}")
        lines.extend(
            [
                f"status: {article.status}",
                "---",
                "",
                f"# {article.title}",
                "",
            ]
        )

        image_media = [media for media in article.media if media.kind == "image"]
        other_media = [media for media in article.media if media.kind != "image"]

        for media in image_media:
            alt = media.title or article.title
            lines.append(f"![{alt}]({media.filename})")
            lines.append("")

        if article.text.strip():
            lines.extend([article.text.strip(), ""])

        for media in other_media:
            if media.kind == "image":
                alt = media.title or article.title
                lines.append(f"![{alt}]({media.filename})")
            elif media.kind == "video":
                label = media.title or media.filename
                lines.append(f"视频：[{label}]({media.filename})")
            else:
                label = media.title or media.filename
                lines.append(f"附件：[{label}]({media.filename})")
            lines.append("")

        reference_links = [link for link in article.links if not is_wechat_article_url(link.url)]
        if reference_links:
            for link in reference_links:
                lines.append(link.name)
                lines.append("")
                lines.append(link.url)
                lines.append("")

        return "\n".join(lines).rstrip() + "\n"


def quote_yaml(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
