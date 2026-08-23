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
    "themoviedb.org",
    "netflix.com",
    "wikipedia.org",
)
MOVIE_TITLE_PREFIX_RE = re.compile(r"^\s*名称\s*[:：]\s*")
MOVIE_YEAR_RE = re.compile(r"[（(](?P<year>19\d{2}|20\d{2})[)）]")
MOVIE_TAG_RE = re.compile(r"【[^】]+】")
MOVIE_DESCRIPTION_LINE_RE = re.compile(r"^\s*(?:描述|简介|剧情)\s*[:：]\s*(?P<value>.+?)\s*$")
MOVIE_RESOURCE_LABEL_RE = re.compile(r"^\s*(?P<label>夸克|百度|迅雷|115|阿里)\s*[:：]\s*(?P<url>https?://\S+)\s*$")
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
    directors: tuple[str, ...] = ()
    cast: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()
    duration: str | None = None
    rating: str | None = None
    overview: str | None = None
    image_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class MovieResourceLink:
    label: str
    url: str


def parse_movie_title(title: str) -> MovieTitle:
    value = MOVIE_TITLE_PREFIX_RE.sub("", title or "").strip()
    year_match = MOVIE_YEAR_RE.search(value)
    year = year_match.group("year") if year_match else None
    value = MOVIE_YEAR_RE.sub("", value)
    value = MOVIE_TAG_RE.sub("", value)
    value = re.split(r"\s{2,}|[|｜]", value, maxsplit=1)[0]
    value = re.sub(r"\s+", " ", value).strip(" ：:-")
    return MovieTitle(raw=title, name=value or title.strip(), year=year)


def build_movie_publish_title(title: str, context_text: str | None = None, info: MovieInfo | None = None) -> str:
    movie_title = parse_movie_title(title)
    name = display_movie_name(movie_title, info)
    genres = movie_genres_from_text(title)
    if not genres and info and info.genres:
        genres = list(info.genres[:2])
    hook = movie_hook_from_description(extract_movie_description(context_text or ""))
    if hook:
        return trim_title(f"{name}：{hook}")
    if genres:
        return trim_title(f"{name}：{'/'.join(genres[:2])}新片资源整理")
    return trim_title(name)


def display_movie_name(movie_title: MovieTitle, info: MovieInfo | None = None) -> str:
    name = movie_title.name or (info.name if info else "") or movie_title.raw
    year = movie_title.year or year_from_text(info.release_date if info else None)
    if year and year not in name:
        return f"{name}({year})"
    return name


def trim_title(value: str, limit: int = 64) -> str:
    value = re.sub(r"\s+", " ", value).strip(" ：:-")
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def movie_genres_from_text(text: str) -> list[str]:
    genres: list[str] = []
    for tag in MOVIE_TAG_RE.findall(text or ""):
        content = tag.strip("【】")
        for part in re.split(r"[、,/&|｜.\s]+", content):
            part = part.strip()
            if part in {"剧情", "喜剧", "科幻", "动作", "悬疑", "惊悚", "恐怖", "犯罪", "战争", "奇幻", "冒险", "动画", "爱情", "纪录"}:
                if part not in genres:
                    genres.append(part)
    return genres


def year_from_text(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"(19\d{2}|20\d{2})", value)
    return match.group(1) if match else None


def extract_movie_description(text: str) -> str:
    for line in (text or "").splitlines():
        match = MOVIE_DESCRIPTION_LINE_RE.match(line)
        if match:
            return clean_text(match.group("value"))
    return ""


def movie_hook_from_description(description: str) -> str:
    description = clean_text(description)
    if not description:
        return ""
    replacements = [
        ("在美国陆军游骑兵选拔的最后阶段，", ""),
        ("一支精英团队的训练演习变成了与一种难以想象的威胁之间的生存之战", "精英部队训练突变生存战"),
        ("训练演习变成了", "训练突变为"),
        ("难以想象的威胁", "未知威胁"),
    ]
    hook = description
    for old, new in replacements:
        hook = hook.replace(old, new)
    hook = re.split(r"[。.!！?？]", hook, maxsplit=1)[0]
    hook = re.sub(r"\s+", " ", hook).strip(" ，,。.")
    if hook == description.strip(" ，,。.") and len(hook) > 18:
        return ""
    if len(hook) > 24:
        hook = hook[:24].rstrip(" ，,") + "…"
    return hook


