from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request

from .link_enricher import build_opener

GOOGLE_IMAGE_RE = re.compile(r"https://encrypted-tbn\d\.gstatic\.com/images\?q=tbn:[^\"'\\\s<>]+")
GOOGLE_IMGURL_RE = re.compile(r"[?&](?:imgurl|mediaurl)=([^&\"'<>]+)", re.IGNORECASE)
GOOGLE_ORIGINAL_IMAGE_RE = re.compile(
    r"https?://[^\"'\\<>\s]+?\.(?:png|jpe?g|webp)(?:\?[^\"'\\<>\s]*)?",
    re.IGNORECASE,
)
BING_IMAGE_META_RE = re.compile(r'm="(\{.*?\})"')


def find_google_image_url(query: str, proxy_url: str | None = None, timeout: int = 12) -> str | None:
    urls = find_google_image_urls(query, proxy_url=proxy_url, timeout=timeout, limit=1)
    return urls[0] if urls else None


def find_google_image_urls(
    query: str,
    proxy_url: str | None = None,
    timeout: int = 12,
    limit: int = 8,
) -> list[str]:
    search_url = "https://www.google.com/search?" + urllib.parse.urlencode(
        {"tbm": "isch", "q": query, "hl": "zh-CN"}
    )
    request = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": "Mozilla/5.0 tgexporter/0.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            text = response.read(512_000).decode("utf-8", errors="ignore")
    except OSError:
        return []
    urls: list[str] = []
    for url in extract_google_image_urls(text):
        if url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls


def find_google_image_urls_via_browser(
    query: str,
    profile_dir=None,
    timeout_ms: int = 30000,
    limit: int = 8,
) -> list[str]:
    try:
        from playwright.sync_api import sync_playwright
        from .web_capture import create_browser_context, dismiss_common_overlays
    except ImportError:
        return []


def find_bing_image_urls(
    query: str,
    required_terms: list[str] | None = None,
    timeout: int = 12,
    limit: int = 8,
) -> list[str]:
    search_url = "https://www.bing.com/images/search?" + urllib.parse.urlencode(
        {"q": query, "setlang": "zh-CN", "cc": "HK"}
    )
    request = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read(1_000_000).decode("utf-8", errors="ignore")
    except OSError:
        return []
    urls: list[str] = []
    required = [term.lower() for term in required_terms or [] if term]
    for match in BING_IMAGE_META_RE.finditer(text):
        try:
            data = json.loads(html.unescape(match.group(1)))
        except (TypeError, json.JSONDecodeError):
            continue
        image_url = clean_google_image_url(str(data.get("murl") or data.get("turl") or ""))
        if not image_url or should_skip_google_image_url(image_url):
            continue
        candidate_text = " ".join(str(data.get(key) or "") for key in ("t", "purl", "murl")).lower()
        if required and not any(term in candidate_text for term in required):
            continue
        if image_url not in urls:
            urls.append(image_url)
        if len(urls) >= limit:
            break
    return urls

    search_url = "https://www.google.com/search?" + urllib.parse.urlencode(
        {"tbm": "isch", "q": query, "hl": "zh-CN", "safe": "off"}
    )
    try:
        with sync_playwright() as playwright:
            context, close_context = create_browser_context(playwright, headless=True, profile_dir=profile_dir)
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined })")
            page.set_default_timeout(timeout_ms)
            try:
                page.goto(search_url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(2500)
                dismiss_common_overlays(page)
                page.evaluate("window.scrollTo(0, 350)")
                page.wait_for_timeout(1000)
                visible_urls = page.evaluate(GOOGLE_VISIBLE_IMAGE_SCRIPT, limit * 2)
                urls = extract_google_image_urls(page.content())
                for raw_url in visible_urls:
                    url = clean_google_image_url(str(raw_url))
                    if url and not should_skip_google_image_url(url) and url not in urls:
                        urls.append(url)
                    if len(urls) >= limit:
                        break
                return urls[:limit]
            finally:
                close_context()
    except Exception:
        return []


def extract_google_image_urls(text: str) -> list[str]:
    normalized = decode_google_escaped_text(text)
    urls: list[str] = []
    for regex in (GOOGLE_IMGURL_RE, GOOGLE_ORIGINAL_IMAGE_RE, GOOGLE_IMAGE_RE):
        for match in regex.finditer(normalized):
            raw = match.group(1) if regex is GOOGLE_IMGURL_RE else match.group(0)
            url = clean_google_image_url(raw)
            if not url or should_skip_google_image_url(url):
                continue
            if url not in urls:
                urls.append(url)
    return urls


def decode_google_escaped_text(text: str) -> str:
    return (
        html.unescape(text)
        .replace("\\u003d", "=")
        .replace("\\u0026", "&")
        .replace("\\x3d", "=")
        .replace("\\x26", "&")
        .replace("\\/", "/")
    )


def clean_google_image_url(url: str) -> str:
    url = urllib.parse.unquote(html.unescape(url)).strip()
    url = url.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
    url = url.rstrip(".,);]}\"'")
    return url if url.startswith(("http://", "https://")) else ""


def should_skip_google_image_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if "google" in host and "gstatic.com" not in host:
        return True
    if any(token in path for token in ("/logo", "sprite", "favicon", "icon", "avatar", "qrcode")):
        return True
    return False


GOOGLE_VISIBLE_IMAGE_SCRIPT = """
(limit) => {
    const ignored = /(logo|favicon|avatar|sprite|qrcode|icon|googlelogo|profile)/i;
    const urls = [];
    for (const img of Array.from(document.images)) {
        const rect = img.getBoundingClientRect();
        const src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
        const text = `${src} ${img.alt || ''} ${img.className || ''} ${img.id || ''}`;
        const width = img.naturalWidth || rect.width;
        const height = img.naturalHeight || rect.height;
        if (!src || src.startsWith('data:')) continue;
        if (ignored.test(text)) continue;
        if (rect.width < 80 || rect.height < 60 || width < 120 || height < 90) continue;
        if (rect.bottom < 120 || rect.top > window.innerHeight + 500) continue;
        if (!urls.includes(src)) urls.push(src);
        if (urls.length >= (limit || 8)) break;
    }
    return urls;
}
"""
