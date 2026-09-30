from __future__ import annotations

import html as html_module
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from time import sleep
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from .models import MediaAsset


@dataclass(frozen=True)
class TechSource:
    key: str
    name: str
    url: str
    ranking_method: str
    ranking_labels: tuple[str, ...] = ()
    strategy: str = "ranked_page"
    public_url: str = ""


@dataclass(frozen=True)
class TechCandidate:
    source_key: str
    source_name: str
    title: str
    url: str
    ranking_method: str
    ranking_position: int
    ranking_score: float | None = None
    summary: str = ""
    published_at: str | None = None
    source_url: str = ""
    discussion_url: str = ""
    image_urls: tuple[str, ...] = ()
    video_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class BilingualArticle:
    candidate: TechCandidate
    zh_title: str
    en_title: str
    zh_summary: str
    en_summary: str
    media: tuple[MediaAsset, ...] = ()


@dataclass(frozen=True)
class TechDigestRunResult:
    selected_count: int
    rendered_count: int
    published_count: int
    article_paths: tuple[Path, ...]
    manifest_path: Path
    errors: tuple[str, ...]
    daily_limit_reached: bool = False
    published_today: int = 0
    daily_limit: int = 0


CORE_TECH_SOURCES = (
    TechSource("verge", "The Verge", "https://www.theverge.com/", "most_popular", ("Most Popular",)),
    TechSource("techcrunch", "TechCrunch", "https://techcrunch.com/", "most_popular", ("Most Popular",)),
    TechSource("ars", "Ars Technica", "https://arstechnica.com/", "most_read", ("Most Read",)),
    TechSource(
        "wired",
        "Wired",
        "https://www.wired.com/",
        "editors_picks",
        ("Today's Picks", "Today’s Picks"),
        strategy="json_ld_item_list",
    ),
    TechSource("engadget", "Engadget", "https://www.engadget.com/", "latest_homepage", ("More Stories",)),
    TechSource("tomshardware", "Tom's Hardware", "https://www.tomshardware.com/", "news_stream", ("News Stream",)),
    TechSource(
        "servethehome",
        "ServeTheHome",
        "https://www.servethehome.com/",
        "homepage_editorial",
        strategy="servethehome_modules",
    ),
    TechSource(
        "hackernews",
        "Hacker News",
        "https://hacker-news.firebaseio.com/v0/topstories.json",
        "topstories_score",
        strategy="hacker_news_api",
        public_url="https://news.ycombinator.com/",
    ),
)


BACKUP_TECH_SOURCES = (
    TechSource("ithome", "IT之家", "https://www.ithome.com/Default.htm", "daily_ranking", ("日榜", "排行榜")),
    TechSource("techspot", "TechSpot", "https://www.techspot.com/", "most_popular", ("Most Popular",)),
    TechSource("theregister", "The Register", "https://www.theregister.com/", "most_popular", ("Most Popular",)),
    TechSource(
        "bleepingcomputer",
        "BleepingComputer",
        "https://www.bleepingcomputer.com/",
        "popular_stories",
        ("Popular Stories",),
    ),
)


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "svg"}:
            self._ignored_depth += 1
        if tag == "a" and self._ignored_depth == 0:
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None and self._ignored_depth == 0:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.anchors.append((self._href, _clean_text(" ".join(self._text))))
            self._href = None
            self._text = []
        if tag in {"script", "style", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}
        self.image_urls: list[str] = []
        self.video_urls: list[str] = []
        self._video_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {str(key).lower(): value or "" for key, value in attrs}
        if tag == "video":
            self._video_depth += 1
            _append_unique(self.image_urls, attributes.get("poster", ""))
            _append_unique(self.video_urls, attributes.get("src", ""))
        elif tag == "source" and (
            self._video_depth or attributes.get("type", "").lower().startswith("video/")
        ):
            _append_unique(self.video_urls, attributes.get("src", ""))
        if tag != "meta":
            return
        key = (attributes.get("property") or attributes.get("name") or "").lower()
        content = _clean_text(attributes.get("content", ""))
        if key and content and key not in self.values:
            self.values[key] = content
        if key in {"og:image", "og:image:url", "og:image:secure_url", "twitter:image"}:
            _append_unique(self.image_urls, content)
        if key in {"og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"}:
            _append_unique(self.video_urls, content)

    def handle_endtag(self, tag: str) -> None:
        if tag == "video" and self._video_depth:
            self._video_depth -= 1