def build_movie_article_text(raw_text: str, title: str, info: MovieInfo | None = None) -> str:
    description = extract_movie_description(raw_text)
    resources = extract_movie_resource_links(raw_text)
    movie_title = parse_movie_title(title)
    display_name = display_movie_name(movie_title, info)
    source_synopsis = description or extract_movie_body_text(raw_text, movie_title)
    synopsis = choose_movie_synopsis(source_synopsis, info.overview if info else None)
    lines = ["影片导读", "", build_movie_lead(display_name, raw_text, info)]
    if synopsis:
        lines.extend(["", "故事梗概", "", rewrite_movie_description(synopsis, display_name)])
    highlights = build_movie_highlights(raw_text, info)
    if highlights:
        lines.extend(["", "值得关注", ""])
        lines.extend(f"- {item}" for item in highlights)
    info_section = build_movie_info_section(info, display_name=display_name)
    if info_section:
        lines.extend(["", info_section])
    if resources:
        lines.extend(["", "资源信息", ""])
        for item in resources:
            lines.append(f"{item.label}网盘：{item.url}")
    if info:
        lines.extend(["", "资料来源"])
    return "\n".join(lines).strip()


def build_movie_lead(display_name: str, raw_text: str, info: MovieInfo | None) -> str:
    genres = list(info.genres[:3]) if info and info.genres else movie_genres_from_text(raw_text)[:3]
    release_date = info.release_date if info else None
    directors = list(info.directors[:2]) if info else []
    cast = list(info.cast[:3]) if info else []
    details: list[str] = []
    if release_date:
        details.append(f"于{release_date}上线或上映")
    if genres:
        details.append(f"类型涵盖{'、'.join(genres)}")
    if directors:
        details.append(f"由{'、'.join(directors)}执导")
    if cast:
        details.append(f"{'、'.join(cast)}等主演")
    if details:
        return f"《{display_name}》{'，'.join(details)}。本文结合公开影片资料重新梳理故事背景、主创阵容与版本信息，便于观众快速判断是否符合自己的观看偏好。"
    return f"《{display_name}》的相关片源已整理完成。下面从故事背景、类型看点和版本信息几个方面做一次完整导读。"


def choose_movie_synopsis(source_synopsis: str, overview: str | None) -> str:
    source = clean_text(source_synopsis)
    professional = clean_text(overview or "")
    if not source:
        return professional
    if not professional:
        return source
    normalized_source = normalize_movie_text(source)
    normalized_professional = normalize_movie_text(professional)
    if normalized_source in normalized_professional or normalized_professional in normalized_source:
        return professional if len(professional) > len(source) else source
    if contains_cjk(professional) and len(professional) >= len(source):
        return professional
    return source


def normalize_movie_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value or "", flags=re.UNICODE).lower()


def contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value or ""))


def build_movie_highlights(raw_text: str, info: MovieInfo | None) -> list[str]:
    genres = list(info.genres[:3]) if info and info.genres else movie_genres_from_text(raw_text)[:3]
    highlights: list[str] = []
    genre_focus = genre_focus_text(genres)
    if genre_focus:
        highlights.append(f"类型看点：{genre_focus}。")
    if info and info.directors:
        highlights.append(f"主创阵容：由{'、'.join(info.directors[:2])}执导，{'、'.join(info.cast[:4]) + '等' if info.cast else '主创团队'}共同完成。")
    elif info and info.cast:
        highlights.append(f"演员阵容：{'、'.join(info.cast[:5])}等主演，人物关系与角色表现是观看时值得留意的部分。")
    specs = extract_movie_specs(raw_text)
    if specs:
        highlights.append(f"版本信息：当前整理版本包含{'、'.join(specs[:6])}，可按播放设备与字幕需求选择。")
    if info and info.rating:
        highlights.append(f"资料评分：公开资料页当前标注为 {info.rating}，评分可能随新增评价变化。")
    return highlights


def genre_focus_text(genres: list[str]) -> str:
    focus_by_genre = {
        "动作": "动作场面、人物在高压环境下的选择与节奏推进",
        "科幻": "科技设定、未知力量与现实秩序之间的碰撞",
        "悬疑": "信息差、线索铺陈和真相逐步揭开的过程",
        "惊悚": "持续升级的危机感与人物心理压力",
        "恐怖": "环境压迫、未知威胁和生存处境",
        "犯罪": "案件推进、人物动机与道德边界",
        "战争": "冲突规模、团队协作与战争中的个人命运",
        "剧情": "人物关系、角色选择及其带来的现实余波",
        "喜剧": "角色互动、生活冲突与轻松节奏",
        "爱情": "情感关系的建立、变化与人物成长",
        "冒险": "未知环境、任务推进与团队协作",
        "奇幻": "世界观设定、超自然规则与人物成长",
        "动画": "视觉表达、世界构建与角色成长",
        "纪录": "真实素材、事件脉络与议题观察",
    }
    focuses: list[str] = []
    for genre in genres:
        focus = focus_by_genre.get(genre)
        if focus and focus not in focuses:
            focuses.append(focus)
    return "；".join(focuses[:2])


