from __future__ import annotations

import base64
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .link_enricher import ExtraLink, build_opener, extract_urls
from .models import LinkRef
from .text_filters import is_channel_promo_line

GITHUB_RE = re.compile(r"^https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s#?]+)", re.IGNORECASE)
VIDEO_URL_RE = re.compile(r"https?://[^\s<>()\"']+\.(?:mp4|webm)(?:\?[^\s<>()\"']*)?", re.IGNORECASE)
GITHUB_ATTACHMENT_VIDEO_RE = re.compile(
    r"https?://github\.com/user-attachments/assets/[0-9a-fA-F-]+", re.IGNORECASE
)
PROMO_DOMAINS = {"geekshare.org", "www.geekshare.org"}


@dataclass(frozen=True)
class GitHubRepoInfo:
    owner: str
    repo: str
    full_name: str
    html_url: str
    description: str = ""
    homepage: str = ""
    language: str = ""
    stars: int = 0
    readme: str = ""


@dataclass(frozen=True)
class SoftwareShare:
    title: str
    text: str
    links: list[LinkRef]
    demo_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)


def build_software_share(
    raw_text: str,
    links: list[LinkRef],
    extra_links: list[ExtraLink] | None = None,
    proxy_url: str | None = None,
) -> SoftwareShare | None:
    repo_url = first_github_repo_url(raw_text, links, extra_links)
    if not repo_url:
        return None
    repo_info = fetch_github_repo_info(repo_url, proxy_url=proxy_url)
    intro_title = find_software_intro_title(raw_text) or repo_title_from_info(repo_info)
    title = normalize_software_title(intro_title, repo_info)
    clean_links = build_software_links(raw_text, links, repo_info)
    demo_urls = find_demo_urls(raw_text, repo_info, clean_links)
    video_urls = find_video_urls(raw_text, repo_info.readme)
    body = build_software_body(raw_text, title, repo_info, demo_urls)
    return SoftwareShare(title=title, text=body, links=clean_links, demo_urls=demo_urls, video_urls=video_urls)


def first_github_repo_url(
    raw_text: str,
    links: list[LinkRef],
    extra_links: list[ExtraLink] | None = None,
) -> str | None:
    candidates = extract_urls(raw_text)
    candidates.extend(link.url for link in links)
    candidates.extend(item.url for item in extra_links or [])
    for url in candidates:
        normalized = normalize_github_repo_url(url)
        if normalized:
            return normalized
    return None


def normalize_github_repo_url(url: str) -> str | None:
    match = GITHUB_RE.match(url.strip())
    if not match:
        return None
    owner = match.group("owner")
    repo = match.group("repo").removesuffix(".git")
    if repo.lower() in {"releases", "issues", "pulls"}:
        return None
    return f"https://github.com/{owner}/{repo}"


def fetch_github_repo_info(url: str, proxy_url: str | None = None, timeout: int = 12) -> GitHubRepoInfo:
    match = GITHUB_RE.match(url)
    if not match:
        raise ValueError(f"Not a GitHub repository URL: {url}")
    owner = match.group("owner")
    repo = match.group("repo").removesuffix(".git")
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    data: dict = {}
    try:
        request = urllib.request.Request(api_url, headers=github_headers())
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="ignore"))
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        data = {}
    full_name = data.get("full_name") or f"{owner}/{repo}"
    html_url = data.get("html_url") or f"https://github.com/{owner}/{repo}"
    readme = fetch_github_readme(owner, repo, proxy_url=proxy_url, timeout=timeout)
    return GitHubRepoInfo(
        owner=owner,
        repo=repo,
        full_name=full_name,
        html_url=html_url,
        description=clean_text(data.get("description") or ""),
        homepage=clean_text(data.get("homepage") or ""),
        language=clean_text(data.get("language") or ""),
        stars=int(data.get("stargazers_count") or 0),
        readme=readme,
    )


def fetch_github_readme(owner: str, repo: str, proxy_url: str | None = None, timeout: int = 12) -> str:
    api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    try:
        request = urllib.request.Request(api_url, headers=github_headers())
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="ignore"))
        content = data.get("content") or ""
        if not content:
            return ""
        return base64.b64decode(content).decode("utf-8", errors="ignore")
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return ""


def github_headers() -> dict[str, str]:
    return {
        "User-Agent": "tgexporter/0.1",
        "Accept": "application/vnd.github+json",
    }


