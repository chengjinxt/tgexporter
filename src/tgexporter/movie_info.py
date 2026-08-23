from __future__ import annotations

import html
import json
import base64
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .link_enricher import build_opener, clean_text, fetch_page_metadata
from .models import LinkRef

MOVIE_SOURCE_DOMAINS = (
    "tmioe.com",
    "douban.com",
    "maoyan.com",
    "imdb.com",
    "rottentomatoes.com",
    "netflix.com",
    "wikipedia.org",
)
MOVIE_TITLE_PREFIX_RE = re.compile(r"^\s*名称\s*[:：]\s*")
MOVIE_YEAR_RE = re.compile(r"[（(](?P<year>19\d{2}|20\d{2})[)）]")
MOVIE_TAG_RE = re.compile(r"【[^】]+】")
JSON_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(?P<json>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
META_DESC_RE = re.compile(
    r"<meta\s+[^>]*(?:property|name)=[\"'](?P<name>og:description|description|twitter:description)[\"'][^>]*>",
    re.IGNORECASE | re.DOTALL,
)
CONTENT_RE = re.compile(r"content=[\"'](?P<content>.*?)[\"']", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class MovieTitle:
    raw: str
    name: str
    year: str | None = None


@dataclass(frozen=True)
class MovieInfo:
    name: str
    source_name: str
    source_url: str
    original_name: str | None = None
    release_date: str | None = None
    genres: tuple[str, ...] = ()
    cast: tuple[str, ...] = ()
    overview: str | None = None
    image_urls: tuple[str, ...] = ()


def parse_movie_title(title: str) -> MovieTitle:
    value = MOVIE_TITLE_PREFIX_RE.sub("", title or "").strip()
    year_match = MOVIE_YEAR_RE.search(value)
    year = year_match.group("year") if year_match else None
    value = MOVIE_YEAR_RE.sub("", value)
    value = MOVIE_TAG_RE.sub("", value)
    value = re.split(r"\s{2,}|[|｜]", value, maxsplit=1)[0]
    value = re.sub(r"\s+", " ", value).strip(" ：:-")
    return MovieTitle(raw=title, name=value or title.strip(), year=year)


def fetch_movie_info(
    title: str,
    proxy_url: str | None = None,
    limit: int = 6,
    context_text: str | None = None,
) -> MovieInfo | None:
    movie_title = parse_movie_title(title)
    for url in movie_source_candidates(movie_title, proxy_url=proxy_url, limit=limit, context_text=context_text):
        info = fetch_movie_page_info(url, movie_title, proxy_url=proxy_url)
        if info:
            return info
    return None


def movie_source_candidates(
    movie_title: MovieTitle,
    proxy_url: str | None = None,
    limit: int = 6,
    context_text: str | None = None,
) -> list[str]:
    candidates: list[str] = []
    queries = [
        f"{movie_title.name} {movie_title.year or ''} 电影 site:tmioe.com/movie",
        f"{movie_title.name} {movie_title.year or ''} 电影 site:douban.com/movie",
        f"{movie_title.name} {movie_title.year or ''} IMDb",
        f"{movie_title.name} {movie_title.year or ''} Rotten Tomatoes",
        f"{movie_title.name} {movie_title.year or ''} 电影 演员 上映时间",
    ]
    queries.extend(movie_context_search_queries(context_text))
    for query in queries:
        urls = [
            *search_movie_source_urls(query, proxy_url=proxy_url),
            *search_google_movie_source_urls(query, proxy_url=proxy_url),
        ]
        for url in urls:
            if url not in candidates:
                candidates.append(url)
            if len(candidates) >= limit:
                return candidates
    return candidates


def movie_context_search_queries(text: str | None) -> list[str]:
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    descriptions = []
    for line in lines:
        if line.startswith(("描述：", "简介：", "剧情：")):
            descriptions.append(line.split("：", 1)[1])
    if not descriptions:
        return []
    snippet = re.sub(r"https?://\S+", "", descriptions[0])
    snippet = re.sub(r"[#【】\[\]（）()]", " ", snippet)
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if len(snippet) > 80:
        snippet = snippet[:80]
    if not snippet:
        return []
    return [
        f"{snippet} 电影 IMDb",
        f"{snippet} 电影 豆瓣",
    ]


def search_movie_source_urls(query: str, proxy_url: str | None = None, timeout: int = 10) -> list[str]:
    urls = search_bing_rss_movie_source_urls(query, proxy_url=proxy_url, timeout=timeout)
    if urls:
        return urls
    search_url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "mkt": "zh-CN"})
    request = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": "Mozilla/5.0 tgexporter/0.1",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            text = response.read(512_000).decode(response.headers.get_content_charset() or "utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    return extract_movie_source_urls(text)