def extract_movie_specs(text: str) -> list[str]:
    specs: list[str] = []
    patterns = (
        r"4K(?:\.SDR|\.HDR)?",
        r"DV(?:双版本)?",
        r"HDR",
        r"杜比视界",
        r"1080p",
        r"REMUX",
        r"WEB-DL",
        r"蓝光原盘",
        r"高码率",
        r"内封[^【】\n]{1,12}字幕",
        r"简繁英(?:双语)?字幕",
        r"\d+集全",
        r"(?:两|三|四|五|六|七|八|九|十)季合集",
    )
    for pattern in patterns:
        for match in re.findall(pattern, text or "", flags=re.IGNORECASE):
            value = clean_text(match)
            if value and not any(value.lower() == item.lower() or value.lower() in item.lower() for item in specs):
                specs.append(value)
    return specs


def extract_movie_body_text(text: str, movie_title: MovieTitle) -> str:
    body_lines: list[str] = []
    title_candidates = {
        clean_text(movie_title.raw),
        clean_text(movie_title.name),
    }
    for line in (text or "").splitlines():
        value = clean_text(line)
        if not value:
            continue
        if value in title_candidates:
            continue
        if MOVIE_TITLE_PREFIX_RE.match(value) or MOVIE_DESCRIPTION_LINE_RE.match(value):
            continue
        if MOVIE_RESOURCE_LABEL_RE.match(value):
            continue
        if value.startswith(("投稿:", "投稿：", "Channel:", "Channel：")):
            continue
        if "资源搜索机器人" in value or "点击搜索" in value:
            continue
        body_lines.append(value)
    return "\n".join(body_lines).strip()


def rewrite_movie_description(description: str, display_name: str) -> str:
    description = clean_text(description)
    if not description:
        return ""
    text = description
    text = text.replace("在美国陆军游骑兵选拔的最后阶段，一支精英团队的训练演习变成了与一种难以想象的威胁之间的生存之战。", "故事从美国陆军游骑兵选拔的最后阶段展开。一次看似常规的训练演习逐渐失控，精英小队被迫面对超出预期的未知威胁，原本的考核变成了真正的生存战。")
    if text == description:
        text = re.sub(r"^这部影片讲述[:：]?", "", text)
        if not text.startswith(("影片", "故事", "本片", "剧集")):
            text = f"故事围绕{text}"
        if not text.endswith(("。", "！", "？")):
            text += "。"
    if display_name and display_name not in text:
        text = f"《{display_name}》{text}"
    return text


def extract_movie_resource_links(text: str) -> list[MovieResourceLink]:
    resources: list[MovieResourceLink] = []
    for line in (text or "").splitlines():
        match = MOVIE_RESOURCE_LABEL_RE.match(line.strip())
        if not match:
            continue
        label = match.group("label")
        url = match.group("url").rstrip(".,;，。；")
        if not any(item.url == url for item in resources):
            resources.append(MovieResourceLink(label=label, url=url))
    return resources


def fetch_movie_info(
    title: str,
    proxy_url: str | None = None,
    limit: int = 6,
    context_text: str | None = None,
) -> MovieInfo | None:
    movie_title = parse_movie_title(title)
    best_info = fetch_imdb_suggestion_info(movie_title, proxy_url=proxy_url)
    best_score = movie_info_quality(best_info) if best_info else -1
    candidates = movie_source_candidates(movie_title, proxy_url=proxy_url, limit=limit, context_text=context_text)
    if best_info and best_info.source_url not in candidates:
        candidates.insert(0, best_info.source_url)
    for url in candidates:
        info = fetch_movie_page_info(url, movie_title, proxy_url=proxy_url)
        if not info or not movie_info_is_usable(info) or not movie_info_matches_expected(info, movie_title):
            continue
        info = merge_movie_infos(info, best_info)
        score = movie_info_quality(info)
        if score > best_score:
            best_info = info
            best_score = score
        if score >= 7:
            break
    return best_info


