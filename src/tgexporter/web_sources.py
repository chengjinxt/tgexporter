from __future__ import annotations

import hashlib
import html as html_lib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .collector import download_web_file, extension_from_url, find_existing_markdown_by_title
from .filename import media_filename, sanitize_title
from .image_filter import is_suitable_article_image
from .link_enricher import build_opener
from .markdown_renderer import MarkdownRenderer
from .models import ArticleDraft, LinkRef, MediaAsset
from .placeholder_image import write_placeholder_png
from .state import StateStore

DEFAULT_USER_AGENT = "Mozilla/5.0 tgexporter/0.1"
MOE_WORK_UPDATES_URL = "http://www.moe.gov.cn/jyb_xwfb/gzdt_gzdt/"


@dataclass(frozen=True)
class WebSourceDefinition:
    key: str
    name: str
    account: str
    list_url: str
    parser: str
    brand_text: str
    intro_label: str = "教育要闻速读"


@dataclass(frozen=True)
class SourceListEntry:
    title: str
    url: str
    date_text: str


@dataclass(frozen=True)
class SourceArticle:
    title: str
    url: str
    published_at: datetime
    source_name: str
    paragraphs: tuple[str, ...]
    image_urls: tuple[str, ...] = ()


FetchText = Callable[[str, str | None], str]


DEFAULT_WEB_SOURCE_GROUPS: dict[str, tuple[WebSourceDefinition, ...]] = {
    "chunhui-xuefu": (
        WebSourceDefinition(
            key="moe-work-updates",
            name="教育部工作动态",
            account="chunhui-xuefu",
            list_url=MOE_WORK_UPDATES_URL,
            parser="moe",
            brand_text="春晖学府",
        ),
    )
}


class WebSourceCollector:
    def __init__(
        self,
        state: StateStore,
        renderer: MarkdownRenderer,
        timezone: str = "Asia/Shanghai",
        proxy_url: str | None = None,
        fetch_text: FetchText | None = None,
    ) -> None:
        self.state = state
        self.renderer = renderer
        self.timezone = ZoneInfo(timezone)
        self.proxy_url = proxy_url
        self.fetch_text = fetch_text or fetch_url_text

    def collect_once(self, sources: list[WebSourceDefinition], limit: int = 1, backfill: bool = False) -> list[Path]:
        rendered: list[Path] = []
        remaining = max(0, limit)
        for source in sources:
            if remaining <= 0:
                break
            entries = self.fetch_entries(source)
            for entry in entries:
                if remaining <= 0:
                    break
                if not is_publishable_education_entry(entry):
                    continue
                if self.state.source_url_processed(source.account, entry.url):
                    if not backfill:
                        break
                    continue
                existing_path = self.state.article_path_by_source_url(source.account, entry.url)
                if existing_path is not None:
                    if not backfill:
                        break
                    continue
                try:
                    article = self.fetch_article(source, entry)
                    path = self.render_article(source, article)
                    if path is None:
                        continue
                except Exception as exc:
                    print(f"Skipped web source article {entry.url}: {exc}", file=sys.stderr)
                    continue
                rendered.append(path)
                remaining -= 1
        return rendered

    def fetch_entries(self, source: WebSourceDefinition) -> list[SourceListEntry]:
        html_text = self.fetch_text(source.list_url, self.proxy_url)
        if source.parser != "moe":
            raise RuntimeError(f"Unsupported web source parser: {source.parser}")
        return parse_moe_list(html_text, source.list_url)

    def fetch_article(self, source: WebSourceDefinition, entry: SourceListEntry) -> SourceArticle:
        html_text = self.fetch_text(entry.url, self.proxy_url)
        if source.parser != "moe":
            raise RuntimeError(f"Unsupported web source parser: {source.parser}")
        return parse_moe_article(html_text, entry.url, fallback_title=entry.title, fallback_date=entry.date_text, timezone=self.timezone)

    def render_article(self, source: WebSourceDefinition, source_article: SourceArticle) -> Path | None:
        date_key = source_article.published_at.strftime("%Y%m%d")
        title = sanitize_title(source_article.title, max_length=70)
        existing_path = find_existing_markdown_by_title(self.renderer.base_dir / date_key, title)
        if existing_path is not None:
            self.state.record_source_url(
                source.account,
                source_article.url,
                build_article_draft(
                    source=source,
                    source_article=source_article,
                    daily_index=0,
                    title=title,
                    text="",
                ),
                existing_path,
            )
            return None

        daily_index = self.state.next_daily_index(date_key)
        text = compose_education_article_text(source, source_article)
        article = build_article_draft(
            source=source,
            source_article=source_article,
            daily_index=daily_index,
            title=title,
            text=text,
        )
        date_dir = self.renderer.base_dir / date_key
        date_dir.mkdir(parents=True, exist_ok=True)
        article.media.extend(self._download_article_images(source, source_article, article, date_dir))
        if not article.media:
            article.media.append(self._create_placeholder(source, article, date_dir))
        article.status = "rendered"
        markdown_path = self.renderer.render(article)
        self.state.record_article(article, markdown_path)
        self.state.record_source_url(source.account, source_article.url, article, markdown_path)
        return markdown_path

    def _download_article_images(
        self,
        source: WebSourceDefinition,
        source_article: SourceArticle,
        article: ArticleDraft,
        date_dir: Path,
    ) -> list[MediaAsset]:
        for index, image_url in enumerate(source_article.image_urls, start=1):
            filename = media_filename(
                article.date_key,
                article.daily_index,
                "PIC",
                index,
                article.title,
                extension_from_url(image_url, ".jpg"),
            )
            destination = date_dir / filename
            if not download_web_file(image_url, destination, proxy_url=self.proxy_url, referer=source_article.url):
                continue
            if not is_suitable_article_image(destination):
                destination.unlink(missing_ok=True)
                continue
            return [
                MediaAsset(
                    kind="image",
                    filename=filename,
                    path=destination,
                    source=source.key,
                    title=source_article.title,
                )
            ]
        return []

    def _create_placeholder(self, source: WebSourceDefinition, article: ArticleDraft, date_dir: Path) -> MediaAsset:
        filename = media_filename(article.date_key, article.daily_index, "PIC", 1, article.title, ".png")
        destination = date_dir / filename
        write_placeholder_png(destination, title=article.title, brand_text=source.brand_text)
        return MediaAsset(
            kind="image",
            filename=filename,
            path=destination,
            source="generated_placeholder",
            title=article.title,
        )