class _ArticleParagraphParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self.article_paragraphs: list[str] = []
        self._ignored_depth = 0
        self._article_depth = 0
        self._paragraph_depth = 0
        self._paragraph_in_article = False
        self._paragraph_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "svg", "noscript"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "article":
            self._article_depth += 1
        if tag == "p":
            if self._paragraph_depth == 0:
                self._paragraph_text = []
                self._paragraph_in_article = self._article_depth > 0
            self._paragraph_depth += 1

    def handle_data(self, data: str) -> None:
        if self._paragraph_depth and not self._ignored_depth:
            self._paragraph_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "p" and self._paragraph_depth:
            self._paragraph_depth -= 1
            if self._paragraph_depth == 0:
                paragraph = _clean_text(" ".join(self._paragraph_text))
                if _is_useful_article_paragraph(paragraph):
                    self.paragraphs.append(paragraph)
                    if self._paragraph_in_article:
                        self.article_paragraphs.append(paragraph)
                self._paragraph_text = []
                self._paragraph_in_article = False
        if tag == "article" and self._article_depth:
            self._article_depth -= 1


def parse_ranked_page(source: TechSource, html: str, limit: int = 12) -> list[TechCandidate]:
    if source.strategy == "json_ld_item_list":
        json_ld_candidates = _parse_json_ld_item_list(source, html, limit)
        if json_ld_candidates:
            return json_ld_candidates

    sections: list[str] = []
    if source.strategy == "servethehome_modules":
        module_match = re.search(r'<div\b[^>]*class=["\'][^"\']*\btd_module_flex_6\b', html, flags=re.IGNORECASE)
        if module_match:
            sections.append(html[module_match.start() :])
    else:
        folded = html.casefold()
        positions = [folded.find(label.casefold()) for label in source.ranking_labels]
        ranking_positions = [position for position in positions if position >= 0]
        if ranking_positions:
            sections.append(html[min(ranking_positions) :])
    if not sections:
        sections.extend(re.findall(r"<article\b[^>]*>.*?</article>", html, flags=re.IGNORECASE | re.DOTALL))
    if not sections:
        sections.append(html)

    found: list[TechCandidate] = []
    seen: set[str] = set()
    for section in sections:
        parser = _AnchorParser()
        parser.feed(section)
        for href, title in parser.anchors:
            url = _article_url(source, href, title)
            if not url or url in seen:
                continue
            seen.add(url)
            found.append(
                TechCandidate(
                    source_key=source.key,
                    source_name=source.name,
                    title=title,
                    url=url,
                    ranking_method=source.ranking_method,
                    ranking_position=len(found) + 1,
                    source_url=source.public_url or source.url,
                )
            )
            if len(found) >= limit:
                return found
    return found


