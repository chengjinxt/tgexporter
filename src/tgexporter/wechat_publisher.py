from __future__ import annotations

import base64
import hashlib
import html
import mimetypes
import random
import re
import time
import unicodedata
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .filename import sanitize_title
from .markdown_utils import is_external_markdown_target, markdown_image_line, markdown_link_line
from .text_filters import is_channel_promo_line


WECHAT_HOME_URL = "https://mp.weixin.qq.com/"
WECHAT_EDITOR_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg"
MAX_WECHAT_ARTICLES = 8
WECHAT_IMAGE_MAX_BYTES = 2_000_000
WECHAT_IMAGE_MAX_DIMENSION = 4096
DEFAULT_HUMAN_PAUSE_MS = (900, 1800)
MARKDOWN_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)\s]+)\)")
URL_ONLY_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
NEW_ARTICLE_LABELS = ["新建内容", "添加图文", "新建图文", "新增图文"]
ARTICLE_SUFFIXES = {".md", ".mk"}
P_STYLE = "margin: 0 0 18px; color: #2b2f36; font-size: 16px; line-height: 1.85; letter-spacing: 0;"
H1_STYLE = (
    "margin: 0 0 22px; padding-bottom: 10px; border-bottom: 1px solid #e5e7eb; "
    "color: #111827; font-size: 26px; line-height: 1.35; font-weight: 700; letter-spacing: 0;"
)
H2_STYLE = (
    "margin: 28px 0 14px; padding-left: 10px; border-left: 4px solid #07c160; "
    "color: #111827; font-size: 20px; line-height: 1.45; font-weight: 700; letter-spacing: 0;"
)
H3_STYLE = "margin: 22px 0 12px; color: #111827; font-size: 17px; line-height: 1.55; font-weight: 700; letter-spacing: 0;"
QUOTE_STYLE = (
    "margin: 18px 0; padding: 12px 14px; border-left: 4px solid #d0d7de; "
    "background: #f6f8fa; color: #57606a; font-size: 15px; line-height: 1.8; letter-spacing: 0;"
)
UL_STYLE = "margin: 0 0 18px 1.2em; padding: 0; color: #2b2f36; font-size: 16px; line-height: 1.85;"
LI_STYLE = "margin: 0 0 8px; padding-left: 2px;"
PRE_STYLE = (
    "margin: 18px 0; padding: 12px 14px; background: #f6f8fa; border: 1px solid #e5e7eb; "
    "border-radius: 4px; color: #24292f; font-size: 14px; line-height: 1.7; white-space: pre-wrap; "
    "word-break: break-word; overflow-wrap: anywhere;"
)
INLINE_CODE_STYLE = (
    "padding: 2px 5px; margin: 0 2px; border-radius: 3px; background: #f6f8fa; "
    "color: #d14; font-family: Consolas, Menlo, Monaco, monospace; font-size: 0.92em;"
)


@dataclass(frozen=True)
class WechatArticle:
    title: str
    body_markdown: str
    source_path: Path


class WechatPublisher:
    def __init__(self, profile_dir: Path, runtime_dir: Path | None = None) -> None:
        self.profile_dir = profile_dir
        self.runtime_dir = runtime_dir or profile_dir.parent
        self.preview_dir = self.runtime_dir / "wechat-preview"
        self.screenshot_dir = self.runtime_dir / "screenshots"

    def prepare_preview(self, article_path: Path) -> Path:
        article = parse_markdown_article(article_path)
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        preview_path = self.preview_dir / f"{sanitize_title(article.title, max_length=80)}.html"
        preview_path.write_text(build_preview_html(article), encoding="utf-8")
        return preview_path

    def open_assisted(self, article_path: Path, use_playwright: bool = True, headless: bool = False) -> Path:
        preview_path = self.prepare_preview(article_path)
        if use_playwright:
            try:
                self._open_with_playwright(preview_path, headless=headless)
                return preview_path
            except ImportError:
                pass
        webbrowser.open(preview_path.as_uri())
        webbrowser.open("https://mp.weixin.qq.com/")
        return preview_path

    def try_auto_fill(
        self,
        article_path: Path,
        headless: bool = False,
        login_timeout_seconds: int = 180,
        review_timeout_seconds: int = 0,
    ) -> Path:
        return self.try_auto_fill_many(
            [article_path],
            headless=headless,
            login_timeout_seconds=login_timeout_seconds,
            review_timeout_seconds=review_timeout_seconds,
        )

    def try_auto_fill_many(
        self,
        article_paths: list[Path],
        headless: bool = False,
        login_timeout_seconds: int = 180,
        review_timeout_seconds: int = 0,
    ) -> Path:
        validate_wechat_article_count(article_paths)
        preview_path = self.prepare_preview(article_paths[0])
        articles = [parse_markdown_article(path) for path in article_paths]
        for path in article_paths[1:]:
            self.prepare_preview(path)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Install with: "
                "python -m pip install -e .[wechat] && python -m playwright install chromium"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=headless,
                viewport={"width": 1440, "height": 1000},
                args=["--start-maximized"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(30000)
                page.set_default_navigation_timeout(30000)
                preview = context.new_page()
                preview.goto(preview_path.as_uri(), wait_until="domcontentloaded")
                page.bring_to_front()
                print(f"Opening WeChat home: {WECHAT_HOME_URL}", flush=True)
                page.goto(WECHAT_HOME_URL, wait_until="domcontentloaded")
                save_stage_screenshot(page, self.screenshot_dir, "wechat-01-home.png")
                wait_for_wechat_login(page, login_timeout_seconds)
                print(f"WeChat login detected: {page.url}", flush=True)
                save_stage_screenshot(page, self.screenshot_dir, "wechat-02-logged-in.png")
                editor_url = build_wechat_editor_url(page, is_multiple=len(articles) > 1)
                print(f"Opening WeChat editor: {editor_url}", flush=True)
                page.goto(editor_url, wait_until="domcontentloaded")
                wait_for_editor_ready(page)
                print(f"WeChat editor ready: {page.url}", flush=True)
                save_stage_screenshot(page, self.screenshot_dir, "wechat-03-editor-ready.png")
                for index, article in enumerate(articles, start=1):
                    if index > 1:
                        add_wechat_article_slot(page, index)
                        save_stage_screenshot(page, self.screenshot_dir, f"wechat-{index:02d}-article-ready.png")
                    fill_current_wechat_article(
                        page,
                        article,
                        index=index,
                        upload_cache_dir=self.runtime_dir / "wechat-upload-cache",
                    )
                save_wechat_draft(page)

                screenshot = self.screenshot_dir / "wechat-auto-filled.png"
                page.screenshot(path=str(screenshot), full_page=True)
                if review_timeout_seconds > 0:
                    print(f"WeChat editor filled. Keeping browser open for {review_timeout_seconds} seconds.", flush=True)
                    page.wait_for_timeout(review_timeout_seconds * 1000)
            except Exception as exc:
                screenshot = self.screenshot_dir / "wechat-auto-fill-failed.png"
                try:
                    page.screenshot(path=str(screenshot), full_page=True)
                except Exception:
                    pass
                raise RuntimeError(
                    f"WeChat auto fill failed: {type(exc).__name__}: {exc}. Screenshot: {screenshot}"
                ) from exc
            finally:
                context.close()
        return preview_path

    def _open_with_playwright(self, preview_path: Path, headless: bool = False) -> None:
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=headless,
                viewport={"width": 1440, "height": 1000},
                args=["--start-maximized"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(WECHAT_HOME_URL, wait_until="domcontentloaded")
            preview = context.new_page()
            preview.goto(preview_path.as_uri(), wait_until="domcontentloaded")
            input("微信公众号后台和本地预览已打开。登录并复制内容完成后，按回车关闭浏览器...")
            context.close()


def parse_markdown_article(path: Path) -> WechatArticle:
    text = path.read_text(encoding="utf-8")
    title = path.stem
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            frontmatter = text[4:end].splitlines()
            body = text[end + 4 :].lstrip()
            for line in frontmatter:
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip('"')
                    break
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    return WechatArticle(title=title, body_markdown=body, source_path=path)


def validate_wechat_article_count(article_paths: list[Path]) -> None:
    if not article_paths:
        raise ValueError("At least one Markdown article is required.")
    if len(article_paths) > MAX_WECHAT_ARTICLES:
        raise ValueError(f"WeChat supports at most {MAX_WECHAT_ARTICLES} articles in one publish batch.")


def build_preview_html(article: WechatArticle) -> str:
    body_html = markdown_to_wechat_html(article)
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{html.escape(article.title)}</title>",
            "<style>",
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.75;max-width:760px;margin:32px auto;padding:0 20px;color:#202124}",
            "img,video{max-width:100%;display:block;margin:18px 0}",
            "blockquote{border-left:4px solid #d0d7de;margin:16px 0;padding:2px 14px;color:#57606a}",
            "a{color:#0969da}",
            "</style>",
            "</head>",
            "<body>",
            body_html,
            "</body>",
            "</html>",
        ]
    )


