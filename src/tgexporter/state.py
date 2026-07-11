from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import ArticleDraft


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def get_last_update_id(self) -> int | None:
        with self._connect() as conn:
            row = conn.execute("select value from kv where key = 'last_update_id'").fetchone()
            return int(row[0]) if row else None

    def set_last_update_id(self, update_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into kv(key, value) values('last_update_id', ?) "
                "on conflict(key) do update set value = excluded.value",
                (str(update_id),),
            )

    def message_processed(self, channel: str, message_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select 1 from messages where channel = ? and message_id = ?",
                (channel, message_id),
            ).fetchone()
            return row is not None

    def next_daily_index(self, date_key: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "select coalesce(max(daily_index), 0) from articles where date_key = ?",
                (date_key,),
            ).fetchone()
            return int(row[0]) + 1

    def record_article(self, article: ArticleDraft, markdown_path: Path) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert or replace into articles(
                    article_key, channel, date_key, daily_index, title, grouped_id,
                    message_ids, markdown_path, status, published_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article_key(article),
                    article.channel,
                    article.date_key,
                    article.daily_index,
                    article.title,
                    article.grouped_id,
                    json.dumps(article.message_ids, ensure_ascii=False),
                    str(markdown_path),
                    article.status,
                    article.published_at.isoformat(),
                ),
            )
            for message_id in article.message_ids:
                conn.execute(
                    """
                    insert or ignore into messages(channel, message_id, article_key)
                    values (?, ?, ?)
                    """,
                    (article.channel, message_id, article_key(article)),
                )

    def update_article_status(self, article_path: Path, status: str, error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                update articles
                set status = ?, error = ?, updated_at = current_timestamp
                where markdown_path = ?
                """,
                (status, error, str(article_path)),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("pragma journal_mode = wal")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists kv (
                    key text primary key,
                    value text not null
                );

                create table if not exists articles (
                    article_key text primary key,
                    channel text not null,
                    date_key text not null,
                    daily_index integer not null,
                    title text not null,
                    grouped_id text,
                    message_ids text not null,
                    markdown_path text not null,
                    status text not null,
                    error text,
                    published_at text not null,
                    created_at text not null default current_timestamp,
                    updated_at text not null default current_timestamp
                );

                create unique index if not exists idx_articles_daily
                on articles(date_key, daily_index);

                create table if not exists messages (
                    channel text not null,
                    message_id integer not null,
                    article_key text not null,
                    created_at text not null default current_timestamp,
                    primary key(channel, message_id)
                );
                """
            )


def article_key(article: ArticleDraft) -> str:
    if article.grouped_id:
        return f"{article.channel}:group:{article.grouped_id}"
    ids = "-".join(str(item) for item in article.message_ids)
    return f"{article.channel}:messages:{ids}"

