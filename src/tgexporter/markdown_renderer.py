from __future__ import annotations

from pathlib import Path

from .filename import article_filename
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
            "message_ids:",
        ]
        for message_id in article.message_ids:
            lines.append(f"  - {message_id}")
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

        if article.text.strip():
            lines.extend([article.text.strip(), ""])

        for media in article.media:
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

        if article.links:
            lines.extend(["## 引用来源", ""])
            for link in article.links:
                lines.append(f"- {link.name}：{link.url}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"


def quote_yaml(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