def find_software_intro_title(raw_text: str) -> str | None:
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or is_channel_promo_line(line) or is_hash_tag_line(line) or line.startswith(("http://", "https://")):
            continue
        line = strip_leading_marker(line)
        if " - " in line or "： " in line or " - " in line.replace("—", "-"):
            return line
        if any(word in line for word in ("一个", "一款", "工具", "项目", "平台", "工作台", "管理", "生成", "资料库", "浏览器")):
            return line
    return None


def normalize_software_title(title: str, repo_info: GitHubRepoInfo) -> str:
    title = strip_leading_marker(title)
    title = re.sub(r"\s+", " ", title).strip(" -：:，,")
    if " - " not in title and repo_info.repo.lower() not in title.lower():
        title = f"{display_repo_name(repo_info.repo)} - {title}"
    elif " - " not in title and repo_info.description:
        title = f"{display_repo_name(repo_info.repo)} - {short_description(repo_info.description)}"
    return title[:80].rstrip()


def repo_title_from_info(repo_info: GitHubRepoInfo) -> str:
    if repo_info.description:
        return f"{display_repo_name(repo_info.repo)} - {short_description(repo_info.description)}"
    return display_repo_name(repo_info.repo)


def display_repo_name(repo: str) -> str:
    return repo[:1].upper() + repo[1:] if repo.islower() else repo


def short_description(value: str, max_length: int = 42) -> str:
    value = clean_text(value).rstrip(".。")
    return value if len(value) <= max_length else value[:max_length].rstrip() + "..."


def build_software_body(raw_text: str, title: str, repo_info: GitHubRepoInfo, demo_urls: list[str]) -> str:
    source_summary = clean_original_summary(raw_text, title)
    readme_points = extract_readme_points(repo_info.readme)
    paragraphs: list[str] = []
    if source_summary:
        paragraphs.append(source_summary)
    elif repo_info.description:
        paragraphs.append(repo_info.description)
    if readme_points:
        paragraphs.append("README 重点：\n" + "\n".join(f"- {point}" for point in readme_points[:5]))
    meta_parts = []
    if repo_info.language:
        meta_parts.append(f"主要技术栈：{repo_info.language}")
    if repo_info.stars:
        meta_parts.append(f"GitHub Stars：{repo_info.stars}")
    if meta_parts:
        paragraphs.append("项目概况：" + "；".join(meta_parts) + "。")
    if demo_urls:
        paragraphs.append("如果项目提供演示站，程序会尽量抓取演示页面截图，方便公众号里直接看到实际效果。")
    return "\n\n".join(paragraphs).strip()


def clean_original_summary(raw_text: str, title: str) -> str:
    ignored_titles = {normalize_title_text(title)}
    intro_title = find_software_intro_title(raw_text)
    if intro_title:
        ignored_titles.add(normalize_title_text(intro_title))
    if " - " in title:
        ignored_titles.add(normalize_title_text(title.split(" - ", 1)[1]))
    lines: list[str] = []
    seen: set[str] = set()
    skip_next_url_name = False
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        normalized = normalize_title_text(strip_leading_marker(line))
        if is_channel_promo_line(line) or is_hash_tag_line(line):
            continue
        if normalized in ignored_titles:
            continue
        if line.startswith(("http://", "https://")):
            continue
        if line in {"网站", "下载页面", "在线体验", "项目地址", "GitHub", "开源地址"}:
            skip_next_url_name = True
            continue
        if skip_next_url_name:
            skip_next_url_name = False
        cleaned = strip_leading_marker(line)
        normalized_cleaned = normalize_title_text(cleaned)
        if normalized_cleaned in seen:
            continue
        seen.add(normalized_cleaned)
        lines.append(cleaned)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def extract_readme_points(readme: str) -> list[str]:
    points: list[str] = []
    for raw_line in readme.splitlines():
        line = clean_markdown_line(raw_line)
        if not line:
            continue
        if line.startswith(("#", "```", "---", "<")):
            continue
        if line.startswith("|") or line.endswith("|"):
            continue
        if len(line) < 8 or len(line) > 90:
            continue
        if looks_like_badge_or_link_only(line):
            continue
        if line.startswith(("-", "*", "+")) or any(keyword in line.lower() for keyword in ("feature", "支持", "功能", "管理", "export", "local", "browser", "map", "database")):
            point = line.lstrip("-*+ ").strip()
            if point and point not in points:
                points.append(point)
        if len(points) >= 8:
            break
    return points


