from __future__ import annotations

import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .filename import (
    extension_from_file_path,
    extension_from_mime,
    media_filename,
    sanitize_title,
)
from .image_search import find_google_image_urls
from .image_filter import is_suitable_article_image
from .link_enricher import ExtraLink, enrich_links, extract_urls, fetch_page_metadata, is_wechat_article_url
from .markdown_renderer import MarkdownRenderer
from .models import ArticleDraft, LinkRef, MediaAsset
from .placeholder_image import write_placeholder_png
from .source_sites import SourceSiteRule, source_rule_for_url
from .state import StateStore
from .telegram_bot import TelegramBotClient, TelegramBotError
from .text_filters import is_channel_promo_line, normalize_text
from .video_cover import create_video_cover
from .web_capture import capture_source_image, extract_source_image_urls

BOT_API_DOWNLOAD_LIMIT = 20_000_000
MARKDOWN_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)]\((?P<url>https?://[^)\s]+)\)")
WECHAT_PAREN_LINK_LINE_RE = re.compile(
    r"^(?P<label>[^\n\[\]()（）]{1,80}?)\s*[（(]\s*(?P<url>https?://mp\.weixin\.qq\.com/[^\s)）]+)\s*[)）]\s*$",
    re.IGNORECASE,
)
WECHAT_BARE_LINK_LINE_RE = re.compile(
    r"^(?P<url>https?://mp\.weixin\.qq\.com/[^\s)）]+)\s*$",
    re.IGNORECASE,
)
BROWSER_IMAGE_SOURCE_DOMAINS = {"fifa.com", "axios.com", "theinformation.com", "reuters.com"}


class TelegramCollector:
    def __init__(
        self,
        client: TelegramBotClient,
        state: StateStore,
        renderer: MarkdownRenderer,
        channel: str,
        timezone: str = "Asia/Shanghai",
        proxy_url: str | None = None,
        source_profile_dir: Path | None = None,
    ) -> None:
        self.client = client
        self.state = state
        self.renderer = renderer
        self.channel = normalize_channel(channel)
        self.timezone = ZoneInfo(timezone)
        self.proxy_url = proxy_url
        self.source_profile_dir = source_profile_dir
        self._warned_source_login_domains: set[str] = set()

    def poll_once(self, timeout: int = 30, latest_only: bool = False) -> list[Path]:
        last_update_id = self.state.get_last_update_id()
        offset = last_update_id + 1 if last_update_id is not None else None
        updates = self.client.get_updates(
            offset=offset,
            timeout=timeout,
            allowed_updates=["channel_post", "edited_channel_post"],
        )
        if latest_only:
            updates = select_latest_updates(updates)
        paths = self.process_updates(updates)
        if updates:
            self.state.set_last_update_id(max(int(item["update_id"]) for item in updates))
        return paths

    def drop_pending(self, timeout: int = 1) -> int:
        updates = self.client.get_updates(
            timeout=timeout,
            allowed_updates=["channel_post", "edited_channel_post"],
        )
        if not updates:
            return 0
        self.state.set_last_update_id(max(int(item["update_id"]) for item in updates))
        return len(updates)

    def listen_forever(self, timeout: int = 30, on_article=None) -> None:
        consecutive_errors = 0
        while True:
            try:
                paths = self.poll_once(timeout=poll_timeout_after_transient_error(timeout, consecutive_errors))
                consecutive_errors = 0
            except TelegramBotError as exc:
                if not is_transient_telegram_error(exc):
                    raise
                consecutive_errors += 1
                delay = min(60, 5 * consecutive_errors)
                next_timeout = poll_timeout_after_transient_error(timeout, consecutive_errors)
                print(
                    f"Telegram getUpdates transient error: {exc}. "
                    f"Retry in {delay}s with poll timeout {next_timeout}s.",
                    file=sys.stderr,
                )
                print(telegram_network_hint(exc, self.proxy_url), file=sys.stderr)
                time.sleep(delay)
                continue
            for path in paths:
                if on_article:
                    on_article(path)
                else:
                    print(f"Rendered: {path}")

    def process_updates(self, updates: list[dict[str, Any]]) -> list[Path]:
        singles: list[dict[str, Any]] = []
        groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)

        for update in updates:
            message = update.get("channel_post") or update.get("edited_channel_post")
            if not message or not self._is_target_channel(message.get("chat", {})):
                continue
            grouped_id = message.get("media_group_id")
            if grouped_id:
                groups[(int(message["chat"]["id"]), str(grouped_id))].append(message)
            else:
                singles.append(message)

        rendered: list[Path] = []
        for message in singles:
            path = self._process_group_safely([message])
            if path:
                rendered.append(path)

        for messages in groups.values():
            path = self._process_group_safely(messages)
            if path:
                rendered.append(path)

        return rendered

    def _process_group_safely(self, messages: list[dict[str, Any]]) -> Path | None:
        try:
            return self._process_message_group(messages)
        except Exception as exc:
            if is_internal_processing_error(exc):
                raise
            message_ids = [str(item.get("message_id", "?")) for item in messages]
            print(f"Skipped Telegram message(s) {', '.join(message_ids)}: {exc}", file=sys.stderr)
            return None

    def _process_message_group(self, messages: list[dict[str, Any]]) -> Path | None:
        messages = sorted(messages, key=lambda item: int(item["message_id"]))
        message_ids = [int(item["message_id"]) for item in messages]
        if any(self.state.message_processed(self.channel, message_id) for message_id in message_ids):
            return None

        first = messages[0]
        published_at = datetime.fromtimestamp(int(first["date"]), UTC).astimezone(self.timezone)
        date_key = published_at.strftime("%Y%m%d")
        raw_text = apply_plain_wechat_markdown_links(collect_text(messages))
        title = derive_title(raw_text, message_ids[0])
        existing_path = self._find_existing_article(date_key, title)
        if existing_path is not None:
            self.state.record_messages_processed(
                self.channel,
                message_ids,
                f"duplicate:{self.channel}:{date_key}:{normalize_text(title)}",
            )
            print(f"Skipped duplicate article: {title} (existing: {existing_path})", file=sys.stderr)
            return None
        daily_index = self.state.next_daily_index(date_key)
        grouped_id = first.get("media_group_id")
        extra_links = collect_entity_links(messages)
        links = enrich_links(
            raw_text,
            fetch_metadata=True,
            extra_urls=[item.url for item in extra_links],
            extra_links=extra_links,
            proxy_url=self.proxy_url,
        )
        links.extend(unique_links(enrich_wechat_article_links(raw_text, proxy_url=self.proxy_url), links))
        text = clean_article_text(raw_text, title, [link.name for link in links])
        article = ArticleDraft(
            source="telegram",
            channel=self.channel,
            message_ids=message_ids,
            grouped_id=str(grouped_id) if grouped_id else None,
            published_at=published_at,
            date_key=date_key,
            daily_index=daily_index,
            title=title,
            text=text,
            links=links,
        )

        date_dir = self.renderer.base_dir / date_key
        date_dir.mkdir(parents=True, exist_ok=True)
        article.media.extend(self._download_message_media(messages, article, date_dir))
        if not any(media.kind == "image" for media in article.media):
            article.media.extend(self._create_video_cover(article, date_dir))
        if not any(media.kind == "image" for media in article.media):
            article.media.extend(self._download_link_images(article, date_dir))
        if not any(media.kind == "image" for media in article.media):
            article.media.extend(self._download_browser_link_images(article, date_dir))
        if not any(media.kind == "image" for media in article.media):
            article.media.extend(self._capture_link_image(article, date_dir))
        if not any(media.kind == "image" for media in article.media):
            article.media.extend(self._download_google_image(article, date_dir))
        if not any(media.kind == "image" for media in article.media):
            article.media.extend(self._create_placeholder_image(article, date_dir))

        article.status = "rendered"
        markdown_path = self.renderer.render(article)
        self.state.record_article(article, markdown_path)
        return markdown_path

    def _find_existing_article(self, date_key: str, title: str) -> Path | None:
        state_path = self.state.article_path_by_title(self.channel, date_key, title)
        if state_path is not None:
            return state_path
        return find_existing_markdown_by_title(self.renderer.base_dir / date_key, title)

    def _download_message_media(
        self,
        messages: list[dict[str, Any]],
        article: ArticleDraft,
        date_dir: Path,
    ) -> list[MediaAsset]:
        assets: list[MediaAsset] = []
        counters = {"image": 0, "video": 0, "file": 0}

        for message in messages:
            for media in iter_message_media(message):
                kind = media.kind
                counters[kind] += 1
                try:
                    file_info = self.client.get_file(media.file_id)
                    extension = extension_from_file_path(
                        file_info.get("file_path"),
                        fallback=extension_from_mime(media.mime_type, media.default_extension),
                    )
                    filename = media_filename(
                        article.date_key,
                        article.daily_index,
                        media.filename_kind,
                        counters[kind],
                        article.title,
                        extension,
                    )
                    destination = date_dir / filename
                    self.client.download_file(file_info["file_path"], destination)
                except TelegramBotError as exc:
                    if kind == "image":
                        print(f"Skipped image in message {message.get('message_id')}: {exc}", file=sys.stderr)
                        continue
                    if "file is too big" not in str(exc).lower():
                        print(f"Skipped {kind} in message {message.get('message_id')}: {exc}", file=sys.stderr)
                    filename = media_filename(
                        article.date_key,
                        article.daily_index,
                        "FILE",
                        counters[kind],
                        article.title,
                        ".txt",
                    )
                    destination = date_dir / filename
                    destination.write_text(
                        "Telegram Bot API 无法下载该媒体文件，原因：文件超过 Bot API getFile 限制。\n"
                        f"媒体类型：{kind}\n"
                        f"原始文件名：{media.title or ''}\n",
                        encoding="utf-8",
                    )
                    kind = "file"
                assets.append(
                    MediaAsset(
                        kind=kind,
                        filename=filename,
                        path=destination,
                        source="telegram",
                        title=media.title,
                    )
                )
        return assets

    def _download_link_images(self, article: ArticleDraft, date_dir: Path) -> list[MediaAsset]:
        seen: set[str] = set()
        for link in article.links:
            for image_url in link_image_candidates(link):
                if image_url in seen:
                    continue
                seen.add(image_url)
                extension = extension_from_url(image_url, ".jpg")
                filename = media_filename(
                    article.date_key,
                    article.daily_index,
                    "PIC",
                    1,
                    article.title,
                    extension,
                )
                destination = date_dir / filename
                if not download_web_file(image_url, destination, proxy_url=self.proxy_url, referer=link.url):
                    continue
                if not is_suitable_article_image(destination):
                    destination.unlink(missing_ok=True)
                    continue
                return [
                    MediaAsset(
                        kind="image",
                        filename=filename,
                        path=destination,
                        source="web_og_image",
                        title=link.name,
                    )
                ]
        return []

    def _capture_link_image(self, article: ArticleDraft, date_dir: Path) -> list[MediaAsset]:
        for link in article.links:
            filename = media_filename(article.date_key, article.daily_index, "PIC", 1, article.title, ".png")
            destination = date_dir / filename
            if not capture_source_image(link.url, destination, profile_dir=self.source_profile_dir):
                self._warn_source_login_if_needed(link)
                continue
            if not is_suitable_article_image(destination):
                destination.unlink(missing_ok=True)
                self._warn_source_login_if_needed(link)
                continue
            return [
                MediaAsset(
                    kind="image",
                    filename=filename,
                    path=destination,
                    source="web_capture",
                    title=f"网页截图：{link.name}",
                )
            ]
        return []

    def _download_browser_link_images(self, article: ArticleDraft, date_dir: Path) -> list[MediaAsset]:
        seen: set[str] = set()
        for link in article.links:
            rule = source_rule_for_url(link.url)
            if rule is None or rule.domain not in BROWSER_IMAGE_SOURCE_DOMAINS:
                continue
            for image_url in extract_source_image_urls(link.url, profile_dir=self.source_profile_dir):
                if image_url in seen:
                    continue
                seen.add(image_url)
                filename = media_filename(
                    article.date_key,
                    article.daily_index,
                    "PIC",
                    1,
                    article.title,
                    extension_from_url(image_url, ".jpg"),
                )
                destination = date_dir / filename
                if not download_web_file(image_url, destination, proxy_url=self.proxy_url, referer=link.url):
                    continue
                if not is_suitable_article_image(destination):
                    destination.unlink(missing_ok=True)
                    continue
                return [
                    MediaAsset(
                        kind="image",
                        filename=filename,
                        path=destination,
                        source="web_browser_image",
                        title=link.name,
                    )
                ]
        return []

    def _warn_source_login_if_needed(self, link: LinkRef) -> None:
        rule = source_rule_for_url(link.url)
        if not should_prompt_source_login(rule):
            return
        if rule.domain in self._warned_source_login_domains:
            return
        self._warned_source_login_domains.add(rule.domain)
        profile_dir = self.source_profile_dir or Path("runtime/source-profile")
        hint = f" {rule.login_hint}" if rule.login_hint else ""
        print(
            "Source site may require login: "
            f"{rule.domain} ({rule.login_requirement}).{hint} "
            f'Login profile: {profile_dir}. Command: tgexporter source-login --url "{link.url}"',
            file=sys.stderr,
        )

    def _download_google_image(self, article: ArticleDraft, date_dir: Path) -> list[MediaAsset]:
        for image_url in find_google_image_urls(article.title, proxy_url=self.proxy_url):
            filename = media_filename(
                article.date_key,
                article.daily_index,
                "PIC",
                1,
                article.title,
                extension_from_url(image_url, ".jpg"),
            )
            destination = date_dir / filename
            if not download_web_file(image_url, destination, proxy_url=self.proxy_url):
                continue
            if not is_suitable_article_image(destination):
                destination.unlink(missing_ok=True)
                continue
            return [
                MediaAsset(
                    kind="image",
                    filename=filename,
                    path=destination,
                    source="google_image_search",
                    title=f"Google 图片搜索：{article.title}",
                )
            ]
        return []

    def _create_placeholder_image(self, article: ArticleDraft, date_dir: Path) -> list[MediaAsset]:
        filename = media_filename(article.date_key, article.daily_index, "PIC", 1, article.title, ".png")
        destination = date_dir / filename
        write_placeholder_png(destination, title=article.title)
        return [
            MediaAsset(
                kind="image",
                filename=filename,
                path=destination,
                source="generated_placeholder",
                title=article.title,
            )
        ]

    def _create_video_cover(self, article: ArticleDraft, date_dir: Path) -> list[MediaAsset]:
        videos = [media for media in article.media if media.kind == "video" and media.path.exists()]
        if not videos:
            return []
        filename = media_filename(article.date_key, article.daily_index, "PIC", 1, article.title, ".jpg")
        destination = date_dir / filename
        if not create_video_cover(videos[0].path, destination, article.title):
            return []
        return [
            MediaAsset(
                kind="image",
                filename=filename,
                path=destination,
                source="video_cover",
                title=f"视频封面：{article.title}",
            )
        ]

    def _is_target_channel(self, chat: dict[str, Any]) -> bool:
        username = normalize_channel(str(chat.get("username", "")))
        chat_id = str(chat.get("id", ""))
        return self.channel in {username, chat_id}


class MessageMedia:
    def __init__(
        self,
        file_id: str,
        kind: str,
        filename_kind: str,
        default_extension: str,
        mime_type: str | None = None,
        title: str | None = None,
    ) -> None:
        self.file_id = file_id
        self.kind = kind
        self.filename_kind = filename_kind
        self.default_extension = default_extension
        self.mime_type = mime_type
        self.title = title


def normalize_channel(channel: str) -> str:
    return channel.strip().removeprefix("@").lower()


def collect_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        value = render_message_text(message)
        value = value.strip()
        if value and value not in parts:
            parts.append(value)
    return "\n\n".join(parts)


def render_message_text(message: dict[str, Any]) -> str:
    if message.get("text"):
        return apply_wechat_markdown_links(message.get("text") or "", message.get("entities") or [])
    return apply_wechat_markdown_links(message.get("caption") or "", message.get("caption_entities") or [])


def apply_wechat_markdown_links(text: str, entities: list[dict[str, Any]]) -> str:
    replacements: list[tuple[int, int, str]] = []
    for entity in entities:
        url = entity.get("url")
        if not url or not is_wechat_article_url(url):
            continue
        if "offset" not in entity or "length" not in entity:
            continue
        start = utf16_index_to_py_index(text, int(entity["offset"]))
        end = utf16_index_to_py_index(text, int(entity["offset"]) + int(entity["length"]))
        label = text[start:end].strip()
        if not label:
            continue
        replacements.append((start, end, f"[{label}]({url})"))
    if not replacements:
        return text

    rendered = text
    last_start = len(text) + 1
    for start, end, value in sorted(replacements, reverse=True):
        if end > last_start:
            continue
        rendered = rendered[:start] + value + rendered[end:]
        last_start = start
    return rendered


def apply_plain_wechat_markdown_links(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        parenthesized = WECHAT_PAREN_LINK_LINE_RE.match(line)
        if parenthesized:
            label = parenthesized.group("label").strip("：: -")
            url = parenthesized.group("url").strip()
            lines.append(f"[{label or '微信文章'}]({url})")
            continue
        bare = WECHAT_BARE_LINK_LINE_RE.match(line)
        if bare:
            url = bare.group("url").strip()
            lines.append(f"[微信文章]({url})")
            continue
        lines.append(raw_line)
    return "\n".join(lines)


def enrich_wechat_article_links(text: str, proxy_url: str | None = None) -> list[LinkRef]:
    found: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for match in MARKDOWN_LINK_RE.finditer(text or ""):
        url = match.group("url").strip()
        if not is_wechat_article_url(url) or url in seen:
            continue
        seen.add(url)
        found.append((url, match.group("label").strip()))
    for url in extract_urls(text or ""):
        if not is_wechat_article_url(url) or url in seen:
            continue
        seen.add(url)
        found.append((url, None))

    links: list[LinkRef] = []
    for url, label in found:
        metadata = fetch_page_metadata(url, proxy_url=proxy_url)
        name = label or metadata.title or urllib.parse.urlparse(url).netloc or "微信文章"
        links.append(
            LinkRef(
                name=name,
                url=url,
                image_url=metadata.image_url,
                image_urls=metadata.image_urls,
            )
        )
    return links


def unique_links(candidates: list[LinkRef], existing: list[LinkRef]) -> list[LinkRef]:
    seen = {link.url for link in existing}
    unique: list[LinkRef] = []
    for link in candidates:
        if link.url in seen:
            continue
        seen.add(link.url)
        unique.append(link)
    return unique


def collect_entity_urls(messages: list[dict[str, Any]]) -> list[str]:
    return [item.url for item in collect_entity_links(messages)]


def collect_entity_links(messages: list[dict[str, Any]]) -> list[ExtraLink]:
    links: list[ExtraLink] = []
    seen: set[str] = set()
    for message in messages:
        entity_sets = [
            (message.get("text") or "", message.get("entities") or []),
            (message.get("caption") or "", message.get("caption_entities") or []),
        ]
        for body, entities in entity_sets:
            for entity in entities:
                url = entity.get("url")
                if url and url not in seen:
                    seen.add(url)
                    links.append(ExtraLink(url=url, name=entity_display_text(body, entity)))
    return links


def entity_display_text(text: str, entity: dict[str, Any]) -> str | None:
    if "offset" not in entity or "length" not in entity:
        return None
    start_units = int(entity["offset"])
    length_units = int(entity["length"])
    start = utf16_index_to_py_index(text, start_units)
    end = utf16_index_to_py_index(text, start_units + length_units)
    value = text[start:end].strip()
    return value or None


def utf16_index_to_py_index(text: str, units: int) -> int:
    count = 0
    for index, char in enumerate(text):
        char_units = len(char.encode("utf-16-le")) // 2
        if count + char_units > units:
            return index
        count += char_units
        if count == units:
            return index + 1
    return len(text)


def clean_article_text(text: str, title: str, reference_names: list[str]) -> str:
    reference_set = {normalize_text(name) for name in reference_names if name}
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        normalized = normalize_text(line)
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if normalized == normalize_text(title):
            continue
        if normalized in reference_set:
            continue
        if is_channel_promo_line(line):
            continue
        lines.append(line)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def find_existing_markdown_by_title(date_dir: Path, title: str) -> Path | None:
    if not date_dir.exists():
        return None
    target = normalize_text(title)
    for path in sorted(date_dir.rglob("*.md")):
        if normalize_text(markdown_title(path)) == target:
            return path
    return None


def markdown_title(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    in_frontmatter = bool(lines and lines[0].strip() == "---")
    if in_frontmatter:
        for line in lines[1:]:
            stripped = line.strip()
            if stripped == "---":
                break
            if stripped.startswith("title:"):
                return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def select_latest_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not updates:
        return []
    latest = max(updates, key=lambda item: int(item["update_id"]))
    latest_message = latest.get("channel_post") or latest.get("edited_channel_post") or {}
    latest_group_id = latest_message.get("media_group_id")
    if not latest_group_id:
        return [latest]
    selected: list[dict[str, Any]] = []
    for update in updates:
        message = update.get("channel_post") or update.get("edited_channel_post") or {}
        if message.get("media_group_id") == latest_group_id:
            selected.append(update)
    return selected


def derive_title(text: str, message_id: int) -> str:
    for line in text.splitlines():
        candidate = line.strip().strip("#>-* ")
        if not candidate or candidate.startswith("http://") or candidate.startswith("https://"):
            continue
        candidate = re.sub(r"\s+", " ", candidate)
        return sanitize_title(candidate, max_length=70)
    return f"Telegram消息_{message_id}"


def iter_message_media(message: dict[str, Any]) -> list[MessageMedia]:
    media: list[MessageMedia] = []
    photos = message.get("photo") or []
    if photos:
        largest = max(photos, key=lambda item: int(item.get("file_size", 0)))
        media.append(
            MessageMedia(
                file_id=largest["file_id"],
                kind="image",
                filename_kind="PIC",
                default_extension=".jpg",
                title="Telegram图片",
            )
        )

    video = message.get("video")
    if video:
        video_file = choose_video_variant(video)
        media.append(
            MessageMedia(
                file_id=video_file["file_id"],
                kind="video",
                filename_kind="VID",
                default_extension=".mp4",
                mime_type=video.get("mime_type"),
                title=video.get("file_name") or "Telegram视频",
            )
        )

    animation = message.get("animation")
    if animation:
        animation_file = choose_video_variant(animation)
        media.append(
            MessageMedia(
                file_id=animation_file["file_id"],
                kind="video",
                filename_kind="VID",
                default_extension=".mp4",
                mime_type=animation.get("mime_type"),
                title=animation.get("file_name") or "Telegram动画",
            )
        )

    document = message.get("document")
    if document:
        mime_type = document.get("mime_type", "")
        if mime_type.startswith("image/"):
            media.append(
                MessageMedia(
                    file_id=document["file_id"],
                    kind="image",
                    filename_kind="PIC",
                    default_extension=extension_from_mime(mime_type, ".jpg"),
                    mime_type=mime_type,
                    title=document.get("file_name") or "Telegram图片",
                )
            )
        elif mime_type.startswith("video/"):
            media.append(
                MessageMedia(
                    file_id=document["file_id"],
                    kind="video",
                    filename_kind="VID",
                    default_extension=extension_from_mime(mime_type, ".mp4"),
                    mime_type=mime_type,
                    title=document.get("file_name") or "Telegram视频",
                )
            )
        else:
            media.append(
                MessageMedia(
                    file_id=document["file_id"],
                    kind="file",
                    filename_kind="FILE",
                    default_extension=".bin",
                    mime_type=mime_type,
                    title=document.get("file_name") or "Telegram附件",
                )
            )
    return media


def choose_video_variant(video: dict[str, Any]) -> dict[str, Any]:
    original = {
        "file_id": video["file_id"],
        "file_size": int(video.get("file_size") or 0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "codec": video.get("codec", ""),
    }
    variants = [original]
    variants.extend(video.get("qualities") or [])
    downloadable = [item for item in variants if 0 < int(item.get("file_size") or 0) <= BOT_API_DOWNLOAD_LIMIT]
    if downloadable:
        h264 = [item for item in downloadable if str(item.get("codec", "")).lower() == "h264"]
        pool = h264 or downloadable
        return max(pool, key=lambda item: (int(item.get("width") or 0) * int(item.get("height") or 0), int(item.get("file_size") or 0)))
    return min(variants, key=lambda item: int(item.get("file_size") or BOT_API_DOWNLOAD_LIMIT + 1))


def link_image_candidates(link: LinkRef) -> list[str]:
    candidates: list[str] = []
    if link.image_url:
        candidates.append(link.image_url)
    for image_url in link.image_urls:
        if image_url not in candidates:
            candidates.append(image_url)
    return candidates


def extension_from_url(url: str, fallback: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix
    if suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return suffix
    return fallback


def download_web_file(url: str, destination: Path, proxy_url: str | None = None, referer: str | None = None) -> bool:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "image/jpeg,image/png,image/*;q=0.8,*/*;q=0.5",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    try:
        from .link_enricher import build_opener

        with build_opener(proxy_url).open(request, timeout=20) as response:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as file:
                shutil.copyfileobj(response, file)
        return True
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def is_transient_telegram_error(exc: TelegramBotError) -> bool:
    text = str(exc).lower()
    transient_markers = [
        "timed out",
        "timeout",
        "urlopen error",
        "ssl",
        "handshake",
        "connection reset",
        "connection aborted",
        "connection refused",
        "remote end closed",
        "temporarily unavailable",
        "service unavailable",
        "gateway timeout",
        "bad gateway",
        "too many requests",
    ]
    permanent_markers = [
        "unauthorized",
        "not found",
        "invalid token",
        "forbidden",
    ]
    return any(marker in text for marker in transient_markers) and not any(
        marker in text for marker in permanent_markers
    )


def poll_timeout_after_transient_error(timeout: int, consecutive_errors: int) -> int:
    if consecutive_errors <= 0:
        return timeout
    return min(timeout, 5)


def telegram_network_hint(exc: TelegramBotError, proxy_url: str | None = None) -> str:
    text = str(exc).lower()
    proxy = proxy_url or "direct"
    if any(marker in text for marker in ("ssl", "handshake", "unexpected_eof", "eof occurred", "read operation timed out")):
        reason = "Telegram Bot API 的 HTTPS 连接被代理或网络中途断开"
    elif any(marker in text for marker in ("connection refused", "connection reset", "connection aborted")):
        reason = "代理端口或到 Telegram Bot API 的连接被拒绝/重置"
    elif "timed out" in text or "timeout" in text:
        reason = "Telegram Bot API 请求超时"
    else:
        reason = "Telegram Bot API 网络请求失败"
    return (
        f"排查提示：{reason}；当前代理为 {proxy}。请确认代理软件正在运行，"
        "HTTP 代理端口可用，规则/全局模式允许 api.telegram.org。"
        "可运行 `tgexporter doctor` 做短连接检测；如果短连接正常但 listen 仍反复报错，"
        "程序会自动改用 5 秒短轮询重试，通常是代理长连接不稳定。"
    )


def should_prompt_source_login(rule: SourceSiteRule | None) -> bool:
    if rule is None:
        return False
    requirement = rule.login_requirement.strip()
    if requirement in {"无需登录", "通常无需登录"}:
        return False
    return any(marker in requirement for marker in ("登录", "订阅", "需要"))


def is_internal_processing_error(exc: Exception) -> bool:
    return isinstance(exc, (AssertionError, AttributeError, NameError, TypeError, UnboundLocalError))