def merge_movie_infos(primary: MovieInfo, fallback: MovieInfo | None) -> MovieInfo:
    if not fallback:
        return primary
    return MovieInfo(
        name=primary.name or fallback.name,
        source_name=primary.source_name,
        source_url=primary.source_url,
        original_name=primary.original_name or fallback.original_name,
        release_date=primary.release_date or fallback.release_date,
        genres=primary.genres or fallback.genres,
        directors=primary.directors or fallback.directors,
        cast=primary.cast or fallback.cast,
        countries=primary.countries or fallback.countries,
        duration=primary.duration or fallback.duration,
        rating=primary.rating or fallback.rating,
        overview=primary.overview or fallback.overview,
        image_urls=tuple(dict.fromkeys([*primary.image_urls, *fallback.image_urls])),
    )


def fetch_imdb_suggestion_info(
    movie_title: MovieTitle,
    proxy_url: str | None = None,
    timeout: int = 10,
) -> MovieInfo | None:
    query = urllib.parse.quote(movie_title.name)
    url = f"https://v3.sg.media-imdb.com/suggestion/x/{query}.json"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    try:
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            payload = json.loads(response.read(256_000).decode("utf-8", errors="ignore"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    return movie_info_from_imdb_suggestions(payload, movie_title)


def movie_info_from_imdb_suggestions(payload, expected: MovieTitle) -> MovieInfo | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("d"), list):
        return None
    candidates = [item for item in payload["d"] if isinstance(item, dict) and str(item.get("id", "")).startswith("tt")]
    if not candidates:
        return None
    if expected.year:
        year_matches = [item for item in candidates if str(item.get("y") or "") == expected.year]
        if year_matches:
            candidates = year_matches
    item = candidates[0]
    imdb_id = str(item.get("id"))
    name = clean_text(str(item.get("l") or expected.name))
    cast = tuple(clean_text(part) for part in str(item.get("s") or "").split(",") if clean_text(part))
    image = item.get("i") if isinstance(item.get("i"), dict) else {}
    image_url = clean_optional(image.get("imageUrl"))
    year = clean_optional(item.get("y")) or expected.year
    return MovieInfo(
        name=name,
        source_name="IMDb",
        source_url=f"https://www.imdb.com/title/{imdb_id}/",
        original_name=name if name != expected.name else None,
        release_date=year,
        cast=cast[:8],
        image_urls=(image_url,) if image_url else (),
    )


def movie_info_is_usable(info: MovieInfo) -> bool:
    generic_names = {
        "豆瓣",
        "豆瓣电影",
        "imdb",
        "rottentomatoes",
        "rottentomatoesmovie",
        "tmioe",
        "tmdb",
        "猫眼电影",
        "netflix",
    }
    name = normalize_movie_text(info.name)
    return bool(name and name not in generic_names)


def movie_info_matches_expected(info: MovieInfo, expected: MovieTitle) -> bool:
    expected_name = normalize_movie_text(expected.name)
    names = [normalize_movie_text(info.name), normalize_movie_text(info.original_name or "")]
    if expected_name and any(expected_name in name or name in expected_name for name in names if name):
        return True
    return bool(expected.year and year_from_text(info.release_date) == expected.year)


def movie_info_quality(info: MovieInfo) -> int:
    score = 0
    score += 3 if info.overview else 0
    score += 2 if info.cast else 0
    score += 1 if info.directors else 0
    score += 1 if info.release_date else 0
    score += 1 if info.genres else 0
    score += 1 if info.countries else 0
    score += 1 if info.duration else 0
    score += 1 if info.rating else 0
    score += 1 if info.image_urls else 0
    return score


def movie_source_candidates(
    movie_title: MovieTitle,
    proxy_url: str | None = None,
    limit: int = 6,
    context_text: str | None = None,
) -> list[str]:
    candidates = search_tmdb_source_urls(movie_title, proxy_url=proxy_url)
    if candidates:
        return candidates[:limit]
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
            *search_duckduckgo_movie_source_urls(query, proxy_url=proxy_url),
            *search_movie_source_urls(query, proxy_url=proxy_url),
            *search_google_movie_source_urls(query, proxy_url=proxy_url),
        ]
        for url in urls:
            if url not in candidates:
                candidates.append(url)
            if len(candidates) >= limit:
                return candidates
    return candidates


