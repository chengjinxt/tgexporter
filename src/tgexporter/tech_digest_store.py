from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .filename import sanitize_title
from .tech_digest import BilingualArticle, TechCandidate, canonicalize_url


class TechDigestArchive:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def write_article(
        self,
        article: BilingualArticle,
        telegram_message: str,
        run_date: date,
        index: int,
    ) -> Path:
        date_dir = self.output_dir / run_date.strftime("%Y%m%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{index:02d}_{sanitize_title(article.zh_title, max_length=70)}.md"
        path = date_dir / filename
        candidate = article.candidate
        score = "null" if candidate.ranking_score is None else str(candidate.ranking_score)
        media_lines: list[str] = []
        for media in article.media:
            title = media.title or ("原文配图" if media.kind == "image" else "原文视频")
            if media.kind == "image":
                media_lines.extend([f"![{title}]({media.filename})", ""])
            elif media.kind == "video":
                media_lines.extend([f'<video controls src="{media.filename}"></video>', ""])
        content = "\n".join(
            [
                "---",
                f'source: "{_yaml_escape(candidate.source_name)}"',
                f'source_url: "{_yaml_escape(candidate.source_url)}"',
                f'url: "{_yaml_escape(candidate.url)}"',
                f'discussion_url: "{_yaml_escape(candidate.discussion_url)}"',
                f'ranking_method: "{_yaml_escape(candidate.ranking_method)}"',
                f"ranking_position: {candidate.ranking_position}",
                f"ranking_score: {score}",
                f'published_at: "{_yaml_escape(candidate.published_at or "")}"',
                "---",
                "",
                f"# {article.zh_title}",
                "",
                f"## {article.en_title}",
                "",
                *media_lines,
                "### 中文摘要",
                "",
                article.zh_summary,
                "",
                "### English Summary",
                "",
                article.en_summary,
                "",
                f"[原文 / Source]({candidate.url})",
                "",
                "<!-- TELEGRAM_MESSAGE_BEGIN -->",
                telegram_message,
                "<!-- TELEGRAM_MESSAGE_END -->",
                "",
            ]
        )
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def write_manifest(self, run_date: date, records: list[dict[str, object]]) -> Path:
        date_dir = self.output_dir / run_date.strftime("%Y%m%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        path = date_dir / "manifest.json"
        existing: list[dict[str, object]] = []
        if path.exists():
            try:
                raw_existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw_existing, list):
                    existing = [item for item in raw_existing if isinstance(item, dict)]
            except (OSError, json.JSONDecodeError):
                existing = []
        merged = {str(item.get("url", "")): item for item in existing if item.get("url")}
        for record in records:
            if record.get("url"):
                merged[str(record["url"])] = record
        path.write_text(
            json.dumps(list(merged.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        return path


class TechDigestState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def processed_urls(self) -> set[str]:
        with self._connect() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "select canonical_url from tech_articles where status = 'published'"
                )
            }

    def published_count(self, run_date: date) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "select count(*) from tech_articles where run_date = ? and status = 'published'",
                (run_date.isoformat(),),
            ).fetchone()
            return int(row[0]) if row else 0

    def create_backup(self, run_date: date) -> Path:
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        backup_path = self.path.with_name(
            f"{self.path.name}.before-clear-{run_date.strftime('%Y%m%d')}-{timestamp}.bak"
        )
        with sqlite3.connect(self.path) as source, sqlite3.connect(backup_path) as destination:
            source.backup(destination)
        return backup_path

    def clear_published(self, run_date: date) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "delete from tech_articles where run_date = ? and status = 'published'",
                (run_date.isoformat(),),
            )
            return int(cursor.rowcount)

    def record(self, candidate: TechCandidate, local_path: Path, status: str, run_date: date) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                insert into tech_articles(
                    canonical_url, source_key, source_name, title, ranking_method,
                    ranking_position, ranking_score, local_path, status, run_date
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(canonical_url) do update set
                    title = excluded.title,
                    local_path = excluded.local_path,
                    status = excluded.status,
                    run_date = excluded.run_date,
                    updated_at = current_timestamp
                """,
                (
                    canonicalize_url(candidate.url),
                    candidate.source_key,
                    candidate.source_name,
                    candidate.title,
                    candidate.ranking_method,
                    candidate.ranking_position,
                    candidate.ranking_score,
                    str(local_path),
                    status,
                    run_date.isoformat(),
                ),
            )

    def mark_published(self, url: str, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                update tech_articles
                set status = 'published', telegram_message_id = ?, updated_at = current_timestamp
                where canonical_url = ?
                """,
                (message_id, canonicalize_url(url)),
            )

    def publication_status(self, url: str) -> tuple[str, int | None] | None:
        with self._connect() as connection:
            row = connection.execute(
                "select status, telegram_message_id from tech_articles where canonical_url = ?",
                (canonicalize_url(url),),
            ).fetchone()
            return (str(row[0]), int(row[1]) if row[1] is not None else None) if row else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("pragma journal_mode = wal")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                create table if not exists tech_articles (
                    canonical_url text primary key,
                    source_key text not null,
                    source_name text not null,
                    title text not null,
                    ranking_method text not null,
                    ranking_position integer not null,
                    ranking_score real,
                    local_path text not null,
                    status text not null,
                    run_date text not null,
                    telegram_message_id integer,
                    created_at text not null default current_timestamp,
                    updated_at text not null default current_timestamp
                );
                """
            )
            columns = {str(row[1]) for row in connection.execute("pragma table_info(tech_articles)")}
            if "run_date" not in columns:
                connection.execute("alter table tech_articles add column run_date text not null default ''")


def _yaml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")