def search_bing_rss_movie_source_urls(query: str, proxy_url: str | None = None, timeout: int = 10) -> list[str]:
    search_url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "mkt": "zh-CN", "format": "rss"}
    )
    request = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": "Mozilla/5.0 tgexporter/0.1",
            "Accept": "application/rss+xml,text/xml,application/xml,text/html",
        },
    )
    try:
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            text = response.read(512_000).decode(response.headers.get_content_charset() or "utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    return extract_movie_source_urls(text)


def search_google_movie_source_urls(query: str, proxy_url: str | None = None, timeout: int = 10) -> list[str]:
    search_url = "https://www.google.com/search?" + urllib.parse.urlencode({"q": query, "hl": "zh-CN"})
    request = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    try:
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            text = response.read(512_000).decode(response.headers.get_content_charset() or "utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    return extract_movie_source_urls(text)


def extract_movie_source_urls(text: str) -> list[str]:
    urls: list[str] = []
    normalized_text = html.unescape(text or "").replace("\\/", "/")
    for raw in re.findall(r"https?://[^\s\"'<>]+", normalized_text):
        url = clean_search_url(raw)
        if not url:
            continue
        hostname = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.").lower()
        if not any(hostname == domain or hostname.endswith("." + domain) for domain in MOVIE_SOURCE_DOMAINS):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def clean_search_url(url: str) -> str | None:
    url = html.unescape(url).rstrip(".,;，。；)")
    parsed = urllib.parse.urlparse(url)
    if "bing.com" in (parsed.hostname or ""):
        params = urllib.parse.parse_qs(parsed.query)
        redirect = params.get("u") or params.get("url")
        if redirect:
            url = redirect[0]
            if url.startswith("a1"):
                try:
                    encoded = url[2:]
                    padding = "=" * (-len(encoded) % 4)
                    url = base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8", errors="ignore")
                except Exception:
                    return None
            else:
                url = urllib.parse.unquote(url)
            parsed = urllib.parse.urlparse(url)
    if "google." in (parsed.hostname or ""):
        params = urllib.parse.parse_qs(parsed.query)
        redirect = params.get("q") or params.get("url")
        if redirect:
            url = urllib.parse.unquote(redirect[0])
            parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def fetch_movie_page_info(url: str, expected: MovieTitle, proxy_url: str | None = None, timeout: int = 10) -> MovieInfo | None:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 tgexporter/0.1",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    try:
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            text = response.read(1_500_000).decode(response.headers.get_content_charset() or "utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, ValueError):
        metadata = fetch_page_metadata(url, proxy_url=proxy_url)
        if not metadata.title and not metadata.image_urls:
            return None
        return MovieInfo(
            name=metadata.title or expected.name,
            source_name=movie_source_name(url),
            source_url=url,
            image_urls=metadata.image_urls,
        )

    info = movie_info_from_json_ld(text, url)
    metadata = fetch_page_metadata_from_html(text, url)
    if info:
        image_urls = tuple(dict.fromkeys([*info.image_urls, *metadata.image_urls]))
        return MovieInfo(
            name=info.name or metadata.title or expected.name,
            source_name=movie_source_name(url),
            source_url=url,
            original_name=info.original_name,
            release_date=info.release_date,
            genres=info.genres,
            cast=info.cast,
            overview=info.overview or metadata.overview,
            image_urls=image_urls,
        )
    if metadata.title or metadata.overview or metadata.image_urls:
        return MovieInfo(
            name=metadata.title or expected.name,
            source_name=movie_source_name(url),
            source_url=url,
            overview=metadata.overview,
            image_urls=metadata.image_urls,
        )
    return None


@dataclass(frozen=True)
class MoviePageMetadata:
    title: str | None = None
    overview: str | None = None
    image_urls: tuple[str, ...] = ()


def fetch_page_metadata_from_html(text: str, url: str) -> MoviePageMetadata:
    from .link_enricher import find_page_image_urls

    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    title = clean_text(title_match.group(1)) if title_match else None
    overview = None
    for meta in META_DESC_RE.finditer(text):
        content_match = CONTENT_RE.search(meta.group(0))
        if content_match:
            overview = clean_text(content_match.group("content"))
            break
    return MoviePageMetadata(title=title, overview=overview, image_urls=find_page_image_urls(text, url, limit=6))


def movie_info_from_json_ld(text: str, url: str) -> MovieInfo | None:
    for match in JSON_LD_RE.finditer(text):
        data = parse_json_ld(match.group("json"))
        for item in iter_json_ld_items(data):
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if not any(str(value).lower() in {"movie", "tvseries", "tvseason"} for value in types):
                continue
            name = clean_text(str(item.get("name") or ""))
            if not name:
                continue
            return MovieInfo(
                name=name,
                source_name=movie_source_name(url),
                source_url=url,
                original_name=clean_optional(item.get("alternateName")),
                release_date=clean_optional(item.get("datePublished")),
                genres=tuple(clean_sequence(item.get("genre"))),
                cast=tuple(clean_people(item.get("actor") or item.get("actors"))[:8]),
                overview=clean_optional(item.get("description")),
                image_urls=tuple(clean_url_sequence(item.get("image"))),
            )
    return None


def parse_json_ld(raw: str):
    try:
        return json.loads(html.unescape(raw).strip())
    except json.JSONDecodeError:
        return None


def iter_json_ld_items(data):
    if isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            yield from iter_json_ld_items(graph)
        yield data
    elif isinstance(data, list):
        for item in data:
            yield from iter_json_ld_items(item)


def clean_optional(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("name") or value.get("@id")
    if isinstance(value, list):
        values = clean_sequence(value)
        return values[0] if values else None
    text = clean_text(str(value))
    return text or None


def clean_sequence(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [clean_text(item) for item in re.split(r"[,/、，]", value) if clean_text(item)]
    if isinstance(value, dict):
        item = clean_optional(value)
        return [item] if item else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            for cleaned in clean_sequence(item):
                if cleaned and cleaned not in result:
                    result.append(cleaned)
        return result
    text = clean_text(str(value))
    return [text] if text else []


def clean_url_sequence(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = clean_text(value)
        return [text] if text else []
    if isinstance(value, dict):
        text = clean_optional(value.get("url") or value.get("@id") or value.get("contentUrl"))
        return [text] if text else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            for cleaned in clean_url_sequence(item):
                if cleaned and cleaned not in result:
                    result.append(cleaned)
        return result
    text = clean_text(str(value))
    return [text] if text else []


def clean_people(value) -> list[str]:
    return clean_sequence(value)


def movie_source_name(url: str) -> str:
    hostname = (urllib.parse.urlparse(url).hostname or "").removeprefix("www.").lower()
    if hostname.endswith("tmioe.com"):
        return "TMIOE"
    if hostname.endswith("douban.com"):
        return "豆瓣电影"
    if hostname.endswith("maoyan.com"):
        return "猫眼电影"
    if hostname.endswith("imdb.com"):
        return "IMDb"
    if hostname.endswith("rottentomatoes.com"):
        return "Rotten Tomatoes"
    return hostname or "电影资料"


def movie_info_links(info: MovieInfo | None) -> list[LinkRef]:
    if not info:
        return []
    return [
        LinkRef(
            name=info.source_name,
            url=info.source_url,
            image_url=info.image_urls[0] if info.image_urls else None,
            image_urls=info.image_urls,
        )
    ]


def build_movie_info_section(info: MovieInfo | None) -> str:
    if not info:
        return ""
    lines = ["影片资料"]
    if info.name:
        lines.extend(["", f"片名：{info.name}"])
    if info.original_name and info.original_name != info.name:
        lines.append(f"原名：{info.original_name}")
    if info.release_date:
        lines.append(f"上映时间：{info.release_date}")
    if info.genres:
        lines.append(f"类型：{' / '.join(info.genres)}")
    if info.cast:
        lines.append(f"主演：{'、'.join(info.cast)}")
    if info.overview:
        lines.extend(["", f"剧情简介：{info.overview}"])
    lines.extend(["", "资料来源", "", info.source_name, "", info.source_url])
    return "\n".join(lines).strip()


def merge_movie_info_into_text(text: str, info: MovieInfo | None) -> str:
    section = build_movie_info_section(info)
    if not section:
        return text
    body = text.strip()
    if "影片资料" in body and info.source_url in body:
        return body
    return f"{body}\n\n{section}".strip()
