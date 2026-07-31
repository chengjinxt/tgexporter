from __future__ import annotations

import html
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .models import LinkRef

URL_RE = re.compile(r"https?://[^\s<>()\"'，。；、！？]+", re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
META_RE = re.compile(
    r"<meta\s+[^>]*(?:property|name)=[\"'](?P<name>og:title|twitter:title|og:image|og:image:secure_url|twitter:image|twitter:image:src)[\"'][^>]*>",
    re.IGNORECASE,
)
CONTENT_RE = re.compile(r"content=[\"'](?P<content>.*?)[\"']", re.IGNORECASE | re.DOTALL)
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
SRCSET_SPLIT_RE = re.compile(r"\s*,\s*")
IGNORED_DOMAINS = {"mp.weixin.qq.com", "t.me", "telegram.me"}
WECHAT_ARTICLE_DOMAINS = {"mp.weixin.qq.com"}
IGNORED_IMAGE_KEYWORDS = (
    "avatar",
    "head.jpg",
    "icon",
    "images/v2/t.png",
    "logo",
    "qbitai_icon",
    "qrcode",
    "qr-code",
    "wechat",
    "weixin",
    "wx_qrcode",
)
IMAGE_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-url")
IMAGE_META_NAMES = ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src")


@dataclass(frozen=True)
class PageMetadata:
    title: str | None = None
    image_url: str | None = None
    image_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtraLink:
    url: str
    name: str | None = None


def extract_urls(text: str) -> list[str]:
    found: list[str] = []
    for match in URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;，。；)")
        if url not in found:
            found.append(url)
    return found


def enrich_links(
    text: str,
    fetch_metadata: bool = True,
    extra_urls: list[str] | None = None,
    extra_links: list[ExtraLink] | None = None,
    proxy_url: str | None = None,
) -> list[LinkRef]:
    links: list[LinkRef] = []
    link_names: dict[str, str] = {}
    urls = extract_urls(text)
    for item in extra_links or []:
        if item.url not in urls:
            urls.append(item.url)
        if item.name:
            link_names[item.url] = item.name
    for url in extra_urls or []:
        if url not in urls:
            urls.append(url)
    for url in urls:
        if should_ignore_url(url):
            continue
        metadata = fetch_page_metadata(url, proxy_url=proxy_url) if fetch_metadata else PageMetadata()
        name = link_names.get(url) or metadata.title or urllib.parse.urlparse(url).netloc or url
        links.append(
            LinkRef(
                name=clean_text(name),
                url=url,
                image_url=metadata.image_url,
                image_urls=metadata.image_urls,
            )
        )
    return links


def fetch_page_metadata(url: str, timeout: int = 8, proxy_url: str | None = None) -> PageMetadata:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 tgexporter/0.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type and "application/xhtml" not in content_type:
                return PageMetadata()
            raw = response.read(512_000)
            charset = response.headers.get_content_charset() or "utf-8"
    except (urllib.error.URLError, TimeoutError, ValueError):
        return PageMetadata()

    html_text = raw.decode(charset, errors="ignore")
    title = _find_meta(html_text, ("og:title", "twitter:title")) or _find_title(html_text)
    image_urls = find_page_image_urls(html_text, url)
    image_url = image_urls[0] if image_urls else None
    return PageMetadata(title=title, image_url=image_url, image_urls=image_urls)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def should_ignore_url(url: str) -> bool:
    hostname = urllib.parse.urlparse(url).hostname or ""
    hostname = hostname.lower()
    return hostname in IGNORED_DOMAINS


def is_wechat_article_url(url: str) -> bool:
    hostname = urllib.parse.urlparse(url).hostname or ""
    return hostname.lower() in WECHAT_ARTICLE_DOMAINS


def build_opener(proxy_url: str | None = None) -> urllib.request.OpenerDirector:
    if proxy_url:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return urllib.request.build_opener()


def _find_title(html_text: str) -> str | None:
    match = TITLE_RE.search(html_text)
    if not match:
        return None
    return clean_text(match.group(1))


def _find_meta(html_text: str, names: tuple[str, ...]) -> str | None:
    for meta in META_RE.finditer(html_text):
        name = meta.group("name").lower()
        if name not in names:
            continue
        content_match = CONTENT_RE.search(meta.group(0))
        if content_match:
            return clean_text(content_match.group("content"))
    return None


def find_page_image_urls(html_text: str, base_url: str, limit: int = 12) -> tuple[str, ...]:
    candidates: list[str] = []
    for value in _find_meta_values(html_text, IMAGE_META_NAMES):
        append_image_candidate(candidates, value, base_url)
    for tag in IMG_TAG_RE.finditer(html_text):
        tag_text = tag.group(0)
        for attr in IMAGE_ATTRS:
            value = _find_attr(tag_text, attr)
            if value:
                append_image_candidate(candidates, value, base_url)
                break
        srcset = _find_attr(tag_text, "srcset") or _find_attr(tag_text, "data-srcset")
        if srcset:
            for part in SRCSET_SPLIT_RE.split(srcset):
                append_image_candidate(candidates, part.split()[0], base_url)
                if len(candidates) >= limit:
                    break
        if len(candidates) >= limit:
            break
    return tuple(candidates[:limit])


def append_image_candidate(candidates: list[str], value: str, base_url: str) -> None:
    url = normalize_image_url(value, base_url)
    if url and url not in candidates:
        candidates.append(url)


def normalize_image_url(value: str, base_url: str) -> str | None:
    value = html.unescape(value or "").strip()
    if not value or value.startswith("data:"):
        return None
    url = urllib.parse.urljoin(base_url, value)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    lowered = urllib.parse.unquote(url).lower()
    if any(keyword in lowered for keyword in IGNORED_IMAGE_KEYWORDS):
        return None
    suffix = path_like_suffix(parsed.path)
    if suffix and suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        return None
    return url


def _find_meta_values(html_text: str, names: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for meta in META_RE.finditer(html_text):
        name = meta.group("name").lower()
        if name not in names:
            continue
        content_match = CONTENT_RE.search(meta.group(0))
        if content_match:
            value = clean_text(content_match.group("content"))
            if value and value not in values:
                values.append(value)
    return values


def _find_attr(tag_text: str, attr: str) -> str | None:
    match = re.search(rf"\b{re.escape(attr)}\s*=\s*([\"'])(?P<value>.*?)\1", tag_text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return match.group("value")


def path_like_suffix(path: str) -> str:
    suffix_match = re.search(r"(\.[a-zA-Z0-9]+)$", path)
    return suffix_match.group(1).lower() if suffix_match else ""