def markdown_to_wechat_html(
    article: WechatArticle,
    embed_local_images: bool = False,
    render_local_videos: bool = False,
    include_title: bool = True,
    skip_duplicate_intro: bool = False,
    include_images: bool = True,
) -> str:
    base_dir = article.source_path.parent
    blocks: list[str] = []
    markdown_buffer: list[str] = []

    def flush_markdown_buffer() -> None:
        if markdown_buffer:
            blocks.extend(render_wechat_markdown_blocks(markdown_buffer, include_h1=include_title))
            markdown_buffer.clear()

    lines = article.body_markdown.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        if not line:
            markdown_buffer.append("")
            continue
        if is_channel_promo_line(line):
            flush_markdown_buffer()
            continue
        image = markdown_image_line(line)
        if image:
            flush_markdown_buffer()
            if not include_images:
                continue
            alt_text, src_value = image
            src = resolve_markdown_image_asset(base_dir, src_value, embed_local_images=embed_local_images)
            alt = html.escape(alt_text)
            blocks.append(
                f'<p style="{P_STYLE}"><img src="{html.escape(src)}" alt="{alt}" '
                'style="max-width:100%;display:block;margin:0 auto 18px;"></p>'
            )
            continue
        video = markdown_link_line(line)
        if video and video[0].strip() in {"", "视频："} and re.search(r"\.(?:mp4|mov|webm)$", video[2], re.I):
            flush_markdown_buffer()
            src_value = video[2]
            if render_local_videos or src_value.startswith(("http://", "https://")):
                src = resolve_markdown_asset(base_dir, src_value)
                label = html.escape(video[1])
                blocks.append(
                    f'<p style="{P_STYLE}">{label}</p>'
                    f'<video controls src="{html.escape(src)}" style="max-width:100%;display:block;margin:0 auto 18px;"></video>'
                )
            continue
        if skip_duplicate_intro and not is_reference_label_line(lines, index) and is_duplicate_title(line, article.title):
            continue
        markdown_buffer.append(line)

    flush_markdown_buffer()
    return "\n".join(blocks)


def render_wechat_markdown_blocks(lines: list[str], include_h1: bool = True) -> list[str]:
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_ordered = False
    quote_lines: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        if paragraph:
            content = "<br>".join(markdown_inline_to_html(item) for item in paragraph)
            blocks.append(f'<p style="{P_STYLE}">{content}</p>')
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_ordered
        if list_items:
            tag = "ol" if list_ordered else "ul"
            items_html = "".join(f'<li style="{LI_STYLE}">{item}</li>' for item in list_items)
            blocks.append(f'<{tag} style="{UL_STYLE}">{items_html}</{tag}>')
            list_items.clear()
            list_ordered = False

    def flush_quote() -> None:
        if quote_lines:
            content = "<br>".join(markdown_inline_to_html(item) for item in quote_lines)
            blocks.append(f'<blockquote style="{QUOTE_STYLE}">{content}</blockquote>')
            quote_lines.clear()

    def flush_code() -> None:
        if code_lines:
            code = html.escape("\n".join(code_lines))
            blocks.append(f'<pre style="{PRE_STYLE}"><code>{code}</code></pre>')
            code_lines.clear()

    def flush_all_text() -> None:
        flush_paragraph()
        flush_list()
        flush_quote()

    for raw_line in lines:
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_all_text()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not line.strip():
            flush_all_text()
            continue
        stripped = line.strip()
        if re.fullmatch(r"[-*_]{3,}", stripped):
            flush_all_text()
            blocks.append('<hr style="border:0;border-top:1px solid #e5e7eb;margin:24px 0;">')
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            flush_list()
            quote_lines.append(stripped.lstrip("> ").strip())
            continue
        heading = re.match(r"^(#{1,3})\s+(?P<title>.+)$", stripped)
        if heading:
            flush_all_text()
            level = len(heading.group(1))
            title = markdown_inline_to_html(heading.group("title").strip())
            if level == 1:
                if include_h1:
                    blocks.append(f'<h1 style="{H1_STYLE}">{title}</h1>')
            elif level == 2:
                blocks.append(f'<h2 style="{H2_STYLE}">{title}</h2>')
            else:
                blocks.append(f'<h3 style="{H3_STYLE}">{title}</h3>')
            continue
        bullet = re.match(r"^[-*+]\s+(?P<value>.+)$", stripped)
        ordered = re.match(r"^\d+[.)、]\s+(?P<value>.+)$", stripped)
        if bullet or ordered:
            flush_paragraph()
            flush_quote()
            ordered_line = ordered is not None
            if list_items and list_ordered != ordered_line:
                flush_list()
            list_ordered = ordered_line
            value = (ordered or bullet).group("value")
            list_items.append(markdown_inline_to_html(value.strip()))
            continue
        flush_list()
        flush_quote()
        paragraph.append(line)

    if in_code:
        flush_code()
    flush_all_text()
    return blocks


def normalize_title_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


def is_duplicate_title(value: str, title: str) -> bool:
    left = normalize_title_text(value)
    right = normalize_title_text(title)
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) < 10:
        return False
    return left in right or right in left


def is_reference_label_line(lines: list[str], index: int) -> bool:
    line = lines[index].strip()
    if not line or URL_ONLY_RE.match(line):
        return False
    if line.startswith(("# ", "## ", "- ", "![", "视频：")):
        return False
    if is_channel_promo_line(line):
        return False
    for next_line in lines[index + 1 :]:
        stripped = next_line.strip()
        if not stripped:
            continue
        return bool(URL_ONLY_RE.match(stripped))
    return False


def clean_wechat_title(title: str) -> str:
    cleaned = []
    for char in title:
        if char in {"\ufe0f", "\u200d"}:
            continue
        if unicodedata.category(char) in {"So", "Sk"}:
            continue
        cleaned.append(char)
    value = re.sub(r"\s+", " ", "".join(cleaned)).strip()
    return value or title.strip()


def markdown_inline_to_html(value: str) -> str:
    parts: list[str] = []
    last = 0
    for match in MARKDOWN_LINK_RE.finditer(value):
        parts.append(markdown_plain_inline_to_html(value[last : match.start()]))
        label = markdown_plain_inline_to_html(match.group("label").strip())
        url = html.escape(match.group("url").strip(), quote=True)
        parts.append(f'<a href="{url}" style="color:#576b95;text-decoration:underline;">{label}</a>')
        last = match.end()
    parts.append(markdown_plain_inline_to_html(value[last:]))
    return "".join(parts)


def markdown_plain_inline_to_html(value: str) -> str:
    parts: list[str] = []
    last = 0
    for match in re.finditer(r"`([^`]+)`", value):
        parts.append(apply_basic_inline_markdown(html.escape(value[last : match.start()])))
        code = html.escape(match.group(1).strip())
        parts.append(f'<code style="{INLINE_CODE_STYLE}">{code}</code>')
        last = match.end()
    parts.append(apply_basic_inline_markdown(html.escape(value[last:])))
    return "".join(parts)


def apply_basic_inline_markdown(value: str) -> str:
    value = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", value)
    value = re.sub(r"__(.+?)__", r"<strong>\1</strong>", value)
    value = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", value)
    value = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<em>\1</em>", value)
    return value


def resolve_markdown_asset(base_dir: Path, value: str) -> str:
    if value.startswith(("http://", "https://", "file://")):
        return value
    return (base_dir / value).resolve().as_uri()


def resolve_markdown_image_asset(base_dir: Path, value: str, embed_local_images: bool = False) -> str:
    if value.startswith(("http://", "https://", "data:", "file://")):
        return value
    path = (base_dir / value).resolve()
    if not embed_local_images or not path.exists():
        return path.as_uri()
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def first_local_image_path(article: WechatArticle) -> Path | None:
    paths = local_image_paths(article)
    return paths[0] if paths else None


def local_image_paths(article: WechatArticle) -> list[Path]:
    base_dir = article.source_path.parent
    paths: list[Path] = []
    for raw_line in article.body_markdown.splitlines():
        image = markdown_image_line(raw_line)
        if not image:
            continue
        src = image[1]
        if is_external_markdown_target(src):
            continue
        path = (base_dir / src).resolve()
        if path.exists():
            paths.append(path)
    return paths


def wait_for_wechat_login(page, timeout_seconds: int) -> None:
    if timeout_seconds <= 0 or appears_wechat_logged_in(page):
        return
    print("Please log in to WeChat Official Account in the opened browser.", flush=True)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(2000)
        if appears_wechat_logged_in(page):
            return
    raise RuntimeError(f"WeChat login was not completed within {timeout_seconds} seconds.")


def save_stage_screenshot(page, screenshot_dir: Path, name: str) -> None:
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_dir / name), full_page=True)
    except Exception:
        pass


def appears_wechat_logged_in(page) -> bool:
    url = page.url.lower()
    if extract_wechat_token(page):
        return True
    if "mp.weixin.qq.com/cgi-bin" in url and "login" not in url and "token=" in url:
        return True
    selectors = [
        "a[href*='appmsg']",
        "a[href*='material']",
        "text=新的创作",
        "text=草稿箱",
        "text=首页",
    ]
    for selector in selectors:
        try:
            if page.locator(selector).first.count():
                return True
        except Exception:
            continue
    return False


def extract_wechat_token(page) -> str | None:
    token = parse_qs(urlsplit(page.url).query).get("token", [None])[0]
    if token:
        return token
    try:
        links = page.locator("a[href*='token=']").evaluate_all("els => els.map(el => el.href)")
    except Exception:
        return None
    for href in links:
        token = parse_qs(urlsplit(href).query).get("token", [None])[0]
        if token:
            return token
    return None