def clean_markdown_line(value: str) -> str:
    value = re.sub(r"!\[[^\]]*]\([^)]+\)", "", value)
    value = re.sub(r"\[([^\]]+)]\([^)]+\)", r"\1", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    return clean_text(value).strip()


def looks_like_badge_or_link_only(line: str) -> bool:
    lowered = line.lower()
    if "shields.io" in lowered or "img.shields" in lowered or lowered.startswith(("http://", "https://")):
        return True
    return line in {"[]", "[][]"} or re.fullmatch(r"\[[^\]]*]\s*", line) is not None


def build_software_links(raw_text: str, links: list[LinkRef], repo_info: GitHubRepoInfo) -> list[LinkRef]:
    result: list[LinkRef] = []
    append_link(result, LinkRef(name="GitHub 开源地址", url=repo_info.html_url))
    for url in extract_urls(raw_text):
        if should_skip_source_url(url):
            continue
        name = infer_link_name(raw_text, url, links)
        append_link(result, LinkRef(name=name, url=url))
    if repo_info.homepage and not should_skip_source_url(repo_info.homepage):
        append_link(result, LinkRef(name="演示网站" if not is_github_url(repo_info.homepage) else "项目主页", url=repo_info.homepage))
    return result


def append_link(links: list[LinkRef], link: LinkRef) -> None:
    normalized = normalize_url_for_compare(link.url)
    if normalized and all(normalize_url_for_compare(item.url) != normalized for item in links):
        links.append(link)


def infer_link_name(raw_text: str, url: str, links: list[LinkRef]) -> str:
    lowered = url.lower()
    if "github.com" in lowered and "/releases" in lowered:
        return "下载页面"
    if "github.com" in lowered:
        return "GitHub 开源地址"
    for link in links:
        if normalize_url_for_compare(link.url) == normalize_url_for_compare(url):
            return link.name
    if "sticker" in lowered or "demo" in lowered or "github.io" in lowered or "terraink.app" in lowered:
        return "在线体验"
    return urllib.parse.urlparse(url).netloc or "项目链接"


def find_demo_urls(raw_text: str, repo_info: GitHubRepoInfo, links: list[LinkRef]) -> list[str]:
    urls: list[str] = []
    for link in links:
        if should_capture_demo_url(link.url, link.name):
            urls.append(link.url)
    if repo_info.homepage and should_capture_demo_url(repo_info.homepage, "演示网站"):
        urls.append(repo_info.homepage)
    return unique_urls(urls)


def should_capture_demo_url(url: str, name: str) -> bool:
    if is_github_url(url) or should_skip_source_url(url):
        return False
    lowered = f"{name} {url}".lower()
    return any(keyword in lowered for keyword in ("在线", "demo", "体验", "homepage", "app", "github.io")) or True


def find_video_urls(raw_text: str, readme: str) -> list[str]:
    urls = VIDEO_URL_RE.findall(raw_text + "\n" + readme)
    urls.extend(GITHUB_ATTACHMENT_VIDEO_RE.findall(raw_text + "\n" + readme))
    return unique_urls(urls)[:2]


def should_skip_source_url(url: str) -> bool:
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    return hostname in PROMO_DOMAINS


def is_github_url(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "").lower() == "github.com"


def unique_urls(urls: list[str]) -> list[str]:
    result: list[str] = []
    for url in urls:
        normalized = normalize_url_for_compare(url)
        if normalized and all(normalize_url_for_compare(item) != normalized for item in result):
            result.append(url)
    return result


def normalize_url_for_compare(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunparse(("", parsed.netloc.lower(), path, "", parsed.query, ""))


def is_hash_tag_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped.startswith("#") and re.fullmatch(r"(#[\w\u4e00-\u9fff-]+)(\s+#[\w\u4e00-\u9fff-]+)*", stripped))


def strip_leading_marker(value: str) -> str:
    return re.sub(r"^[^\w\u4e00-\u9fff]+", "", value).strip()


def normalize_title_text(value: str) -> str:
    return re.sub(r"\s+", "", strip_leading_marker(value)).lower()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()
