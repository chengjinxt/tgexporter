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
    r"<meta\s+[^>]*(?:property|name)=[\"'](?P<name>og:title|twitter:title|og:image|twitter:image)[\"'][^>]*>",
    re.IGNORECASE,
)
CONTENT_RE = re.compile(r"content=[\"'](?P<content>.*?)[\"']", re.IGNORECASE | re.DOTALL)
IGNORED_DOMAINS = {"mp.weixin.qq.com", "t.me", "telegram.me"}


@dataclass(frozen=True)
class PageMetadata:
    title: str | None = None
    image_url: str | None = None


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
        links.append(LinkRef(name=clean_text(name), url=url, image_url=metadata.image_url))
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
    image_url = _find_meta(html_text, ("og:image", "twitter:image"))
    if image_url:
        image_url = urllib.parse.urljoin(url, image_url)
    return PageMetadata(title=title, image_url=image_url)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def should_ignore_url(url: str) -> bool:
    hostname = urllib.parse.urlparse(url).hostname or ""
    hostname = hostname.lower()
    return hostname in IGNORED_DOMAINS


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
