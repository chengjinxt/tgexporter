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
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_IMAGE_RE = re.compile(r"<img\b[^>]*\bsrc=[\"'](?P<url>[^\"']+)[\"'][^>]*>", re.IGNORECASE)
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
    default_branch: str = "main"


@dataclass(frozen=True)
class SoftwareShare:
    title: str
    text: str
    links: list[LinkRef]
    image_urls: list[str] = field(default_factory=list)
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
    image_urls = find_readme_image_urls(repo_info)
    demo_urls = find_demo_urls(raw_text, repo_info, clean_links)
    video_urls = find_video_urls(raw_text, repo_info.readme)
    body = build_software_body(raw_text, title, repo_info, demo_urls)
    return SoftwareShare(
        title=title,
        text=body,
        links=clean_links,
        image_urls=image_urls,
        demo_urls=demo_urls,
        video_urls=video_urls,
    )


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
        default_branch=clean_text(data.get("default_branch") or "main") or "main",
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
    readme_points = summarize_readme_in_chinese(repo_info)
    paragraphs: list[str] = []
    if source_summary:
        paragraphs.append(source_summary)
    elif repo_info.description:
        paragraphs.append(repo_info.description)
    if readme_points:
        paragraphs.append("README 中文总结：\n" + "\n".join(f"- {point}" for point in readme_points[:5]))
    meta_parts = []
    if repo_info.language:
        meta_parts.append(f"主要技术栈：{repo_info.language}")
    if repo_info.stars:
        meta_parts.append(f"GitHub Stars：{repo_info.stars}")
    if meta_parts:
        paragraphs.append("项目概况：" + "；".join(meta_parts) + "。")
    if demo_urls:
        paragraphs.append("已优先补充项目 README 或演示站中的主要界面图，方便直接查看实际效果。")
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
    skip_generated_block = False
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            skip_generated_block = False
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if should_skip_previous_render_line(line):
            skip_generated_block = True
            continue
        if skip_generated_block:
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


def should_skip_previous_render_line(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("# "):
        return True
    if MARKDOWN_IMAGE_RE.search(stripped):
        return True
    if re.search(r"\]\([^)]+\.(?:png|jpe?g|webp|gif)\)", stripped, re.IGNORECASE):
        return True
    if stripped.startswith(("README 重点", "README 中文总结", "项目概况", "已优先补充", "如果项目提供演示站")):
        return True
    if looks_like_command_line(stripped):
        return True
    if stripped.lower().startswith(("rust 2024 edition", "platform-specific system dependencies")):
        return True
    return False


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


def summarize_readme_in_chinese(repo_info: GitHubRepoInfo) -> list[str]:
    readme = repo_info.readme or ""
    chinese_points = extract_chinese_readme_points(readme)
    inferred_points = infer_chinese_readme_points(readme, repo_info)
    points = unique_texts(chinese_points + inferred_points)
    if not points and repo_info.description:
        points.append(f"项目定位：{repo_info.description.rstrip('。')}")
    return points[:5]


def extract_chinese_readme_points(readme: str) -> list[str]:
    points: list[str] = []
    for raw_line in readme.splitlines():
        line = clean_markdown_line(raw_line)
        if not line or not contains_cjk(line):
            continue
        if line.startswith(("#", "```", "---", "<")) or line.startswith("|") or line.endswith("|"):
            continue
        if looks_like_badge_or_link_only(line) or looks_like_command_line(line):
            continue
        line = line.lstrip("-*+0123456789.、) ").strip()
        if 10 <= len(line) <= 90:
            points.append(normalize_chinese_sentence(line))
        if len(points) >= 5:
            break
    return points


def infer_chinese_readme_points(readme: str, repo_info: GitHubRepoInfo) -> list[str]:
    text = f"{repo_info.description}\n{readme}".lower()
    rules: list[tuple[tuple[str, ...], str]] = [
        (("database", "sql", "query", "postgres", "mysql", "sqlite"), "支持数据库连接、SQL 查询、结果查看和数据管理等工作流。"),
        (("ssh", "sftp", "terminal", "rdp", "vnc", "port forward", "server"), "集成 SSH/SFTP、终端、端口转发、远程桌面和服务器管理能力。"),
        (("mcp", "ai", "assistant", "agent"), "可结合 AI/MCP 能力辅助查询解释、运维操作或自动化处理。"),
        (("rich text", "sticker", "image upload", "peel", "webgl", "three"), "提供富文本、图片上传和交互式视觉效果，适合生成可展示的创意素材。"),
        (("browser", "profile", "cookie", "proxy", "fingerprint", "rpa"), "支持多浏览器环境、Cookie/缓存隔离、代理/指纹配置和自动化流程。"),
        (("map", "geocoding", "theme", "poster", "terrain", "png"), "支持地理编码、地图主题、自定义样式和高分辨率图片导出。"),
        (("ielts", "vocabulary", "listening", "speaking", "writing", "reading"), "整理雅思词汇、听说读写练习和备考资料，适合本地化学习使用。"),
        (("mac", "battery", "disk", "camera", "pdf", "report"), "支持 Mac 硬件、电池、硬盘、摄像头等检测，并可生成验机报告。"),
        (("windows", "context menu", "right click", "shell extension"), "面向 Windows 桌面使用场景，提供右键菜单或本地效率增强能力。"),
        (("local", "desktop", "offline", "privacy"), "强调本地运行和隐私友好，适合离线或自托管环境使用。"),
    ]
    points = [sentence for keywords, sentence in rules if any(keyword in text for keyword in keywords)]
    if repo_info.language:
        points.append(f"项目主要使用 {repo_info.language} 开发，适合关注该技术栈的用户参考。")
    return points


def find_readme_image_urls(repo_info: GitHubRepoInfo) -> list[str]:
    readme = repo_info.readme or ""
    urls: list[str] = []
    for pattern in (MARKDOWN_IMAGE_RE, HTML_IMAGE_RE):
        for match in pattern.finditer(readme):
            raw_url = html.unescape(match.group("url")).strip()
            resolved = resolve_readme_asset_url(raw_url, repo_info)
            if is_article_image_url(resolved):
                urls.append(resolved)
    return unique_urls(urls)[:4]


def resolve_readme_asset_url(url: str, repo_info: GitHubRepoInfo) -> str:
    url = url.strip("<>")
    if url.startswith("//"):
        return "https:" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc.lower() == "github.com" and "/blob/" in parsed.path:
            return url.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/")
        return url
    if url.startswith("#") or url.startswith("data:"):
        return ""
    path = url.split("#", 1)[0].split("?", 1)[0].lstrip("./")
    if not path:
        return ""
    branch = repo_info.default_branch or "main"
    encoded_path = "/".join(urllib.parse.quote(part) for part in path.split("/"))
    return f"https://raw.githubusercontent.com/{repo_info.owner}/{repo_info.repo}/{branch}/{encoded_path}"


def is_article_image_url(url: str) -> bool:
    if not url:
        return False
    lowered = urllib.parse.urlparse(url).path.lower()
    return lowered.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


def contains_cjk(value: str) -> bool:
    return re.search(r"[\u4e00-\u9fff]", value) is not None


def looks_like_command_line(line: str) -> bool:
    lowered = line.lower()
    return bool(
        re.match(r"^\s*(npm|pnpm|yarn|pip|cargo|go|docker|git|python|navop)\s+", lowered)
        or lowered.startswith(("http://", "https://"))
        or "--" in lowered
    )


def normalize_chinese_sentence(value: str) -> str:
    value = clean_text(value).strip("。；;,.，")
    return value + "。"


def unique_texts(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", "", value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(value)
    return result


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