def build_article_draft(
    source: WebSourceDefinition,
    source_article: SourceArticle,
    daily_index: int,
    title: str,
    text: str,
) -> ArticleDraft:
    return ArticleDraft(
        source="web",
        channel=source.account,
        message_ids=[],
        grouped_id=None,
        published_at=source_article.published_at,
        date_key=source_article.published_at.strftime("%Y%m%d"),
        daily_index=daily_index,
        title=title,
        text=text,
        links=[LinkRef(name=source_article.source_name or source.name, url=source_article.url)],
        source_id=source_id_for_url(source_article.url),
    )


def load_web_source_group(group: str) -> list[WebSourceDefinition]:
    try:
        return list(DEFAULT_WEB_SOURCE_GROUPS[group])
    except KeyError as exc:
        groups = ", ".join(sorted(DEFAULT_WEB_SOURCE_GROUPS))
        raise RuntimeError(f"Unknown web source group: {group}. Available groups: {groups}") from exc


def run_web_source_loop(
    collector: WebSourceCollector,
    sources: list[WebSourceDefinition],
    limit: int,
    interval_seconds: int,
    backfill: bool = False,
) -> None:
    if interval_seconds <= 0:
        paths = collector.collect_once(sources, limit=limit, backfill=backfill)
        print_rendered(paths)
        return
    while True:
        paths = collector.collect_once(sources, limit=limit, backfill=backfill)
        print_rendered(paths)
        time.sleep(interval_seconds)


def print_rendered(paths: list[Path]) -> None:
    if not paths:
        print("No new web source articles.")
        return
    for path in paths:
        print(f"Rendered: {path}")


