from __future__ import annotations

import json
import sqlite3
import urllib.parse
from pathlib import Path

from .models import ArticleDraft

IGNORED_STAT_DOMAINS = {"mp.weixin.qq.com", "t.me", "telegram.me"}


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

    def record_messages_processed(self, channel: str, message_ids: list[int], article_key_value: str) -> None:
        with self._connect() as conn:
            for message_id in message_ids:
                conn.execute(
                    """
                    insert or ignore into messages(channel, message_id, article_key)
                    values (?, ?, ?)
                    """,
                    (channel, message_id, article_key_value),
                )

    def article_path_by_title(self, channel: str, date_key: str, title: str) -> Path | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select markdown_path
                from articles
                where channel = ? and date_key = ? and title = ?
                order by daily_index asc
                limit 1
                """,
                (channel, date_key, title),
            ).fetchone()
            return Path(row[0]) if row else None

    def next_daily_index(self, date_key: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "select coalesce(max(daily_index), 0) from articles where date_key = ?",
                (date_key,),
            ).fetchone()
            return int(row[0]) + 1

    def record_article(self, article: ArticleDraft, markdown_path: Path) -> None:
        with self._connect() as conn:
            key = article_key(article)
            conn.execute(
                """
                insert or replace into articles(
                    article_key, channel, date_key, daily_index, title, grouped_id,
                    message_ids, markdown_path, status, published_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
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
                    (article.channel, message_id, key),
                )
            for link in article.links:
                domain = domain_from_url(link.url)
                if not domain or domain in IGNORED_STAT_DOMAINS:
                    continue
                conn.execute(
                    """
                    insert or ignore into reference_domains(article_key, url, domain, name)
                    values (?, ?, ?, ?)
                    """,
                    (key, link.url, domain, link.name),
                )

    def source_url_processed(self, channel: str, url: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select 1 from source_urls where channel = ? and url = ?",
                (channel, normalize_url(url)),
            ).fetchone()
            return row is not None

    def article_path_by_source_url(self, channel: str, url: str) -> Path | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select markdown_path
                from source_urls
                where channel = ? and url = ?
                limit 1
                """,
                (channel, normalize_url(url)),
            ).fetchone()
            return Path(row[0]) if row and row[0] else None

    def record_source_url(self, channel: str, url: str, article: ArticleDraft, markdown_path: Path) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert or replace into source_urls(channel, url, article_key, markdown_path)
                values (?, ?, ?, ?)
                """,
                (channel, normalize_url(url), article_key(article), str(markdown_path)),
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

    def domain_stats(self, limit: int = 50) -> list[tuple[str, int]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select domain, count(*) as count
                from reference_domains
                where domain not in ('mp.weixin.qq.com', 't.me', 'telegram.me')
                group by domain
                order by count desc, domain asc
                limit ?
                """,
                (limit,),
            ).fetchall()
            return [(str(domain), int(count)) for domain, count in rows]

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

                create table if not exists reference_domains (
                    article_key text not null,
                    url text not null,
                    domain text not null,
                    name text not null,
                    created_at text not null default current_timestamp,
                    primary key(article_key, url)
                );

                create index if not exists idx_reference_domains_domain
                on reference_domains(domain);

                create table if not exists source_urls (
                    channel text not null,
                    url text not null,
                    article_key text not null,
                    markdown_path text,
                    created_at text not null default current_timestamp,
                    primary key(channel, url)
                );
                """
            )


def article_key(article: ArticleDraft) -> str:
    if article.source_id:
        return f"{article.channel}:source:{article.source_id}"
    if article.grouped_id:
        return f"{article.channel}:group:{article.grouped_id}"
    ids = "-".join(str(item) for item in article.message_ids)
    return f"{article.channel}:messages:{ids}"


def domain_from_url(url: str) -> str:
    hostname = urllib.parse.urlparse(url).hostname or ""
    hostname = hostname.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def normalize_url(url: str) -> str:
    return url.strip()
