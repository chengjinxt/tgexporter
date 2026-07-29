from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from .collector import (
    TelegramCollector,
    is_transient_telegram_error,
    poll_timeout_after_transient_error,
    telegram_network_hint,
)
from .wechat_publisher import ARTICLE_SUFFIXES, MAX_WECHAT_ARTICLES, WechatPublisher


class DraftPublisher(Protocol):
    def try_auto_fill_many(
        self,
        article_paths: list[Path],
        headless: bool = False,
        login_timeout_seconds: int = 180,
        review_timeout_seconds: int = 0,
    ) -> Path:
        ...


@dataclass(frozen=True)
class DraftRunOptions:
    poll_timeout_seconds: int
    check_interval_seconds: int = 30
    daily_flush_hour: int = 23
    login_timeout_seconds: int = 180
    review_timeout_seconds: int = 0
    headless: bool = False


class DailyDraftRunner:
    def __init__(
        self,
        collector: TelegramCollector,
        publisher: DraftPublisher,
        output_base_dir: Path,
        timezone: str = "Asia/Shanghai",
        options: DraftRunOptions | None = None,
    ) -> None:
        self.collector = collector
        self.publisher = publisher
        self.output_base_dir = output_base_dir.resolve()
        self.timezone = ZoneInfo(timezone)
        self.options = options or DraftRunOptions(poll_timeout_seconds=30)

    def run_forever(self) -> None:
        self.output_base_dir.mkdir(parents=True, exist_ok=True)
        consecutive_errors = 0
        startup_now = datetime.now(self.timezone)
        print(
            "Draft runner started. "
            f"Output: {self.output_base_dir}; today only: {self.today_dir(startup_now)}; "
            f"batch size: {MAX_WECHAT_ARTICLES}; "
            f"daily flush: {self.options.daily_flush_hour:02d}:00.",
            flush=True,
        )
        while True:
            now = datetime.now(self.timezone)
            try:
                paths = self.collector.poll_once_for_date(
                    now.strftime("%Y%m%d"),
                    timeout=poll_timeout_after_transient_error(
                        self.options.poll_timeout_seconds,
                        consecutive_errors,
                    )
                )
                consecutive_errors = 0
            except Exception as exc:
                if not is_transient_telegram_error(exc):
                    raise
                consecutive_errors += 1
                delay = min(60, 5 * consecutive_errors)
                next_timeout = poll_timeout_after_transient_error(
                    self.options.poll_timeout_seconds,
                    consecutive_errors,
                )
                print(
                    f"Telegram getUpdates transient error: {exc}. "
                    f"Retry in {delay}s with poll timeout {next_timeout}s.",
                    file=sys.stderr,
                    flush=True,
                )
                print(telegram_network_hint(exc, self.collector.proxy_url), file=sys.stderr, flush=True)
                time.sleep(delay)
                continue

            for path in paths:
                print(f"Rendered: {path}", flush=True)
            self.publish_due_batches(now=now)
            time.sleep(self.options.check_interval_seconds)

    def publish_due_batches(self, now: datetime) -> list[Path]:
        previews: list[Path] = []
        date_dir = self.today_dir(now)
        pending = list_pending_articles(date_dir)
        while len(pending) >= MAX_WECHAT_ARTICLES:
            previews.append(self.publish_batch(date_dir, pending[:MAX_WECHAT_ARTICLES]))
            pending = list_pending_articles(date_dir)
        if pending and self.should_daily_flush(now):
            for batch in chunk_articles(pending, MAX_WECHAT_ARTICLES):
                previews.append(self.publish_batch(date_dir, batch))
        return previews

    def should_daily_flush(self, now: datetime) -> bool:
        return now.time() >= dt_time(hour=self.options.daily_flush_hour)

    def publish_batch(self, article_dir: Path, articles: list[Path]) -> Path:
        from .cli import move_published_batch, next_batch_index

        batch_number = next_batch_index(article_dir)
        print(f"Publishing draft batch 第{batch_number}批: {len(articles)} article(s).", flush=True)
        preview = self.publisher.try_auto_fill_many(
            articles,
            headless=self.options.headless,
            login_timeout_seconds=self.options.login_timeout_seconds,
            review_timeout_seconds=self.options.review_timeout_seconds,
        )
        batch_dir = article_dir / f"第{batch_number}批"
        moved = move_published_batch(articles, batch_dir)
        print(f"Moved published draft batch to {batch_dir}: {len(moved)} file(s).", flush=True)
        print(f"WeChat preview: {preview}", flush=True)
        return preview

    def today_dir(self, now: datetime) -> Path:
        return self.output_base_dir / now.strftime("%Y%m%d")


def list_pending_articles(article_dir: Path) -> list[Path]:
    if not article_dir.exists():
        return []
    return sorted(
        path.resolve() for path in article_dir.iterdir() if path.is_file() and path.suffix.lower() in ARTICLE_SUFFIXES
    )


def chunk_articles(articles: list[Path], size: int) -> list[list[Path]]:
    return [articles[index : index + size] for index in range(0, len(articles), size)]


def create_daily_draft_runner(
    collector: TelegramCollector,
    output_base_dir: Path,
    wechat_profile_dir: Path,
    timezone: str,
    options: DraftRunOptions,
) -> DailyDraftRunner:
    return DailyDraftRunner(
        collector=collector,
        publisher=WechatPublisher(wechat_profile_dir),
        output_base_dir=output_base_dir,
        timezone=timezone,
        options=options,
    )