def build_wechat_editor_url(page, is_multiple: bool = False) -> str:
    token = extract_wechat_token(page)
    query = {
        "t": "media/appmsg_edit",
        "action": "edit",
        "type": "10",
        "isNew": "1",
        "isMul": "1" if is_multiple else "0",
        "lang": "zh_CN",
    }
    if token:
        query["token"] = token
    parts = urlsplit(WECHAT_EDITOR_URL)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def wait_for_editor_ready(page, timeout_seconds: int = 45) -> None:
    title_selectors = [
        "#title",
        "textarea#title",
        "textarea[name='title']",
        "input[name='title']",
        "input[placeholder*='标题']",
        "textarea[placeholder*='标题']",
    ]
    body_selectors = [
        "#ueditor_0",
        "iframe[id*='ueditor']",
        "[contenteditable='true']",
        ".ProseMirror",
    ]
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if any(locator_exists(page, selector) for selector in title_selectors) and any(
            locator_exists(page, selector) for selector in body_selectors
        ):
            return
        if "login" in page.url.lower() or locator_exists(page, "text=扫码登录"):
            raise RuntimeError("WeChat editor requires login.")
        page.wait_for_timeout(1000)
    raise RuntimeError("WeChat editor did not become ready.")


def locator_exists(page, selector: str) -> bool:
    try:
        return bool(page.locator(selector).first.count())
    except Exception:
        return False


def locator_is_visible(locator) -> bool:
    try:
        return bool(locator.is_visible(timeout=1000))
    except Exception:
        return False


def human_pause(page, minimum_ms: int | None = None, maximum_ms: int | None = None) -> None:
    low, high = DEFAULT_HUMAN_PAUSE_MS
    if minimum_ms is not None:
        low = minimum_ms
    if maximum_ms is not None:
        high = maximum_ms
    if high < low:
        high = low
    page.wait_for_timeout(random.randint(low, high))


def first_visible_locator(page, selector: str, limit: int = 20):
    locators = page.locator(selector)
    try:
        count = min(locators.count(), limit)
    except Exception:
        return None
    for index in range(count):
        locator = locators.nth(index)
        if locator_is_visible(locator):
            return locator
    return None