def fetch_url_text(url: str, proxy_url: str | None = None, timeout: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    try:
        with build_opener(proxy_url).open(request, timeout=timeout) as response:
            raw = response.read(1_500_000)
            charset = response.headers.get_content_charset() or "utf-8"
            final_url = response.geturl()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
    text = raw.decode(charset, errors="ignore")
    return text.replace(final_url, url)


def parse_moe_list(html_text: str, base_url: str) -> list[SourceListEntry]:
    parser = MoeListParser(base_url)
    parser.feed(html_text)
    return parser.entries


def parse_moe_article(
    html_text: str,
    url: str,
    fallback_title: str,
    fallback_date: str,
    timezone: ZoneInfo,
) -> SourceArticle:
    parser = MoeArticleParser(url)
    parser.feed(html_text)
    meta = parser.meta
    title = clean_text(meta.get("articletitle") or parser.title or fallback_title)
    pubdate = meta.get("pubdate") or meta.get("publishdate") or fallback_date
    published_at = parse_datetime(pubdate, timezone)
    source_name = clean_text(meta.get("contentsource") or meta.get("source") or "教育部")
    image_urls = tuple(url for url in [meta.get("pictrue"), *parser.image_urls] if url)
    paragraphs = tuple(paragraph for paragraph in parser.paragraphs if paragraph)
    if not paragraphs:
        description = clean_text(meta.get("description") or "")
        paragraphs = (description,) if description else (title,)
    return SourceArticle(
        title=title,
        url=url,
        published_at=published_at,
        source_name=source_name,
        paragraphs=paragraphs,
        image_urls=unique_tuple(image_urls),
    )


class MoeListParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.entries: list[SourceListEntry] = []
        self.in_list = False
        self.list_depth = 0
        self.current: dict[str, str] | None = None
        self.in_link = False
        self.in_date = False
        self.text_parts: list[str] = []
        self.date_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = attrs_to_dict(attrs)
        if tag == "ul" and attrs_dict.get("id") == "list":
            self.in_list = True
            self.list_depth = 1
            return
        if not self.in_list:
            return
        self.list_depth += 1
        if tag == "li":
            self.current = {}
        elif tag == "a" and self.current is not None:
            href = attrs_dict.get("href", "")
            self.current["url"] = urllib.parse.urljoin(self.base_url, href)
            if attrs_dict.get("title"):
                self.current["title"] = attrs_dict["title"]
            self.in_link = True
            self.text_parts = []
        elif tag == "span" and self.current is not None:
            self.in_date = True
            self.date_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_link:
            self.text_parts.append(data)
        elif self.in_date:
            self.date_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.in_list:
            return
        if tag == "a" and self.in_link:
            if self.current is not None and not self.current.get("title"):
                self.current["title"] = clean_text("".join(self.text_parts))
            self.in_link = False
        elif tag == "span" and self.in_date:
            if self.current is not None:
                self.current["date"] = clean_text("".join(self.date_parts))
            self.in_date = False
        elif tag == "li":
            if self.current and self.current.get("title") and self.current.get("url"):
                self.entries.append(
                    SourceListEntry(
                        title=clean_text(self.current["title"]),
                        url=self.current["url"],
                        date_text=clean_text(self.current.get("date", "")),
                    )
                )
            self.current = None
        self.list_depth -= 1
        if self.list_depth <= 0:
            self.in_list = False


class MoeArticleParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.meta: dict[str, str] = {}
        self.title = ""
        self.paragraphs: list[str] = []
        self.image_urls: list[str] = []
        self.in_h1 = False
        self.h1_parts: list[str] = []
        self.in_editor = False
        self.editor_depth = 0
        self.current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = attrs_to_dict(attrs)
        if tag == "meta":
            name = (attrs_dict.get("name") or attrs_dict.get("property") or "").lower()
            content = attrs_dict.get("content", "")
            if name and content:
                self.meta[name] = html_lib.unescape(content).strip()
            return
        if tag == "h1":
            self.in_h1 = True
            self.h1_parts = []
        if tag == "div" and "trs_editor" in attrs_dict.get("class", "").lower():
            self.in_editor = True
            self.editor_depth = 1
            self.current_text = []
            return
        if not self.in_editor:
            return
        self.editor_depth += 1
        if tag in {"p", "li"}:
            self.flush_current_text()
        if tag == "img":
            image_url = first_attr(attrs_dict, ("src", "data-src", "data-original"))
            if image_url:
                normalized = urllib.parse.urljoin(self.base_url, image_url)
                if normalized not in self.image_urls:
                    self.image_urls.append(normalized)
        if tag == "br":
            self.flush_current_text()

    def handle_data(self, data: str) -> None:
        if self.in_h1:
            self.h1_parts.append(data)
        if self.in_editor:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self.in_h1:
            self.title = clean_text("".join(self.h1_parts))
            self.in_h1 = False
        if not self.in_editor:
            return
        if tag in {"p", "li", "div"}:
            self.flush_current_text()
        self.editor_depth -= 1
        if self.editor_depth <= 0:
            self.in_editor = False

    def flush_current_text(self) -> None:
        text = clean_text("".join(self.current_text))
        if text:
            self.paragraphs.append(text)
        self.current_text = []


def compose_education_article_text(source: WebSourceDefinition, article: SourceArticle) -> str:
    date_label = article.published_at.strftime("%Y-%m-%d")
    paragraphs = [trim_text(paragraph, 130) for paragraph in article.paragraphs[:3]]
    lines = [
        source.intro_label,
        "",
        f"{article.source_name} {date_label} 发布：{article.title}。",
        "",
        "重点速览",
    ]
    for paragraph in paragraphs:
        lines.append(f"- {paragraph}")
    lines.extend(["", "春晖学府关注"])
    lines.extend(f"- {item}" for item in account_angle_lines(article))
    lines.extend(
        [
            "",
            "可转视频口播",
            f"- 开头：教育部刚发布了一条和{short_topic(article.title)}相关的新动态，值得学校、老师和家长一起看。",
            "- 结尾：更多教育政策与升学信息，春晖学府会持续跟进。",
        ]
    )
    return "\n".join(lines).strip()


def account_angle_lines(article: SourceArticle) -> list[str]:
    text = article.title + "\n" + "\n".join(article.paragraphs)
    if any(keyword in text for keyword in ("教师", "师资", "教研")):
        return [
            "关注教师培养、师资配置、培训支持和待遇保障，这些往往会影响学校长期教学质量。",
            "适合延伸成“政策解读 + 教师成长路径 + 家长怎么看”的服务型图文。",
        ]
    if any(keyword in text for keyword in ("智慧教育", "平台", "数字教育")):
        return [
            "关注优质数字资源如何进入课堂、课后服务和家庭学习场景。",
            "适合整理成家长可操作的资源清单或老师备课工具提醒。",
        ]
    if any(keyword in text for keyword in ("职业教育", "专业目录", "专业")):
        return [
            "关注专业设置变化背后的产业方向，帮助学生和家长理解未来人才需求。",
            "适合延伸成“新增专业怎么选、适合谁、未来看什么岗位”的选科择校内容。",
        ]
    if any(keyword in text for keyword in ("助学贷款", "资助", "奖学金")):
        return [
            "关注申请时间、办理渠道和材料要求，信息越早整理对家庭越有帮助。",
            "适合做成流程图文或短视频清单，降低家长和学生的信息搜索成本。",
        ]
    if any(keyword in text for keyword in ("高考", "招生", "录取", "志愿")):
        return [
            "关注报名、志愿、录取和征集志愿等时间节点，适合做成提醒型内容。",
            "适合按省份或人群拆分，形成连续更新的升学服务栏目。",
        ]
    return [
        "关注政策变化背后的学校、教师、学生和家长四类影响。",
        "适合整理成“发生了什么、为什么重要、接下来关注什么”的三段式图文。",
    ]


def is_publishable_education_entry(entry: SourceListEntry) -> bool:
    title = entry.title
    if "任" in title and any(keyword in title for keyword in ("党委书记", "校长")):
        return False
    return True


def parse_datetime(value: str, timezone: ZoneInfo) -> datetime:
    value = clean_text(value)
    candidates = []
    if len(value) >= 16:
        candidates.append((value[:16], "%Y-%m-%d %H:%M"))
    if len(value) >= 10:
        candidates.append((value[:10], "%Y-%m-%d"))
    for candidate, fmt in candidates:
        try:
            return datetime.strptime(candidate, fmt).replace(tzinfo=timezone)
        except ValueError:
            continue
    return datetime.now(timezone)


def source_id_for_url(url: str) -> str:
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:16]


def clean_text(value: str) -> str:
    value = html_lib.unescape(value or "")
    value = value.replace("\u3000", " ")
    return re.sub(r"\s+", " ", value).strip()


def trim_text(value: str, max_length: int) -> str:
    value = clean_text(value)
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip("，。；、 ") + "…"


def short_topic(title: str) -> str:
    title = clean_text(title)
    for keyword in ("教师", "智慧教育", "职业教育", "助学贷款", "高考", "招生"):
        if keyword in title:
            return keyword
    return "教育政策"


def attrs_to_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {name.lower(): value or "" for name, value in attrs}


def first_attr(attrs: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = attrs.get(name)
        if value:
            return value
    return ""


def unique_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return tuple(unique)