def _parse_json_ld_item_list(source: TechSource, html: str, limit: int) -> list[TechCandidate]:
    scripts = re.findall(
        r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for script in scripts:
        try:
            value = json.loads(html_module.unescape(script).strip())
        except json.JSONDecodeError:
            continue
        item_lists = _find_json_ld_item_lists(value)
        for item_list in item_lists:
            candidates: list[TechCandidate] = []
            for raw_item in item_list.get("itemListElement", []):
                if not isinstance(raw_item, dict):
                    continue
                nested = raw_item.get("item") if isinstance(raw_item.get("item"), dict) else raw_item
                title = _clean_text(str(raw_item.get("name") or nested.get("name") or ""))
                href = str(raw_item.get("url") or nested.get("url") or "")
                url = _article_url(source, href, title)
                if not url:
                    continue
                candidates.append(
                    TechCandidate(
                        source_key=source.key,
                        source_name=source.name,
                        title=title,
                        url=url,
                        ranking_method=source.ranking_method,
                        ranking_position=len(candidates) + 1,
                        source_url=source.public_url or source.url,
                    )
                )
                if len(candidates) >= limit:
                    return candidates
            if candidates:
                return candidates
    return []


def _find_json_ld_item_lists(value: object) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, dict):
        if value.get("@type") == "ItemList" and isinstance(value.get("itemListElement"), list):
            found.append(value)
        for child in value.values():
            found.extend(_find_json_ld_item_lists(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_json_ld_item_lists(child))
    return found


def parse_article_metadata(candidate: TechCandidate, html: str) -> TechCandidate:
    parser = _MetadataParser()
    parser.feed(html)
    paragraph_parser = _ArticleParagraphParser()
    paragraph_parser.feed(html)
    title = _clean_article_title(
        parser.values.get("og:title") or parser.values.get("twitter:title") or candidate.title,
        candidate.source_name,
    )
    description = (
        parser.values.get("og:description")
        or parser.values.get("twitter:description")
        or parser.values.get("description")
        or candidate.summary
    )
    paragraphs = (
        paragraph_parser.article_paragraphs
        if len(paragraph_parser.article_paragraphs) >= 2
        else paragraph_parser.paragraphs
    )
    if not paragraphs:
        article_body = _json_ld_article_body(html)
        if article_body:
            paragraphs = [article_body]
    summary = _build_article_summary(description, paragraphs) or description
    published_at = (
        parser.values.get("article:published_time")
        or parser.values.get("datepublished")
        or _json_ld_publication_date(html)
        or candidate.published_at
    )
    image_urls = _absolute_unique_urls(candidate.url, (*candidate.image_urls, *parser.image_urls))
    video_urls = _absolute_unique_urls(candidate.url, (*candidate.video_urls, *parser.video_urls))
    return replace(
        candidate,
        title=title,
        summary=summary,
        published_at=published_at,
        image_urls=image_urls,
        video_urls=video_urls,
    )


def _json_ld_publication_date(html: str) -> str | None:
    scripts = re.findall(
        r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for script in scripts:
        try:
            value = json.loads(html_module.unescape(script).strip())
        except json.JSONDecodeError:
            continue
        published_at = _find_json_ld_publication_date(value)
        if published_at:
            return published_at
    return None


def _json_ld_article_body(html: str) -> str | None:
    scripts = re.findall(
        r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for script in scripts:
        try:
            value = json.loads(html_module.unescape(script).strip())
        except json.JSONDecodeError:
            continue
        article_body = _find_json_ld_string(value, "articlebody")
        if article_body:
            return article_body
    return None


def _find_json_ld_publication_date(value: object) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() == "datepublished" and isinstance(child, str) and child.strip():
                return child.strip()
        for child in value.values():
            published_at = _find_json_ld_publication_date(child)
            if published_at:
                return published_at
    elif isinstance(value, list):
        for child in value:
            published_at = _find_json_ld_publication_date(child)
            if published_at:
                return published_at
    return None


def _find_json_ld_string(value: object, wanted_key: str) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() == wanted_key and isinstance(child, str) and child.strip():
                return child.strip()
        for child in value.values():
            result = _find_json_ld_string(child, wanted_key)
            if result:
                return result
    elif isinstance(value, list):
        for child in value:
            result = _find_json_ld_string(child, wanted_key)
            if result:
                return result
    return None


def _is_useful_article_paragraph(paragraph: str) -> bool:
    if len(paragraph) < 55 and sum(1 for char in paragraph if "\u3400" <= char <= "\u9fff") < 28:
        return False
    folded = paragraph.casefold()
    boilerplate = (
        "subscribe",
        "sign up",
        "newsletter",
        "privacy policy",
        "cookie policy",
        "all rights reserved",
        "advertisement",
        "follow us",
        "disrupt 2026",
        "ticket savings",
        "register here",
        "your next big connection",
        "by submitting your email",
        "相关阅读",
        "扫码关注",
        "责任编辑",
    )
    return not any(marker in folded for marker in boilerplate)


def _build_article_summary(description: str, paragraphs: list[str]) -> str:
    sentences: list[str] = []
    for block in [*paragraphs, description]:
        for sentence in _split_summary_sentences(block):
            normalized = re.sub(r"[^\w\u3400-\u9fff]+", "", sentence).casefold()
            if not normalized or any(
                SequenceMatcher(None, normalized, existing).ratio() >= 0.92
                for existing in (
                    re.sub(r"[^\w\u3400-\u9fff]+", "", item).casefold()
                    for item in sentences
                )
            ):
                continue
            sentences.append(sentence)
    if len(sentences) < 2:
        return sentences[0] if sentences else ""

    selected_indexes = {0}
    caveat_indexes = [
        index
        for index, sentence in enumerate(sentences[1:], start=1)
        if re.search(r"\b(?:but|however|yet|although|despite|no|not)\b|但|不过|然而|尚未|未公布", sentence, re.I)
    ]
    if caveat_indexes:
        selected_indexes.add(caveat_indexes[-1])
    remaining_slots = 4 - len(selected_indexes)
    ranked = sorted(
        (index for index in range(1, len(sentences)) if index not in selected_indexes),
        key=lambda index: (_sentence_information_score(sentences[index]), -index),
        reverse=True,
    )
    selected_indexes.update(ranked[:remaining_slots])
    selected = [sentences[index] for index in sorted(selected_indexes)]
    split_at = max(1, len(selected) // 2)
    return " ".join(selected[:split_at]) + "\n\n" + " ".join(selected[split_at:])


def _clean_article_title(title: str, source_name: str) -> str:
    cleaned = _clean_text(title)
    source = _clean_text(source_name).casefold()
    for separator in (" | ", " — ", " – ", " - "):
        headline, found, suffix = cleaned.rpartition(separator)
        if found and suffix.strip().casefold() == source:
            return headline.strip()
    return cleaned


def _split_summary_sentences(text: str) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    parts = re.split(r"(?<=[。！？!?])\s*|(?<=\.)\s+(?=[A-Z0-9])", normalized)
    return [part.strip() for part in parts if len(part.strip()) >= 20]


def _sentence_information_score(sentence: str) -> int:
    score = min(4, len(re.findall(r"\d+(?:\.\d+)?", sentence)))
    score += 2 * len(
        re.findall(
            r"\b(?:billion|million|parameters?|percent|benchmark|platform|model|chip|funding)\b|"
            r"参数|模型|平台|芯片|融资|性能|百分比",
            sentence,
            flags=re.IGNORECASE,
        )
    )
    if re.search(r"\b(?:but|however|yet|although|despite|no|not)\b|但|不过|然而|尚未|未公布", sentence, re.I):
        score += 3
    return score


def has_substantial_summary(summary: str) -> bool:
    sentences = _split_summary_sentences(summary)
    if len(sentences) < 2:
        return False
    chinese_count = sum(1 for char in summary if "\u3400" <= char <= "\u9fff")
    if chinese_count:
        return chinese_count >= 70
    return len(summary.split()) >= 35


def _append_unique(values: list[str], value: str) -> None:
    value = value.strip()
    if value and value not in values:
        values.append(value)


def _absolute_unique_urls(base_url: str, values: tuple[str, ...]) -> tuple[str, ...]:
    urls: list[str] = []
    for value in values:
        url = urljoin(base_url, value.strip())
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    return tuple(urls)


def render_telegram_message(article: BilingualArticle, index: int, total: int) -> str:
    candidate = article.candidate
    del index, total
    references = _telegram_reference_links(candidate)
    publication_date = format_publication_date(candidate.published_at)
    message = "\n".join(
        [
            f"<b>{html_module.escape(article.zh_title)}</b>",
            html_module.escape(article.zh_summary),
            "",
            f"<b>{html_module.escape(article.en_title)}</b>",
            html_module.escape(article.en_summary),
            "",
            references,
            f"发布时间 / Published: {html_module.escape(publication_date)}",
        ]
    )
    if len(message) > 4096:
        raise ValueError(f"Telegram message exceeds 4096 characters: {len(message)}")
    return message


def _telegram_reference_links(candidate: TechCandidate) -> str:
    source_url = candidate.source_url or _site_homepage(candidate.url)
    raw_links = [
        (candidate.source_name, source_url),
        ("讨论 / Discussion", candidate.discussion_url),
        ("原文 / Source", candidate.url),
    ]
    links: list[str] = []
    seen_urls: set[str] = set()
    for label, url in raw_links:
        url = url.strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        links.append(
            f'<a href="{html_module.escape(url, quote=True)}">{html_module.escape(label)}</a>'
        )
    return " | ".join(links)


def _site_homepage(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def format_publication_date(value: str | None) -> str:
    if not value or not value.strip():
        return "原文未提供 / Not provided by source"
    raw_value = value.strip()
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return raw_value
    if "T" not in raw_value and " " not in raw_value:
        return parsed.date().isoformat()
    rendered = parsed.strftime("%Y-%m-%d %H:%M")
    offset = parsed.utcoffset()
    if offset is None:
        return rendered
    if offset == timedelta(0):
        return f"{rendered} UTC"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"{rendered} UTC{sign}{hours:02d}:{minutes:02d}"


class TechSourceCollector:
    def __init__(self, fetch_text: Callable[[str], str] | None = None) -> None:
        self.fetch_text = fetch_text or _fetch_text

    def collect_source(self, source: TechSource, limit: int = 12) -> list[TechCandidate]:
        if source.strategy == "hacker_news_api":
            return self._collect_hacker_news(source, limit)
        return parse_ranked_page(source, self.fetch_text(source.url), limit=limit)

    def enrich(self, candidate: TechCandidate) -> TechCandidate:
        enriched = parse_article_metadata(candidate, self.fetch_text(candidate.url))
        if not has_substantial_summary(enriched.summary):
            raise ValueError("Article body did not provide a substantial multi-sentence summary.")
        return enriched

    def _collect_hacker_news(self, source: TechSource, limit: int) -> list[TechCandidate]:
        story_ids = json.loads(self.fetch_text(source.url))
        candidates: list[TechCandidate] = []
        for story_id in story_ids:
            item_url = f"https://hacker-news.firebaseio.com/v0/item/{int(story_id)}.json"
            item = json.loads(self.fetch_text(item_url))
            if item.get("type") != "story" or not item.get("title"):
                continue
            article_url = item.get("url") or f"https://news.ycombinator.com/item?id={int(story_id)}"
            candidates.append(
                TechCandidate(
                    source_key=source.key,
                    source_name=source.name,
                    title=_clean_text(str(item["title"])),
                    url=canonicalize_url(str(article_url)),
                    ranking_method=source.ranking_method,
                    ranking_position=len(candidates) + 1,
                    ranking_score=_optional_float(item.get("score")),
                    published_at=(
                        datetime.fromtimestamp(float(item["time"]), tz=timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                        if item.get("time") is not None
                        else None
                    ),
                    source_url=source.public_url or "https://news.ycombinator.com/",
                    discussion_url=f"https://news.ycombinator.com/item?id={int(story_id)}",
                )
            )
            if len(candidates) >= limit:
                break
        return candidates


class TechDigestRunner:
    def __init__(
        self,
        *,
        collector: TechSourceCollector,
        translator: Any,
        archive: Any,
        state: Any,
        media_collector: Any | None = None,
        publisher: Any | None = None,
        core_sources: tuple[TechSource, ...] = CORE_TECH_SOURCES,
        backup_sources: tuple[TechSource, ...] = BACKUP_TECH_SOURCES,
    ) -> None:
        self.collector = collector
        self.translator = translator
        self.archive = archive
        self.state = state
        self.media_collector = media_collector
        self.publisher = publisher
        self.core_sources = core_sources
        self.backup_sources = backup_sources

    def run_once(
        self,
        *,
        run_date: date,
        candidates_per_source: int,
        per_core: int,
        daily_limit: int,
        publish: bool,
        target_channel: str,
    ) -> TechDigestRunResult:
        if publish and not target_channel:
            raise RuntimeError("A Telegram target channel is required for --publish.")
        if publish and self.publisher is None:
            raise RuntimeError("A Telegram publisher is required for --publish.")

        errors: list[str] = []
        already_published = self.state.published_count(run_date)
        remaining_limit = max(0, daily_limit - already_published)
        if remaining_limit == 0:
            manifest_path = self.archive.write_manifest(run_date, [])
            return TechDigestRunResult(
                selected_count=0,
                rendered_count=0,
                published_count=0,
                article_paths=(),
                manifest_path=manifest_path,
                errors=(),
                daily_limit_reached=True,
                published_today=already_published,
                daily_limit=daily_limit,
            )
        core_candidates = self._collect_group(self.core_sources, candidates_per_source, errors)
        backup_candidates = self._collect_group(self.backup_sources, candidates_per_source, errors)
        pool_limit = (
            remaining_limit + len(self.backup_sources) * candidates_per_source
            if remaining_limit
            else 0
        )
        selected_pool = select_digest_candidates(
            core_candidates,
            backup_candidates,
            processed_urls=self.state.processed_urls(),
            per_core=per_core,
            daily_limit=pool_limit,
        )
        selected_count = min(remaining_limit, len(selected_pool))

        prepared: list[tuple[BilingualArticle, str, Path]] = []
        for item in selected_pool:
            if len(prepared) >= remaining_limit:
                break
            try:
                enriched = self.collector.enrich(item)
            except Exception as exc:
                errors.append(f"{item.source_name} article metadata failed ({item.url}): {exc}")
                continue
            try:
                article = self.translator.translate(enriched)
            except Exception as exc:
                errors.append(f"{item.source_name} translation failed ({item.url}): {exc}")
                continue
            if any(
                _titles_describe_same_event(article.en_title, existing.en_title)
                or _titles_describe_same_event(article.zh_title, existing.zh_title)
                for existing, _message, _path in prepared
            ):
                errors.append(f"{item.source_name} bilingual duplicate skipped ({item.url})")
                continue
            daily_index = already_published + len(prepared) + 1
            if self.media_collector is not None:
                try:
                    media = self.media_collector.collect(
                        article.candidate,
                        run_date,
                        daily_index,
                        article.zh_title,
                    )
                    if not any(asset.kind == "image" for asset in media):
                        raise RuntimeError("media collector did not produce a required article image")
                    article = replace(article, media=tuple(media))
                except Exception as exc:
                    errors.append(f"{item.source_name} media collection failed ({item.url}): {exc}")
                    continue
            message = render_telegram_message(article, index=daily_index, total=daily_limit)
            path = self.archive.write_article(
                article,
                message,
                run_date,
                index=daily_index,
            )
            self.state.record(article.candidate, path, status="rendered", run_date=run_date)
            prepared.append((article, message, path))

        published_count = 0
        statuses: dict[str, tuple[str, int | None]] = {}
        if publish:
            for article, message, _path in prepared:
                try:
                    if article.media and hasattr(self.publisher, "send_article"):
                        response = self.publisher.send_article(target_channel, message, article.media)
                    else:
                        response = self.publisher.send_message(target_channel, message)
                    message_id = int(response["message_id"])
                    self.state.mark_published(article.candidate.url, message_id)
                    statuses[canonicalize_url(article.candidate.url)] = ("published", message_id)
                    published_count += 1
                    for media_error in response.get("media_errors", []):
                        errors.append(f"{article.candidate.source_name} optional video failed: {media_error}")
                except Exception as exc:
                    statuses[canonicalize_url(article.candidate.url)] = ("publish_failed", None)
                    errors.append(f"{article.candidate.source_name} Telegram publish failed: {exc}")

        records: list[dict[str, object]] = []
        for article, _message, path in prepared:
            candidate = article.candidate
            status, message_id = statuses.get(
                canonicalize_url(candidate.url),
                ("rendered", None),
            )
            records.append(
                {
                    "source": candidate.source_name,
                    "source_url": candidate.source_url,
                    "url": candidate.url,
                    "discussion_url": candidate.discussion_url,
                    "published_at": candidate.published_at,
                    "ranking_method": candidate.ranking_method,
                    "ranking_position": candidate.ranking_position,
                    "ranking_score": candidate.ranking_score,
                    "local_path": str(path),
                    "media": [
                        {
                            "kind": asset.kind,
                            "path": str(asset.path),
                            "source": asset.source,
                        }
                        for asset in article.media
                    ],
                    "status": status,
                    "telegram_message_id": message_id,
                }
            )
        manifest_path = self.archive.write_manifest(run_date, records)
        return TechDigestRunResult(
            selected_count=selected_count,
            rendered_count=len(prepared),
            published_count=published_count,
            article_paths=tuple(path for _article, _message, path in prepared),
            manifest_path=manifest_path,
            errors=tuple(errors),
            published_today=already_published + published_count,
            daily_limit=daily_limit,
        )

    def _collect_group(
        self,
        sources: tuple[TechSource, ...],
        limit: int,
        errors: list[str],
    ) -> dict[str, list[TechCandidate]]:
        result: dict[str, list[TechCandidate]] = {}
        for source in sources:
            try:
                result[source.key] = self.collector.collect_source(source, limit=limit)
            except Exception as exc:
                result[source.key] = []
                errors.append(f"{source.name} ranking collection failed: {exc}")
        return result


def next_scheduled_run(now: datetime, schedule_at: str, timezone: ZoneInfo) -> datetime:
    match = re.fullmatch(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})", schedule_at.strip())
    if not match:
        raise ValueError("Schedule time must use HH:MM format, for example 20:00.")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        raise ValueError("Schedule time must be a valid 24-hour HH:MM value.")
    localized_now = now.astimezone(timezone)
    target = datetime.combine(
        localized_now.date(),
        datetime_time(hour=hour, minute=minute),
        tzinfo=timezone,
    )
    if target <= localized_now:
        target += timedelta(days=1)
    return target


def format_tech_digest_run_result(result: TechDigestRunResult, *, publish: bool) -> str:
    mode = "published" if publish else "rendered"
    count = result.published_count if publish else result.rendered_count
    if result.daily_limit_reached:
        return (
            f"Tech digest: daily limit reached ({result.published_today}/{result.daily_limit}), "
            f"selected=0, {mode}=0, manifest={result.manifest_path}"
        )
    return (
        f"Tech digest: selected={result.selected_count}, {mode}={count}, "
        f"manifest={result.manifest_path}"
    )


def run_tech_digest_command(args: Any) -> int:
    from .config import load_config, require_bot_token
    from .tech_digest_store import TechDigestArchive, TechDigestState
    from .tech_digest_media import TechDigestMediaCollector
    from .tech_digest_translation import build_tech_digest_translator
    from .telegram_bot import TelegramBotClient

    root = Path(args.root).resolve()
    config = load_config(root)
    digest_config = config.tech_digest
    timezone = ZoneInfo(digest_config.timezone)
    target_channel = (args.channel or digest_config.target_channel or config.telegram.channel).strip()
    publish = bool(args.publish)
    if publish:
        require_bot_token(config)
    translation = digest_config.translation
    translator = build_tech_digest_translator(translation)
    translator.validate()
    publisher = (
        TelegramBotClient(config.telegram.bot_token, proxy_url=config.telegram.proxy_url)
        if publish
        else None
    )
    runner = TechDigestRunner(
        collector=TechSourceCollector(),
        translator=translator,
        archive=TechDigestArchive(digest_config.output_dir),
        state=TechDigestState(root / "data" / "tech_digest.sqlite"),
        media_collector=TechDigestMediaCollector(
            output_dir=digest_config.output_dir,
            proxy_url=config.telegram.proxy_url,
            profile_dir=config.source.profile_dir,
        ),
        publisher=publisher,
    )

    def execute_once() -> None:
        result = runner.run_once(
            run_date=datetime.now(timezone).date(),
            candidates_per_source=digest_config.candidates_per_source,
            per_core=digest_config.articles_per_core,
            daily_limit=digest_config.daily_limit,
            publish=publish,
            target_channel=target_channel,
        )
        print(format_tech_digest_run_result(result, publish=publish))
        for error in result.errors:
            print(f"WARNING: {error}")

    if not args.watch:
        execute_once()
        return 0

    schedule_at = args.at or digest_config.schedule_at
    while True:
        target = next_scheduled_run(datetime.now(timezone), schedule_at, timezone)
        print(f"Next tech digest run: {target.isoformat()}")
        while True:
            remaining = (target - datetime.now(timezone)).total_seconds()
            if remaining <= 0:
                break
            sleep(min(remaining, 60))
        execute_once()


def run_tech_digest_clear_published_command(args: Any) -> int:
    from .tech_digest_store import TechDigestState

    try:
        run_date = date.fromisoformat(str(args.date))
    except ValueError as exc:
        raise ValueError("--date must use YYYY-MM-DD format.") from exc
    if not bool(args.confirm):
        raise RuntimeError(
            "Refusing to clear published state without --confirm. "
            "This command can cause duplicate Telegram publication during testing."
        )

    root = Path(args.root).resolve()
    state = TechDigestState(root / "data" / "tech_digest.sqlite")
    published_count = state.published_count(run_date)
    if published_count == 0:
        print(f"Tech digest published state: no records for {run_date.isoformat()}, deleted=0")
        return 0

    backup_path = state.create_backup(run_date)
    deleted_count = state.clear_published(run_date)
    if deleted_count != published_count:
        raise RuntimeError(
            f"Published state changed while clearing {run_date.isoformat()}: "
            f"expected={published_count}, deleted={deleted_count}, backup={backup_path}"
        )
    print(
        f"Tech digest published state cleared: date={run_date.isoformat()}, "
        f"deleted={deleted_count}, backup={backup_path}"
    )
    return 0


def _fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; tgexporter-tech-digest/0.1; +https://github.com/chengjinxt/tgexporter)",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < 3:
                sleep(attempt)
    raise OSError(f"Failed to fetch {url} after 3 attempts: {last_error}") from last_error


def _article_url(source: TechSource, href: str, title: str) -> str | None:
    title = _clean_text(title)
    if len(title) < 8 or title.casefold() in {"read more", "learn more", "view all", "see all", "更多"}:
        return None
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    absolute = urljoin(source.url, html_module.unescape(href))
    parsed = urlsplit(absolute)
    source_host = (urlsplit(source.url).hostname or "").removeprefix("www.").lower()
    link_host = (parsed.hostname or "").removeprefix("www.").lower()
    if link_host != source_host and not link_host.endswith(f".{source_host}"):
        return None
    path = parsed.path.rstrip("/")
    path_folded = path.casefold()
    if not path or path_folded in {
        "/about",
        "/contact",
        "/search",
        "/newsletters",
        "/privacy",
        "/terms",
    }:
        return None
    excluded_prefixes = (
        "/author/",
        "/authors/",
        "/category/",
        "/tag/",
        "/store/",
        "/subscribe/",
        "/membership",
        "/premium",
        "/my-account",
        "/events/",
        "/newsletters",
    )
    if path_folded.startswith(excluded_prefixes):
        return None
    required_patterns = {
        "techcrunch": r"^/\d{4}/\d{2}/\d{2}/[^/]+$",
        "wired": r"^/story/[^/]+$",
        "engadget": r"^/\d+/[^/]+$",
        "techspot": r"^/news/\d+-.+\.html$",
    }
    required_pattern = required_patterns.get(source.key)
    if required_pattern and not re.fullmatch(required_pattern, path, flags=re.IGNORECASE):
        return None
    if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|css|js|xml|rss)$", path, flags=re.IGNORECASE):
        return None
    return canonicalize_url(absolute)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(value)).strip()


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid"}


def canonicalize_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    port = f":{parsed.port}" if parsed.port else ""
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in TRACKING_QUERY_KEYS
            and not any(key.lower().startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES)
        ],
        doseq=True,
    )
    return urlunsplit(((parsed.scheme or "https").lower(), f"{hostname}{port}", path, query, ""))


def select_digest_candidates(
    core_candidates: dict[str, list[TechCandidate]],
    backup_candidates: dict[str, list[TechCandidate]],
    *,
    processed_urls: set[str] | None = None,
    per_core: int = 2,
    daily_limit: int = 16,
) -> list[TechCandidate]:
    selected: list[TechCandidate] = []
    seen_urls = {canonicalize_url(url) for url in (processed_urls or set())}

    for candidates in core_candidates.values():
        source_count = 0
        for item in candidates:
            if source_count >= per_core or len(selected) >= daily_limit:
                break
            if _append_if_unique(selected, seen_urls, item):
                source_count += 1

    for candidates in backup_candidates.values():
        for item in candidates:
            if len(selected) >= daily_limit:
                return selected
            _append_if_unique(selected, seen_urls, item)
    return selected


def _append_if_unique(
    selected: list[TechCandidate],
    seen_urls: set[str],
    item: TechCandidate,
) -> bool:
    canonical_url = canonicalize_url(item.url)
    if not canonical_url or canonical_url in seen_urls:
        return False
    if any(_titles_describe_same_event(item.title, existing.title) for existing in selected):
        return False
    seen_urls.add(canonical_url)
    selected.append(item)
    return True


def _titles_describe_same_event(first: str, second: str) -> bool:
    first_normalized = _normalize_title(first)
    second_normalized = _normalize_title(second)
    if not first_normalized or not second_normalized:
        return False
    if first_normalized == second_normalized:
        return True
    first_tokens = set(first_normalized.split())
    second_tokens = set(second_normalized.split())
    if min(len(first_tokens), len(second_tokens)) >= 4:
        union = first_tokens | second_tokens
        if union and len(first_tokens & second_tokens) / len(union) >= 0.55:
            return True
    return (
        min(len(first_normalized), len(second_normalized)) >= 15
        and SequenceMatcher(None, first_normalized, second_normalized).ratio() >= 0.84
    )


def _normalize_title(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))