def search_tmdb_source_urls(
    movie_title: MovieTitle,
    proxy_url: str | None = None,
    timeout: int = 10,
) -> list[str]:
    search_url = "https://www.themoviedb.org/search?" + urllib.parse.urlencode(
        {"query": movie_title.name, "language": "zh-CN"}
    )
    request = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    try:
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            text = response.read(512_000).decode(response.headers.get_content_charset() or "utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    return extract_tmdb_search_urls(text)


def extract_tmdb_search_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r'href=["\'](?P<path>/(?:movie|tv)/\d+[^"\']*)["\']', text or "", re.IGNORECASE):
        url = urllib.parse.urljoin("https://www.themoviedb.org", html.unescape(match.group("path")))
        if url not in urls:
            urls.append(url)
    return urls[:6]


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


def search_duckduckgo_movie_source_urls(query: str, proxy_url: str | None = None, timeout: int = 10) -> list[str]:
    search_url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
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
    normalized_text = urllib.parse.unquote(html.unescape(text or "").replace("\\/", "/"))
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
    url = re.sub(r"&rut=[0-9a-f]+(?:&.*)?$", "", url, flags=re.IGNORECASE)
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
            directors=info.directors,
            cast=info.cast,
            countries=info.countries,
            duration=info.duration,
            rating=info.rating,
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
                genres=tuple(localize_movie_terms(clean_sequence(item.get("genre")))),
                directors=tuple(clean_people(item.get("director"))[:4]),
                cast=tuple(clean_people(item.get("actor") or item.get("actors"))[:8]),
                countries=tuple(localize_movie_terms(clean_people(item.get("countryOfOrigin"))[:4])),
                duration=format_movie_duration(clean_optional(item.get("duration"))),
                rating=clean_movie_rating(item.get("aggregateRating")),
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


def localize_movie_terms(values: list[str]) -> list[str]:
    translations = {
        "action": "动作",
        "adventure": "冒险",
        "animation": "动画",
        "comedy": "喜剧",
        "crime": "犯罪",
        "documentary": "纪录",
        "drama": "剧情",
        "family": "家庭",
        "fantasy": "奇幻",
        "history": "历史",
        "horror": "恐怖",
        "mystery": "悬疑",
        "romance": "爱情",
        "sci-fi": "科幻",
        "science fiction": "科幻",
        "thriller": "惊悚",
        "war": "战争",
        "united states": "美国",
        "usa": "美国",
        "united kingdom": "英国",
        "uk": "英国",
        "south korea": "韩国",
        "korea": "韩国",
        "japan": "日本",
        "china": "中国",
        "france": "法国",
        "germany": "德国",
        "canada": "加拿大",
    }
    result: list[str] = []
    for value in values:
        localized = translations.get(value.strip().lower(), value)
        if localized not in result:
            result.append(localized)
    return result


def clean_movie_rating(value) -> str | None:
    if not isinstance(value, dict):
        return clean_optional(value)
    rating = clean_optional(value.get("ratingValue"))
    if not rating:
        return None
    best = clean_optional(value.get("bestRating"))
    return f"{rating}/{best}" if best and "/" not in rating else rating


def format_movie_duration(value: str | None) -> str | None:
    if not value:
        return None
    match = re.fullmatch(r"PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?", value, re.IGNORECASE)
    if not match:
        return value
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    if hours and minutes:
        return f"{hours}小时{minutes}分钟"
    if hours:
        return f"{hours}小时"
    return f"{minutes}分钟" if minutes else value


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
    if hostname.endswith("themoviedb.org"):
        return "TMDB"
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


def build_movie_info_section(
    info: MovieInfo | None,
    display_name: str | None = None,
) -> str:
    if not info:
        return ""
    lines = ["影片资料"]
    resolved_name = display_name or info.name
    if resolved_name:
        lines.extend(["", f"片名：{resolved_name}"])
    if info.original_name and info.original_name != info.name:
        lines.append(f"原名：{info.original_name}")
    if info.release_date:
        lines.append(f"上映时间：{info.release_date}")
    if info.genres:
        lines.append(f"类型：{' / '.join(info.genres)}")
    if info.directors:
        lines.append(f"导演：{'、'.join(info.directors)}")
    if info.cast:
        lines.append(f"主演：{'、'.join(info.cast)}")
    if info.countries:
        lines.append(f"国家/地区：{' / '.join(info.countries)}")
    if info.duration:
        lines.append(f"片长：{info.duration}")
    if info.rating:
        lines.append(f"资料评分：{info.rating}")
    return "\n".join(lines).strip()
