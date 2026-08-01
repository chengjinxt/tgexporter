from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class LinkRef:
    name: str
    url: str
    image_url: str | None = None
    image_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaAsset:
    kind: str
    filename: str
    path: Path
    source: str
    title: str | None = None


@dataclass
class ArticleDraft:
    source: str
    channel: str
    message_ids: list[int]
    grouped_id: str | None
    published_at: datetime
    date_key: str
    daily_index: int
    title: str
    text: str
    links: list[LinkRef] = field(default_factory=list)
    media: list[MediaAsset] = field(default_factory=list)
    status: str = "collected"
    source_id: str | None = None
    route: str = "default"
    account: str = "default"
    output_subdir: str = ""
    cover_brand: str = "firemail 科技频道"

    @property
    def prefix(self) -> str:
        return f"{self.date_key}_{self.daily_index:03d}"