def fill_title(page, title: str) -> None:
    selectors = [
        "#title",
        "textarea#title",
        "textarea[name='title']",
        "input[name='title']",
        ".js_title",
        "input[placeholder*='标题']",
        "textarea[placeholder*='标题']",
        "[contenteditable='true'][placeholder*='标题']",
        "[data-placeholder*='标题']",
        ".title input",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count() and locator_is_visible(locator):
            print(f"Filling WeChat title via selector: {selector}", flush=True)
            tag_name = locator.evaluate("node => node.tagName.toLowerCase()", timeout=3000)
            if tag_name in {"input", "textarea"}:
                locator.fill(title, timeout=3000)
            elif locator.evaluate("node => node.isContentEditable", timeout=3000):
                set_editable_html(locator, html.escape(title))
            else:
                click_and_type(page, locator, title)
            return
    title_placeholder = first_visible_locator(page, "text=请在这里输入标题")
    if title_placeholder is not None:
        print("Filling WeChat title via title placeholder.", flush=True)
        click_and_type(page, title_placeholder, title)
        return
    print("Filling WeChat title via first editable fallback.", flush=True)
    click_and_type(page, page.locator("input, textarea, [contenteditable='true']").first, title)


def fill_current_wechat_article(
    page,
    article: WechatArticle,
    index: int,
    upload_cache_dir: Path | None = None,
) -> None:
    title = clean_wechat_title(article.title)
    fill_title(page, title)
    human_pause(page)
    print(f"WeChat article {index} title filled.", flush=True)
    fill_article_body_with_local_uploads(page, article, upload_cache_dir=upload_cache_dir)
    assert_title_not_polluted(page, title, index)
    human_pause(page, 1200, 2400)
    print(f"WeChat article {index} body filled.", flush=True)
    cover_image = first_local_image_path(article)
    if not cover_image:
        print(f"WeChat article {index} has no local cover image.", flush=True)
        return
    if upload_cover(page, cover_image):
        human_pause(page, 1200, 2400)
        print(f"WeChat article {index} cover uploaded: {cover_image}", flush=True)
    else:
        print(f"WeChat article {index} cover upload skipped or failed: {cover_image}", flush=True)


def add_wechat_article_slot(page, index: int) -> None:
    close_wechat_search_component_dialog(page)
    if activate_existing_blank_sidebar_article(page):
        print(f"Reusing existing blank WeChat article slot {index}.", flush=True)
        return
    last_count = count_sidebar_article_cards(page)
    for attempt in range(1, 4):
        page.mouse.wheel(0, -2400)
        human_pause(page, 800, 1500)
        before_count = count_sidebar_article_cards(page)
        if click_new_article_button_in_sidebar(page):
            print(f"Adding WeChat article slot {index} via left sidebar button.", flush=True)
            human_pause(page, 1000, 2000)
            clicked_option = click_write_new_article_option(page)
            if clicked_option:
                human_pause(page, 1800, 3200)
            else:
                print("WeChat new article menu option not found; checking whether editor switched directly.", flush=True)
            if activate_new_article_editor(page, index, before_count):
                return
        elif click_new_article_button_by_text(page):
            print(f"Adding WeChat article slot {index} via text fallback.", flush=True)
            human_pause(page, 1000, 2000)
            clicked_option = click_write_new_article_option(page)
            if clicked_option:
                human_pause(page, 1800, 3200)
            if activate_new_article_editor(page, index, before_count):
                return
        else:
            print(f"WeChat new article button not found on attempt {attempt}; retrying.", flush=True)
        current_count = count_sidebar_article_cards(page)
        if current_count > before_count or current_count > last_count:
            if activate_existing_blank_sidebar_article(page):
                return
            raise RuntimeError(
                f"WeChat sub-article slot {index} was created but could not be activated; "
                "refusing to create a duplicate blank slot."
            )
        if activate_existing_blank_sidebar_article(page):
            return
        last_count = max(last_count, current_count)
        close_wechat_search_component_dialog(page)
        close_visible_popovers(page)
        human_pause(page, 900, 1700)
    raise RuntimeError(f"Could not add WeChat sub-article slot {index}.")


def activate_new_article_editor(page, index: int, before_count: int) -> bool:
    reached_count = wait_for_sidebar_article_card_count(page, before_count + 1)
    for attempt in range(8):
        if title_is_blank_or_placeholder(page):
            wait_for_editor_ready(page)
            return True
        if click_blank_sidebar_article_card(page):
            human_pause(page, 900, 1700)
        elif reached_count and click_newest_sidebar_article_card_after_count(page, before_count):
            human_pause(page, 900, 1700)
        elif click_newest_unselected_sidebar_article_card(page):
            human_pause(page, 900, 1700)
        elif click_latest_sidebar_article_card(page):
            human_pause(page, 900, 1700)
        wait_for_editor_ready(page)
        if title_is_blank_or_placeholder(page):
            return True
        page.wait_for_timeout(700)
    print(f"WeChat sub-article slot {index} did not become active on this attempt.", flush=True)
    return False


def activate_existing_blank_sidebar_article(page) -> bool:
    if not click_blank_sidebar_article_card(page):
        return False
    for _ in range(8):
        wait_for_editor_ready(page)
        if title_is_blank_or_placeholder(page):
            return True
        page.wait_for_timeout(500)
    return False


def wait_for_blank_title(page, index: int, timeout_seconds: int = 10) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if title_is_blank_or_placeholder(page):
            return
        page.wait_for_timeout(500)
    raise RuntimeError(f"WeChat sub-article slot {index} did not become active before filling.")


def title_is_blank_or_placeholder(page) -> bool:
    title = read_current_title(page)
    if not title:
        return True
    normalized = re.sub(r"\s+", "", title)
    return "请在这里输入标题" in normalized or normalized in {"标题", "请输入标题"}


def read_current_title(page) -> str:
    selectors = [
        "#title",
        "textarea#title",
        "textarea[name='title']",
        "input[name='title']",
        ".js_title",
        "input[placeholder*='标题']",
        "textarea[placeholder*='标题']",
        "[contenteditable='true'][placeholder*='标题']",
        "[data-placeholder*='标题']",
        ".title input",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if not locator.count() or not locator_is_visible(locator):
                continue
            value = locator.evaluate(
                """node => {
                        if ('value' in node) return node.value || '';
                        return node.innerText || node.textContent || '';
                    }""",
                timeout=3000,
            )
            return str(value).strip()
        except Exception:
            continue
    return ""


def assert_title_not_polluted(page, expected_title: str, index: int) -> None:
    title = read_current_title(page)
    expected = expected_title.strip()
    if not title or title == expected:
        return
    if title.startswith(expected) and len(title) > len(expected) + 8:
        raise RuntimeError(f"WeChat article {index} body appears to have been inserted into the title field.")


def save_wechat_draft(page) -> None:
    human_pause(page, 1500, 3000)
    page.mouse.wheel(0, 3000)
    human_pause(page, 800, 1600)
    for text in ["保存为草稿", "保存草稿"]:
        if click_visible_button(page, text) or click_visible_text(page, text) or click_button_by_dom_text(page, text):
            print("WeChat draft save clicked.", flush=True)
            human_pause(page, 2500, 5000)
            confirm_save_dialog(page)
            print("WeChat draft save attempted.", flush=True)
            return
    raise RuntimeError("Could not find WeChat save draft button.")


def fill_body(page, body_html: str) -> None:
    body = find_body_editor(page)
    if body is not None:
        set_editable_html(body, body_html)
        return
    selectors = [
        "#ueditor_0",
        "iframe#ueditor_0",
        "iframe[id*='ueditor']",
        "iframe",
        ".ProseMirror",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        if not locator.count() or not locator_is_visible(locator):
            continue
        tag_name = locator.evaluate("node => node.tagName.toLowerCase()", timeout=3000)
        if tag_name == "iframe":
            frame = locator.element_handle(timeout=3000).content_frame()
            if frame:
                set_editable_html(frame.locator("body"), body_html)
                return
        else:
            set_editable_html(locator, body_html)
            return
    raise RuntimeError("No editable WeChat body area found.")


def fill_article_body_with_local_uploads(
    page,
    article: WechatArticle,
    upload_cache_dir: Path | None = None,
) -> None:
    items = build_wechat_body_items(article)
    fill_body(page, "")
    close_wechat_editor_blocking_overlays(page)
    focus_body_editor(page, at_start=True)
    if not items:
        return
    for kind, value in items:
        focus_body_editor(page, at_end=True)
        if kind == "html":
            paste_html_at_cursor(page, str(value))
            human_pause(page, 700, 1400)
            continue
        image_path = Path(value)
        upload_path = prepare_wechat_upload_image(image_path, upload_cache_dir)
        if upload_path != image_path:
            print(f"Normalized WeChat image for upload: {image_path} -> {upload_path}", flush=True)
        if insert_local_body_image(page, upload_path):
            print(f"WeChat body image uploaded: {image_path}", flush=True)
            human_pause(page, 1800, 3600)
        else:
            print(f"WeChat body image upload skipped or failed: {image_path}", flush=True)


def build_wechat_body_items(article: WechatArticle) -> list[tuple[str, str | Path]]:
    base_dir = article.source_path.parent
    items: list[tuple[str, str | Path]] = []
    markdown_buffer: list[str] = []

    def flush_markdown_buffer() -> None:
        if markdown_buffer:
            for block in render_wechat_markdown_blocks(markdown_buffer, include_h1=False):
                items.append(("html", block))
            markdown_buffer.clear()

    lines = article.body_markdown.splitlines()
    for index, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        if not line:
            markdown_buffer.append("")
            continue
        if is_channel_promo_line(line):
            flush_markdown_buffer()
            continue

        image = markdown_image_line(line)
        if image:
            flush_markdown_buffer()
            src = image[1]
            if is_external_markdown_target(src):
                continue
            path = (base_dir / src).resolve()
            if path.exists():
                items.append(("image", path))
            continue

        video = markdown_link_line(line)
        if video and video[0].strip() in {"", "视频："} and re.search(r"\.(?:mp4|mov|webm)$", video[2], re.I):
            flush_markdown_buffer()
            continue

        if line.startswith("# "):
            flush_markdown_buffer()
            continue
        if not is_reference_label_line(lines, index) and is_duplicate_title(line, article.title):
            continue
        markdown_buffer.append(line)

    flush_markdown_buffer()
    return items


def focus_body_editor(page, at_start: bool = False, at_end: bool = False) -> None:
    close_wechat_editor_blocking_overlays(page)
    body = wait_for_body_editor(page)
    if body is None:
        raise RuntimeError("No visible WeChat body editor found.")
    try:
        body.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        body.click(timeout=3000)
    except Exception:
        if close_wechat_editor_blocking_overlays(page):
            body.click(timeout=3000)
        else:
            body.evaluate("node => node.focus()", timeout=3000)
    try:
        assert_active_editor_is_not_title(page)
    except RuntimeError:
        print(f"WeChat editor diagnostics: {describe_wechat_editors(page)!r}", flush=True)
        raise
    if at_start or at_end:
        set_body_cursor(body, to_start=at_start)


def wait_for_body_editor(page, timeout_ms: int = 5000):
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        body = find_body_editor(page)
        if body is not None:
            return body
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(200)


def find_body_editor(page):
    try:
        existing = page.locator('[data-codex-body-editor="1"]').first
        if existing.count() and locator_is_visible(existing):
            return existing
        found = page.evaluate(
            """() => {
                    document.querySelectorAll('[data-codex-body-editor]').forEach((node) => {
                        node.removeAttribute('data-codex-body-editor');
                    });
                    const usable = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 20 &&
                            rect.height > 10 &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none';
                    };
                    const identityText = (node) => [
                        node.id,
                        node.className,
                        node.getAttribute('placeholder'),
                        node.getAttribute('data-placeholder'),
                        node.getAttribute('aria-label'),
                        node.getAttribute('name'),
                        node.getAttribute('role'),
                    ].filter(Boolean).join(' ');
                    const titleLike = (text) => /标题|请输入作者/.test(text) && !/正文|从这里开始|写正文/.test(text);
                    const nodes = [...document.querySelectorAll('[contenteditable="true"], [contenteditable=true], [contenteditable], .ProseMirror')]
                        .filter(usable)
                        .map((node) => {
                            const rect = node.getBoundingClientRect();
                            const text = identityText(node);
                            const parentText = node.parentElement ? identityText(node.parentElement) : '';
                            const grandText = node.parentElement && node.parentElement.parentElement
                                ? identityText(node.parentElement.parentElement)
                                : '';
                            const context = `${text} ${parentText} ${grandText}`;
                            const hasBodyMarker = /正文|从这里开始|写正文|ueditor|ProseMirror/i.test(context);
                            const isTitle = titleLike(text) ||
                                node.closest('#title, .title, .js_title, [data-placeholder*="标题"], [placeholder*="标题"]');
                            return {node, rect, area: rect.width * rect.height, hasBodyMarker, isTitle};
                        })
                        .filter((item) => !item.isTitle && (item.hasBodyMarker || item.rect.height >= 120))
                        .sort((a, b) => {
                            if (a.hasBodyMarker !== b.hasBodyMarker) return a.hasBodyMarker ? -1 : 1;
                            return b.area - a.area;
                        });
                    if (!nodes.length) return false;
                    nodes[0].node.setAttribute('data-codex-body-editor', '1');
                    return true;
                }"""
        )
        if not found:
            return None
        locator = page.locator('[data-codex-body-editor="1"]').first
        if locator.count() and locator_is_visible(locator):
            return locator
    except Exception:
        return None
    return None


def describe_wechat_editors(page):
    try:
        return page.evaluate(
            """() => [...document.querySelectorAll('[contenteditable], .ProseMirror')].map((node) => {
                const rect = node.getBoundingClientRect();
                const value = (name) => node.getAttribute(name) || '';
                return {
                    tag: node.tagName,
                    id: node.id || '',
                    className: String(node.className || ''),
                    placeholder: value('placeholder'),
                    dataPlaceholder: value('data-placeholder'),
                    role: value('role'),
                    contenteditable: value('contenteditable'),
                    marker: value('data-codex-body-editor'),
                    text: String(node.innerText || node.textContent || '').trim().slice(0, 120),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    top: Math.round(rect.top),
                    active: node === document.activeElement || node.contains(document.activeElement),
                    inTitle: Boolean(node.closest('#title, .title, .js_title, [data-placeholder*="标题"], [placeholder*="标题"]')),
                };
            })"""
        )
    except Exception as exc:
        return {"error": str(exc)}


def assert_active_editor_is_not_title(page) -> None:
    is_title = page.evaluate(
        """() => {
                const node = document.activeElement;
                if (!node) return false;
                const editable = node.closest('[contenteditable], input, textarea') || node;
                const text = [
                    editable.id,
                    editable.className,
                    editable.getAttribute && editable.getAttribute('placeholder'),
                    editable.getAttribute && editable.getAttribute('data-placeholder'),
                    editable.getAttribute && editable.getAttribute('aria-label'),
                ].filter(Boolean).join(' ');
                return /标题/.test(text) || Boolean(editable.closest && editable.closest('#title, .title, .js_title, [data-placeholder*="标题"], [placeholder*="标题"]'));
            }"""
    )
    if is_title:
        raise RuntimeError("Focused editor is the title field, not the body field.")


def set_body_cursor(locator, to_start: bool = False) -> None:
    locator.evaluate(
        """(node, toStart) => {
            const editable = node.closest('[contenteditable="true"], [contenteditable=true], [contenteditable]') || node;
            editable.focus();
            const range = document.createRange();
            range.selectNodeContents(editable);
            range.collapse(Boolean(toStart));
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
        }""",
        to_start,
        timeout=3000,
    )


def prepare_wechat_upload_image(image_path: Path, cache_dir: Path | None = None) -> Path:
    try:
        from PIL import Image, ImageOps

        expected_formats = {
            ".jpg": {"JPEG"},
            ".jpeg": {"JPEG"},
            ".png": {"PNG"},
            ".gif": {"GIF"},
            ".webp": {"WEBP"},
        }
        with Image.open(image_path) as source:
            actual_format = str(source.format or "").upper()
            width, height = source.size
            expected = expected_formats.get(image_path.suffix.lower(), set())
            needs_conversion = (
                actual_format not in expected
                or image_path.stat().st_size > WECHAT_IMAGE_MAX_BYTES
                or max(width, height) > WECHAT_IMAGE_MAX_DIMENSION
            )
            if not needs_conversion:
                return image_path

            target_dir = cache_dir or image_path.parent / ".wechat-upload-cache"
            target_dir.mkdir(parents=True, exist_ok=True)
            fingerprint = hashlib.sha1(
                f"{image_path.resolve()}:{image_path.stat().st_mtime_ns}:{image_path.stat().st_size}".encode("utf-8")
            ).hexdigest()[:12]
            target = target_dir / f"{image_path.stem}-{fingerprint}.jpg"
            if target.exists() and target.stat().st_size > 0:
                return target

            image = ImageOps.exif_transpose(source).convert("RGB")
            if max(image.size) > WECHAT_IMAGE_MAX_DIMENSION:
                image.thumbnail(
                    (WECHAT_IMAGE_MAX_DIMENSION, WECHAT_IMAGE_MAX_DIMENSION),
                    Image.Resampling.LANCZOS,
                )
            quality = 90
            while True:
                image.save(target, format="JPEG", quality=quality, optimize=True, progressive=True)
                if target.stat().st_size <= WECHAT_IMAGE_MAX_BYTES or quality <= 60:
                    break
                quality -= 10
            return target
    except Exception as exc:
        print(f"WeChat image normalization skipped for {image_path}: {exc}", flush=True)
        return image_path


def insert_local_body_image(page, image_path: Path) -> bool:
    dismiss_wechat_image_upload_error(page)
    submitted = choose_local_image_from_toolbar(page, image_path)
    if not submitted:
        submitted = set_visible_file_input(page, image_path)
    if not submitted:
        return False
    if wechat_image_upload_error_visible(page):
        dismiss_wechat_image_upload_error(page)
        return False
    return True


def wechat_image_upload_error_visible(page) -> bool:
    try:
        return bool(
            page.evaluate(
                r"""() => [...document.querySelectorAll('body *')].some((node) => {
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
                    return rect.width > 0 && rect.height > 0 &&
                        rect.bottom > 0 && rect.right > 0 &&
                        rect.top < window.innerHeight && rect.left < window.innerWidth &&
                        style.visibility !== 'hidden' && style.display !== 'none' &&
                        /上传失败|上传文件过大|图片上传失败|文件过大/.test(text);
                })"""
            )
        )
    except Exception:
        return False


def dismiss_wechat_image_upload_error(page) -> bool:
    try:
        point = page.evaluate(
            r"""() => {
                const visible = (node) => {
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.right > 0 &&
                        rect.top < window.innerHeight && rect.left < window.innerWidth &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                };
                const error = [...document.querySelectorAll('div, span')].find((node) =>
                    visible(node) && /上传失败|上传文件过大|图片上传失败|文件过大/.test(
                        (node.innerText || node.textContent || '').replace(/\s+/g, '')
                    )
                );
                if (!error) return null;
                const root = error.closest('[role="dialog"], .weui-desktop-dialog, .weui-desktop-dialog__wrp, .dialog_wrp, .popover') || error.parentElement;
                const close = root && [...root.querySelectorAll('button, a, i, span, div')]
                    .find((node) => visible(node) && /^(×|x|关闭|知道了|确定)$/i.test((node.innerText || node.textContent || '').trim()));
                if (close) {
                    const rect = close.getBoundingClientRect();
                    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
                }
                const rect = root ? root.getBoundingClientRect() : error.getBoundingClientRect();
                return {x: rect.right - 12, y: rect.top + 12};
            }"""
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        page.wait_for_timeout(400)
        return True
    except Exception:
        return False


def choose_local_image_from_toolbar(page, image_path: Path) -> bool:
    image_button = first_visible_locator(page, "text=图片")
    if image_button is None:
        return False
    try:
        with page.expect_file_chooser(timeout=4000) as chooser_info:
            image_button.click(timeout=3000)
        chooser_info.value.set_files(str(image_path))
        human_pause(page, 4500, 7000)
        return True
    except Exception:
        pass
    try:
        image_button.click(timeout=3000)
        human_pause(page, 800, 1400)
    except Exception:
        return False
    for text in ["本地上传", "上传图片", "本地图片"]:
        option = first_visible_locator(page, f"text={text}")
        if option is None:
            continue
        try:
            with page.expect_file_chooser(timeout=5000) as chooser_info:
                option.click(timeout=3000)
            chooser_info.value.set_files(str(image_path))
            human_pause(page, 4500, 7000)
            return True
        except Exception:
            continue
    return False


def set_visible_file_input(page, image_path: Path) -> bool:
    inputs = page.locator("input[type='file']")
    try:
        count = inputs.count()
    except Exception:
        return False
    for index in range(count):
        input_locator = inputs.nth(index)
        try:
            input_locator.set_input_files(str(image_path), timeout=3000)
            human_pause(page, 4500, 7000)
            return True
        except Exception:
            continue
    return False


def set_editable_html(locator, body_html: str) -> None:
    locator.evaluate(
        """(node, html) => {
            node.innerHTML = html;
            node.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertHTML'}));
            node.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        body_html,
        timeout=3000,
    )


def set_nearest_editable_html(locator, body_html: str) -> None:
    locator.evaluate(
        """(node, html) => {
            const editable = node.closest('[contenteditable="true"], [contenteditable=true], [contenteditable]');
            if (!editable) {
                throw new Error('No nearest editable area found');
            }
            editable.innerHTML = html;
            editable.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertHTML'}));
            editable.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        body_html,
        timeout=3000,
    )


def click_and_type(page, locator, text: str) -> None:
    locator.click(timeout=3000)
    human_pause(page, 300, 900)
    page.keyboard.press("Control+A")
    human_pause(page, 200, 600)
    page.keyboard.insert_text(text)
    human_pause(page, 500, 1100)


def paste_html_at_locator(page, locator, body_html: str) -> None:
    locator.click(timeout=3000)
    page.evaluate(
        """async html => {
            if (navigator.clipboard && window.ClipboardItem) {
                const item = new ClipboardItem({
                    'text/html': new Blob([html], {type: 'text/html'}),
                    'text/plain': new Blob([html.replace(/<[^>]+>/g, '\\n')], {type: 'text/plain'})
                });
                await navigator.clipboard.write([item]);
                return true;
            }
            return false;
        }""",
        body_html,
    )
    page.keyboard.press("Control+V")


def paste_html_at_cursor(page, body_html: str) -> None:
    copied = page.evaluate(
        """async html => {
            if (navigator.clipboard && window.ClipboardItem) {
                const item = new ClipboardItem({
                    'text/html': new Blob([html], {type: 'text/html'}),
                    'text/plain': new Blob([html.replace(/<[^>]+>/g, '\\n')], {type: 'text/plain'})
                });
                await navigator.clipboard.write([item]);
                return true;
            }
            return false;
        }""",
        body_html,
    )
    if copied:
        page.keyboard.press("Control+V")
        return
    page.evaluate(
        """html => {
            document.execCommand('insertHTML', false, html);
            const active = document.activeElement;
            const editable = active && active.closest && active.closest('[contenteditable]');
            if (editable) {
                editable.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertHTML'}));
            }
        }""",
        body_html,
    )


def upload_cover(page, image_path: Path) -> bool:
    cover_locator = first_visible_locator(page, "text=拖拽或选择封面") or first_visible_locator(page, "text=选择封面")
    if cover_locator is None:
        return False
    try:
        page.mouse.wheel(0, 1000)
        human_pause(page, 700, 1400)
        cover_locator.click(timeout=3000)
        human_pause(page, 1000, 2000)
        if choose_cover_from_body(page):
            return True
        close_cover_picker(page)
        return False
    except Exception:
        close_cover_picker(page)
        return False


def choose_cover_from_body(page) -> bool:
    option = first_visible_locator(page, "text=从正文选择")
    if option is None:
        return False
    option.click(timeout=3000)
    human_pause(page, 1600, 2800)
    thumbnail_clicked = False
    for _ in range(8):
        if click_cover_thumbnail(page):
            thumbnail_clicked = True
            break
        page.wait_for_timeout(500)
    if not thumbnail_clicked:
        return False
    human_pause(page, 800, 1600)
    print("WeChat cover thumbnail selected.", flush=True)
    if not click_visible_button(page, "下一步"):
        return False
    print("WeChat cover next clicked.", flush=True)
    human_pause(page, 1500, 2800)
    confirm_cover_dialog(page)
    human_pause(page, 1800, 3200)
    return not cover_picker_visible(page)


def close_cover_picker(page) -> bool:
    if not cover_picker_visible(page):
        return False
    for text in ["取消", "关闭"]:
        if click_visible_button(page, text) or click_visible_text(page, text):
            human_pause(page, 500, 1000)
            return True
    try:
        page.keyboard.press("Escape")
        human_pause(page, 500, 1000)
        return not cover_picker_visible(page)
    except Exception:
        return False


def choose_cover_local_upload(page, image_path: Path) -> bool:
    for text in ["本地上传", "上传图片", "从图片库选择"]:
        option = first_visible_locator(page, f"text={text}")
        if option is None:
            continue
        try:
            with page.expect_file_chooser(timeout=5000) as chooser_info:
                option.click(timeout=3000)
            chooser_info.value.set_files(str(image_path))
            human_pause(page, 3000, 5200)
            confirm_cover_dialog(page)
            return True
        except Exception:
            continue
    return False


def confirm_cover_dialog(page) -> None:
    for _ in range(3):
        clicked = False
        for text in ["确认", "确定", "完成", "下一步"]:
            if click_visible_button(page, text):
                human_pause(page, 1000, 2200)
                clicked = True
                break
            if click_visible_text(page, text):
                human_pause(page, 1000, 2200)
                clicked = True
                break
        if not clicked:
            return


def confirm_save_dialog(page) -> None:
    for _ in range(4):
        clicked = False
        for text in ["确定", "确认", "我知道了", "知道了"]:
            if click_visible_button(page, text) or click_visible_text(page, text) or click_button_by_dom_text(page, text):
                human_pause(page, 1000, 2200)
                clicked = True
                break
        if not clicked:
            return


def click_button_by_dom_text(page, text: str) -> bool:
    try:
        point = page.evaluate(
            """text => {
                    const selectors = 'button, a, span, div, .weui-desktop-btn, .btn';
                    const elements = [...document.querySelectorAll(selectors)]
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            const label = (el.innerText || el.textContent || '').trim();
                            return {el, rect, area: rect.width * rect.height, style, label};
                        })
                        .filter(({label, rect, area, style}) =>
                            label.includes(text) &&
                            area > 0 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.pointerEvents !== 'none'
                        )
                        .sort((a, b) => a.area - b.area);
                    if (!elements.length) return null;
                    const rect = elements[0].rect;
                    const x = Math.min(Math.max(rect.left + rect.width / 2, 4), window.innerWidth - 4);
                    const y = Math.min(Math.max(rect.top + rect.height / 2, 4), window.innerHeight - 4);
                    return {x, y};
                }""",
            text,
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        return True
    except Exception:
        return False


def cover_picker_visible(page) -> bool:
    return first_visible_locator(page, "text=请从正文插入的图片和视频封面中选择封面") is not None


def click_visible_button(page, text: str) -> bool:
    selectors = [
        f"button:has-text('{text}')",
        f"a:has-text('{text}')",
        f".weui-desktop-btn:has-text('{text}')",
        f".btn:has-text('{text}')",
    ]
    for selector in selectors:
        locators = page.locator(selector)
        try:
            count = min(locators.count(), 10)
        except Exception:
            continue
        for index in range(count):
            locator = locators.nth(index)
            if not locator_is_visible(locator):
                continue
            try:
                locator.click(timeout=2000, force=True)
                return True
            except Exception:
                continue
    return False


def click_cover_thumbnail(page) -> bool:
    try:
        point = page.evaluate(
            """() => {
                    const roots = [...document.querySelectorAll('[role="dialog"], .weui-desktop-dialog, .weui-desktop-dialog__wrp, .dialog_wrp, .popover, .weui-desktop-popover, body')]
                        .filter((root) => {
                            const text = root.innerText || '';
                            return text.includes('选择图片') || text.includes('请从正文') || text.includes('请选择') || root === document.body;
                        });
                    const dialogRoot = roots.find((root) => root !== document.body) || document.body;
                    const nodes = [...dialogRoot.querySelectorAll('img, div, span, a, button')];
                    const candidates = nodes
                        .map((node) => {
                            const rect = node.getBoundingClientRect();
                            const style = window.getComputedStyle(node);
                            const hasImage = node.tagName === 'IMG' || (style.backgroundImage && style.backgroundImage !== 'none');
                            let clickable = node;
                            for (let i = 0; i < 5 && clickable.parentElement; i += 1) {
                                const parent = clickable.parentElement;
                                const parentRect = parent.getBoundingClientRect();
                                if (parentRect.width >= rect.width && parentRect.height >= rect.height) {
                                    clickable = parent;
                                }
                                if (/^(LI|A|BUTTON)$/.test(parent.tagName) || parent.getAttribute('role') === 'button') {
                                    clickable = parent;
                                    break;
                                }
                            }
                            const clickRect = clickable.getBoundingClientRect();
                            return {node, clickable, rect, clickRect, area: rect.width * rect.height, style, hasImage};
                        })
                        .filter(({rect, area, style, hasImage}) =>
                            hasImage &&
                            area > 900 &&
                            area < 80000 &&
                            rect.width > 24 &&
                            rect.height > 24 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none'
                        )
                        .sort((a, b) => b.area - a.area);
                    if (!candidates.length) return null;
                    const rect = candidates[0].clickRect;
                    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
                }"""
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        page.wait_for_timeout(500)
        return True
    except Exception:
        return False


def click_visible_text(page, text: str) -> bool:
    try:
        point = page.evaluate(
            """text => {
                    const elements = [...document.querySelectorAll('button, a, span, div')]
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return {el, rect, area: rect.width * rect.height, style};
                        })
                        .filter(({el, rect, area, style}) =>
                            (el.innerText || el.textContent || '').trim().includes(text) &&
                            area > 0 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.pointerEvents !== 'none'
                        )
                        .sort((a, b) => a.area - b.area);
                    if (!elements.length) return null;
                    const rect = elements[0].rect;
                    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
                }""",
            text,
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        return True
    except Exception:
        return False


def click_write_new_article_option(page) -> bool:
    for selector in ["text=写新文章", "text=新建图文", "text=图文消息"]:
        option = first_visible_locator(page, selector)
        if option is None:
            continue
        try:
            print(f"Choosing WeChat new article option via selector: {selector}", flush=True)
            option.click(timeout=5000)
            return True
        except Exception:
            continue
    return click_visible_text(page, "写新文章") or click_write_new_article_option_by_dom(page)


def click_write_new_article_option_by_dom(page) -> bool:
    try:
        point = page.evaluate(
            """() => {
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 20 &&
                            rect.height > 12 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.pointerEvents !== 'none';
                    };
                    const candidates = [...document.querySelectorAll('button, a, span, div, li')]
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const text = (el.innerText || el.textContent || '').replace(/\\s+/g, '');
                            const inToolbar = Boolean(el.closest('[role="toolbar"], .toolbar, [class*="toolbar"], [class*="tool_bar"], [class*="edui-toolbar"]'));
                            return {el, rect, text, inToolbar};
                        })
                        .filter(({rect, text, inToolbar}) =>
                            !inToolbar &&
                            /写新文章|新建图文|图文消息/.test(text) &&
                            rect.left >= 0 &&
                            rect.left < 620 &&
                            rect.top > 80
                        )
                        .sort((a, b) => a.rect.width * a.rect.height - b.rect.width * b.rect.height);
                    if (!candidates.length) return null;
                    const rect = candidates[0].rect;
                    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
                }"""
        )
        if not point:
            return False
        print("Choosing WeChat new article option via DOM fallback.", flush=True)
        page.mouse.click(point["x"], point["y"])
        return True
    except Exception:
        return False


def close_visible_popovers(page) -> None:
    try:
        page.keyboard.press("Escape")
        human_pause(page, 300, 700)
    except Exception:
        pass


def close_wechat_editor_blocking_overlays(page) -> bool:
    try:
        page.keyboard.press("Escape")
        human_pause(page, 200, 500)
    except Exception:
        pass
    try:
        result = page.evaluate(
            """() => {
                    const visible = (node) => {
                        if (!node) return false;
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.opacity !== '0';
                    };
                    const nearestOverlay = (node) =>
                        node.closest('[data-transfer="true"], [v-transfer-dom], [weui="true"], [role="dialog"], .weui-desktop-dialog__wrp, .weui-desktop-dialog, .dialog_wrp, .popover') ||
                        node.parentElement;
                    const roots = new Set();
                    document
                        .querySelectorAll('img[src*="topic_card"], img[src*="topic_card_sticker"], img[src*="sticker_edu"]')
                        .forEach((img) => {
                            const src = String(img.getAttribute('src') || '');
                            if (visible(img) && /topic_card|topic_card_sticker|sticker_edu/.test(src)) {
                                roots.add(nearestOverlay(img));
                            }
                        });
                    document
                        .querySelectorAll('[data-transfer="true"], [v-transfer-dom], [weui="true"], [role="dialog"], .weui-desktop-dialog__wrp, .weui-desktop-dialog, .dialog_wrp, .popover')
                        .forEach((node) => {
                            if (!visible(node)) return;
                            const rect = node.getBoundingClientRect();
                            const text = (node.innerText || node.textContent || '').trim();
                            const hasTopicImage = Boolean(node.querySelector('img[src*="topic_card"], img[src*="topic_card_sticker"], img[src*="sticker_edu"]'));
                            const coversEditor = rect.width > 180 && rect.height > 120 && rect.left < window.innerWidth * 0.85 && rect.right > window.innerWidth * 0.2;
                            if ((hasTopicImage || /话题|选题|卡片|贴纸|教育|写作|推荐/.test(text)) && coversEditor) {
                                roots.add(node);
                            }
                        });
                    const overlays = [...roots].filter(visible);
                    for (const root of overlays) {
                        const close = [...root.querySelectorAll('button, a, i, span, div')]
                            .map((el) => {
                                const rect = el.getBoundingClientRect();
                                const style = window.getComputedStyle(el);
                                const text = (el.innerText || el.textContent || '').trim();
                                const label = [
                                    text,
                                    el.getAttribute('aria-label') || '',
                                    el.getAttribute('title') || '',
                                    String(el.className || ''),
                                ].join(' ');
                                return {el, rect, style, label, area: rect.width * rect.height};
                            })
                            .filter(({rect, style, label, area}) =>
                                area > 0 &&
                                area < 6000 &&
                                rect.bottom > 0 &&
                                rect.right > 0 &&
                                rect.top < window.innerHeight &&
                                rect.left < window.innerWidth &&
                                style.visibility !== 'hidden' &&
                                style.display !== 'none' &&
                                style.pointerEvents !== 'none' &&
                                (/^(×|x)$/i.test(label.trim()) || /close|cancel|关闭|取消|知道了|我知道了/.test(label))
                            )
                            .sort((a, b) => b.rect.top - a.rect.top || b.rect.right - a.rect.right);
                        if (close.length) {
                            close[0].el.click();
                            return {clicked: true, hidden: 0};
                        }
                    }
                    let hidden = 0;
                    for (const root of overlays) {
                        root.setAttribute('data-codex-hidden-overlay', '1');
                        root.style.setProperty('pointer-events', 'none', 'important');
                        root.style.setProperty('display', 'none', 'important');
                        hidden += 1;
                    }
                    return {clicked: false, hidden};
                }"""
        )
        if isinstance(result, dict):
            if result.get("clicked"):
                print("WeChat editor blocking overlay closed.", flush=True)
                human_pause(page, 500, 1000)
                return True
            if result.get("hidden"):
                print("WeChat editor blocking overlay hidden.", flush=True)
                human_pause(page, 300, 700)
                return True
    except Exception:
        pass
    return False


def count_sidebar_article_cards(page) -> int:
    try:
        return int(
            page.evaluate(
                """() => {
                        const visible = (node) => {
                            const rect = node.getBoundingClientRect();
                            const style = window.getComputedStyle(node);
                            return rect.width > 60 &&
                                rect.height > 40 &&
                                rect.bottom > 0 &&
                                rect.right > 0 &&
                                rect.top < window.innerHeight &&
                                rect.left < window.innerWidth &&
                                style.visibility !== 'hidden' &&
                                style.display !== 'none';
                        };
                        const cards = [...document.querySelectorAll('li, div, a')]
                            .map((el) => {
                                const rect = el.getBoundingClientRect();
                                const text = (el.innerText || el.textContent || '').trim();
                                const hasMedia = Boolean(el.querySelector('img')) ||
                                    window.getComputedStyle(el).backgroundImage !== 'none';
                                const inDialog = Boolean(el.closest('[role="dialog"], .weui-desktop-dialog, .weui-desktop-dialog__wrp, .dialog_wrp, .popover, .weui-desktop-popover'));
                                return {el, rect, text, hasMedia, inDialog};
                            })
                            .filter(({el, rect, text, hasMedia, inDialog}) =>
                                visible(el) &&
                                !inDialog &&
                                rect.left >= 0 &&
                                rect.left < 430 &&
                                rect.right <= 470 &&
                                rect.top > 60 &&
                                rect.height >= 36 &&
                                rect.height <= 180 &&
                                text &&
                                !/新建内容|历史版本|原创|广告|留言/.test(text) &&
                                (hasMedia || text.length > 8 || text === '标题')
                            );
                        cards.sort((a, b) => a.rect.top - b.rect.top || b.rect.height - a.rect.height);
                        const groups = [];
                        for (const item of cards) {
                            const top = Math.round(item.rect.top);
                            const bottom = Math.round(item.rect.bottom);
                            const overlap = groups.find((group) =>
                                Math.min(group.bottom, bottom) - Math.max(group.top, top) > 18 ||
                                Math.abs(group.top - top) < 16
                            );
                            if (overlap) {
                                overlap.top = Math.min(overlap.top, top);
                                overlap.bottom = Math.max(overlap.bottom, bottom);
                            } else {
                                groups.push({top, bottom});
                            }
                        }
                        return groups.length;
                    }"""
            )
        )
    except Exception:
        return 0


def wait_for_sidebar_article_card_count(page, expected_count: int, timeout_seconds: int = 12) -> bool:
    if expected_count <= 1:
        return True
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if count_sidebar_article_cards(page) >= expected_count:
            return True
        page.wait_for_timeout(500)
    print(
        f"WeChat sidebar article count did not reach {expected_count}; continuing with editor state check.",
        flush=True,
    )
    return False


def click_newest_sidebar_article_card_after_count(page, before_count: int) -> bool:
    try:
        point = page.evaluate(
            """(beforeCount) => {
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 60 &&
                            rect.height > 36 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.pointerEvents !== 'none';
                    };
                    const rawCandidates = [...document.querySelectorAll('li, div, a')]
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            const text = (el.innerText || el.textContent || '').trim();
                            const hasMedia = Boolean(el.querySelector('img')) || style.backgroundImage !== 'none';
                            const inDialog = Boolean(el.closest('[role="dialog"], .weui-desktop-dialog, .weui-desktop-dialog__wrp, .dialog_wrp, .popover, .weui-desktop-popover'));
                            return {el, rect, text, hasMedia, inDialog};
                        })
                        .filter(({rect, text, hasMedia, inDialog}) =>
                            !inDialog &&
                            rect.left >= 0 &&
                            rect.left < 430 &&
                            rect.right <= 470 &&
                            rect.top > 60 &&
                            rect.height >= 36 &&
                            rect.height <= 180 &&
                            text &&
                            !/新建内容|添加|历史版本|操作时间|操作人|来源|操作|原创|广告|留言/.test(text) &&
                            (hasMedia || text.length > 2 || text === '标题')
                        )
                        .sort((a, b) => a.rect.top - b.rect.top || b.rect.height - a.rect.height);
                    const groups = [];
                    for (const item of rawCandidates) {
                        const top = Math.round(item.rect.top);
                        const bottom = Math.round(item.rect.bottom);
                        const group = groups.find((current) =>
                            Math.min(current.bottom, bottom) - Math.max(current.top, top) > 18 ||
                            Math.abs(current.top - top) < 16
                        );
                        if (group) {
                            group.top = Math.min(group.top, top);
                            group.bottom = Math.max(group.bottom, bottom);
                            if (item.rect.width * item.rect.height > group.area) {
                                group.el = item.el;
                                group.rect = item.rect;
                                group.area = item.rect.width * item.rect.height;
                            }
                        } else {
                            groups.push({top, bottom, el: item.el, rect: item.rect, area: item.rect.width * item.rect.height});
                        }
                    }
                    if (groups.length <= beforeCount || !groups.length) return null;
                    groups.sort((a, b) => b.top - a.top);
                    const rect = groups[0].rect;
                    return {x: rect.left + rect.width / 2, y: rect.top + Math.min(rect.height / 2, 62)};
                }""",
            before_count,
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        human_pause(page, 800, 1500)
        return True
    except Exception:
        return False


def click_blank_sidebar_article_card(page) -> bool:
    try:
        point = page.evaluate(
            """() => {
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.pointerEvents !== 'none';
                    };
                    const textOf = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, '');
                    const candidates = [...document.querySelectorAll('div, li, a, span')]
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const text = textOf(el);
                            const inDialog = Boolean(el.closest('[role="dialog"], .weui-desktop-dialog, .weui-desktop-dialog__wrp, .dialog_wrp, .popover, .weui-desktop-popover'));
                            let clickable = el;
                            for (let i = 0; i < 6 && clickable.parentElement; i += 1) {
                                const parent = clickable.parentElement;
                                const parentRect = parent.getBoundingClientRect();
                                if (parentRect.left >= 0 &&
                                    parentRect.left < 430 &&
                                    parentRect.right <= 470 &&
                                    parentRect.height >= 40 &&
                                    parentRect.height <= 140 &&
                                    parentRect.width >= 120) {
                                    clickable = parent;
                                }
                            }
                            const clickRect = clickable.getBoundingClientRect();
                            return {el, rect, clickRect, text, inDialog};
                        })
                        .filter(({rect, text, inDialog}) =>
                            !inDialog &&
                            text === '标题' &&
                            rect.left >= 0 &&
                            rect.left < 430 &&
                            rect.right <= 470 &&
                            rect.top > 70
                        )
                        .sort((a, b) => b.rect.top - a.rect.top);
                    if (!candidates.length) return null;
                    const rect = candidates[0].clickRect;
                    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
                }"""
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        human_pause(page, 800, 1600)
        return True
    except Exception:
        return False


def click_latest_sidebar_article_card(page) -> bool:
    try:
        point = page.evaluate(
            """() => {
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 60 &&
                            rect.height > 40 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.pointerEvents !== 'none';
                    };
                    const candidates = [...document.querySelectorAll('li, div, a')]
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            const text = (el.innerText || el.textContent || '').trim();
                            const hasMedia = Boolean(el.querySelector('img')) || style.backgroundImage !== 'none';
                            const inDialog = Boolean(el.closest('[role="dialog"], .weui-desktop-dialog, .weui-desktop-dialog__wrp, .dialog_wrp, .popover, .weui-desktop-popover'));
                            return {el, rect, text, hasMedia, inDialog};
                        })
                        .filter(({rect, text, hasMedia, inDialog}) =>
                            !inDialog &&
                            rect.left >= 0 &&
                            rect.left < 430 &&
                            rect.right <= 470 &&
                            rect.top > 60 &&
                            text &&
                            !/新建内容|历史版本|原创|广告|留言/.test(text) &&
                            (hasMedia || text.length > 8 || text === '标题')
                        )
                        .sort((a, b) => {
                            const selectedA = /selected|active|current/.test(String(a.el.className || '')) ? 1 : 0;
                            const selectedB = /selected|active|current/.test(String(b.el.className || '')) ? 1 : 0;
                            if (selectedA !== selectedB) return selectedA - selectedB;
                            return b.rect.top - a.rect.top;
                        });
                    if (!candidates.length) return null;
                    const rect = candidates[0].rect;
                    return {x: rect.left + rect.width / 2, y: rect.top + Math.min(rect.height / 2, 60)};
                }"""
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        human_pause(page, 800, 1500)
        return True
    except Exception:
        return False


def click_newest_unselected_sidebar_article_card(page) -> bool:
    try:
        point = page.evaluate(
            """() => {
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 60 &&
                            rect.height > 40 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.pointerEvents !== 'none';
                    };
                    const textOf = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, '');
                    const selected = (node) => {
                        for (let current = node; current; current = current.parentElement) {
                            const cls = String(current.className || '');
                            const aria = current.getAttribute('aria-selected') || '';
                            if (/selected|active|current|checked|appmsg_item_v2_selected/i.test(cls) || aria === 'true') return true;
                            const rect = current.getBoundingClientRect();
                            if (rect.left >= 0 && rect.left < 430 && rect.right <= 470 && /border|outline/.test(String(current.getAttribute('style') || ''))) return true;
                        }
                        return false;
                    };
                    const candidates = [...document.querySelectorAll('li, div, a')]
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const text = textOf(el);
                            const style = window.getComputedStyle(el);
                            const hasMedia = Boolean(el.querySelector('img')) || style.backgroundImage !== 'none';
                            const inDialog = Boolean(el.closest('[role="dialog"], .weui-desktop-dialog, .weui-desktop-dialog__wrp, .dialog_wrp, .popover, .weui-desktop-popover'));
                            return {el, rect, text, hasMedia, inDialog, selected: selected(el)};
                        })
                        .filter(({rect, text, hasMedia, inDialog, selected}) =>
                            !selected &&
                            !inDialog &&
                            rect.left >= 0 &&
                            rect.left < 430 &&
                            rect.right <= 470 &&
                            rect.top > 60 &&
                            text &&
                            !/新建内容|添加|历史版本|操作时间|操作人|来源|操作/.test(text) &&
                            (hasMedia || text.length > 2 || text === '标题')
                        )
                        .sort((a, b) => b.rect.top - a.rect.top);
                    if (!candidates.length) return null;
                    const rect = candidates[0].rect;
                    return {x: rect.left + rect.width / 2, y: rect.top + Math.min(rect.height / 2, 60)};
                }"""
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        human_pause(page, 800, 1500)
        return True
    except Exception:
        return False


def close_wechat_search_component_dialog(page) -> bool:
    try:
        point = page.evaluate(
            """() => {
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none';
                    };
                    const dialogs = [...document.querySelectorAll('[role="dialog"], .weui-desktop-dialog, .weui-desktop-dialog__wrp, .dialog_wrp')]
                        .filter((node) => visible(node) && /插入搜索组件|搜索词|推荐搜索|添加关键词/.test(node.innerText || node.textContent || ''));
                    if (!dialogs.length) return null;
                    const dialog = dialogs[0];
                    const closeCandidates = [...dialog.querySelectorAll('button, a, i, span, div')]
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            const text = (el.innerText || el.textContent || '').trim();
                            const cls = String(el.className || '');
                            return {el, rect, area: rect.width * rect.height, style, text, cls};
                        })
                        .filter(({rect, area, style, text, cls}) =>
                            area > 0 &&
                            area < 4000 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.pointerEvents !== 'none' &&
                            (/^(×|x)$/i.test(text) || /close|cancel|关闭/.test(cls + text))
                        )
                        .sort((a, b) => b.rect.left - a.rect.left);
                    if (closeCandidates.length) {
                        const rect = closeCandidates[0].rect;
                        return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
                    }
                    const rect = dialog.getBoundingClientRect();
                    return {x: rect.right - 28, y: rect.top + 28};
                }"""
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        human_pause(page, 500, 1000)
        return True
    except Exception:
        return False


def click_new_article_button_in_sidebar(page) -> bool:
    try:
        point = page.evaluate(
            """labels => {
                    const normalizedLabels = labels.map((label) => label.replace(/\\s+/g, ''));
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 0 &&
                            rect.height > 0 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.pointerEvents !== 'none';
                    };
                    const textOf = (node) => (node.innerText || node.textContent || '').replace(/\\s+/g, '');
                    const nodes = [...document.querySelectorAll('button, a, span, div, li')]
                        .filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const text = textOf(el);
                            const rawText = (el.innerText || el.textContent || '').trim();
                            const cls = String(el.className || '');
                            const aria = el.getAttribute('aria-label') || '';
                            const title = el.getAttribute('title') || '';
                            const inDialog = Boolean(el.closest('[role="dialog"], .weui-desktop-dialog, .weui-desktop-dialog__wrp, .dialog_wrp, .popover, .weui-desktop-popover'));
                            const inToolbar = Boolean(el.closest('[role="toolbar"], .toolbar, [class*="toolbar"], [class*="tool_bar"], [class*="edui-toolbar"]'));
                            const hasMedia = Boolean(el.querySelector('img')) || window.getComputedStyle(el).backgroundImage !== 'none';
                            let clickable = el;
                            for (let i = 0; i < 5 && clickable.parentElement; i += 1) {
                                const parent = clickable.parentElement;
                                const parentText = textOf(parent);
                                const parentRect = parent.getBoundingClientRect();
                                if (/^(BUTTON|A|LI)$/.test(parent.tagName) || parent.getAttribute('role') === 'button') {
                                    clickable = parent;
                                    break;
                                }
                                if (normalizedLabels.some((label) => parentText === label || parentText === `+${label}`) &&
                                    parentRect.left < 430 &&
                                    parentRect.right <= 460) {
                                    clickable = parent;
                                }
                            }
                            const clickRect = clickable.getBoundingClientRect();
                            const clickableText = textOf(clickable);
                            const marker = `${rawText} ${cls} ${aria} ${title} ${clickableText}`;
                            const looksAdd = /\\+|add|create|new|append|新建|新增|添加/i.test(marker);
                            return {el, rect, clickRect, text, clickableText, inDialog, inToolbar, hasMedia, looksAdd};
                        })
                        .filter(({rect, clickRect, text, clickableText, inDialog, inToolbar, hasMedia, looksAdd}) =>
                            normalizedLabels.some((label) =>
                                text === label ||
                                text === `+${label}` ||
                                clickableText === label ||
                                clickableText === `+${label}`
                            ) &&
                            !inDialog &&
                            !inToolbar &&
                            !hasMedia &&
                            looksAdd &&
                            rect.left >= 0 &&
                            rect.left < 430 &&
                            rect.right <= 460 &&
                            rect.top > 120 &&
                            clickRect.width > 40 &&
                            clickRect.height > 16 &&
                            clickRect.height < 90
                        )
                        .sort((a, b) => {
                            const aExact = normalizedLabels.some((label) => a.text === label || a.text === `+${label}`) ? 0 : 1;
                            const bExact = normalizedLabels.some((label) => b.text === label || b.text === `+${label}`) ? 0 : 1;
                            if (aExact !== bExact) return aExact - bExact;
                            return b.rect.top - a.rect.top;
                        });
                    if (!nodes.length) return null;
                    const rect = nodes[0].clickRect;
                    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
                }""",
            NEW_ARTICLE_LABELS,
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        return True
    except Exception:
        return False


def click_new_article_button_by_text(page) -> bool:
    try:
        point = page.evaluate(
            """labels => {
                    const elements = [...document.querySelectorAll('button, a, span, div, li')]
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            const text = (el.innerText || el.textContent || '').trim();
                            const inDialog = Boolean(el.closest('[role="dialog"], .weui-desktop-dialog, .weui-desktop-dialog__wrp, .dialog_wrp, .popover, .weui-desktop-popover'));
                            const inToolbar = Boolean(el.closest('[role="toolbar"], .toolbar, [class*="toolbar"], [class*="tool_bar"], [class*="edui-toolbar"]'));
                            return {el, rect, area: rect.width * rect.height, style, text, inDialog, inToolbar};
                        })
                        .filter(({text, rect, area, style, inDialog, inToolbar}) =>
                            labels.some((label) => text.includes(label)) &&
                            !inDialog &&
                            !inToolbar &&
                            rect.left < 430 &&
                            rect.right <= 460 &&
                            rect.top > 120 &&
                            area > 0 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none' &&
                            style.pointerEvents !== 'none'
                        )
                        .sort((a, b) => a.area - b.area);
                    if (!elements.length) return null;
                    const rect = elements[0].rect;
                    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
                }""",
            NEW_ARTICLE_LABELS,
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        return True
    except Exception:
        return False
