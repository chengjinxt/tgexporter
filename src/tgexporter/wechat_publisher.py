from __future__ import annotations

import base64
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
from .text_filters import is_channel_promo_line


WECHAT_HOME_URL = "https://mp.weixin.qq.com/"
WECHAT_EDITOR_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg"
MAX_WECHAT_ARTICLES = 8
DEFAULT_HUMAN_PAUSE_MS = (900, 1800)
MARKDOWN_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)\s]+)\)")


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
                    fill_current_wechat_article(page, article, index=index)
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
    render_local_videos: bool = True,
    include_title: bool = True,
    skip_duplicate_intro: bool = False,
    include_images: bool = True,
) -> str:
    base_dir = article.source_path.parent
    blocks: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{'<br>'.join(paragraph)}</p>")
            paragraph.clear()

    for raw_line in article.body_markdown.splitlines():
        line = raw_line.rstrip()
        if not line:
            flush_paragraph()
            continue
        if is_channel_promo_line(line):
            flush_paragraph()
            continue
        image = re.match(r"!\[(?P<alt>.*?)\]\((?P<src>.*?)\)", line)
        if image:
            flush_paragraph()
            if not include_images:
                continue
            src = resolve_markdown_image_asset(base_dir, image.group("src"), embed_local_images=embed_local_images)
            alt = html.escape(image.group("alt"))
            blocks.append(f'<p><img src="{html.escape(src)}" alt="{alt}"></p>')
            continue
        video = re.match(r"(?:视频：)?\[(?P<label>.*?)\]\((?P<src>.*?\.(?:mp4|mov|webm))\)", line, re.I)
        if video:
            flush_paragraph()
            src_value = video.group("src")
            if render_local_videos or src_value.startswith(("http://", "https://")):
                src = resolve_markdown_asset(base_dir, src_value)
                label = html.escape(video.group("label"))
                blocks.append(f'<p>{label}</p><video controls src="{html.escape(src)}"></video>')
            continue
        if line.startswith("# "):
            flush_paragraph()
            title_text = line[2:].strip()
            if include_title:
                blocks.append(f"<h1>{html.escape(title_text)}</h1>")
            continue
        if line.startswith("## "):
            flush_paragraph()
            blocks.append(f"<h2>{markdown_inline_to_html(line[3:].strip())}</h2>")
            continue
        if line.startswith("- "):
            flush_paragraph()
            blocks.append(f"<p>{markdown_inline_to_html(line[2:].strip())}</p>")
            continue
        if skip_duplicate_intro and is_duplicate_title(line, article.title):
            continue
        paragraph.append(markdown_inline_to_html(line))

    flush_paragraph()
    return "\n".join(blocks)


def normalize_title_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


def is_duplicate_title(value: str, title: str) -> bool:
    left = normalize_title_text(value)
    right = normalize_title_text(title)
    return bool(left and right and (left == right or left in right or right in left))


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
        parts.append(html.escape(value[last : match.start()]))
        label = html.escape(match.group("label").strip())
        url = html.escape(match.group("url").strip(), quote=True)
        parts.append(f'<a href="{url}">{label}</a>')
        last = match.end()
    parts.append(html.escape(value[last:]))
    return "".join(parts)


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
        image = re.match(r"!\[.*?\]\((?P<src>.*?)\)", raw_line.strip())
        if not image:
            continue
        src = image.group("src")
        if src.startswith(("http://", "https://", "data:", "file://")):
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


def fill_current_wechat_article(page, article: WechatArticle, index: int) -> None:
    title = clean_wechat_title(article.title)
    fill_title(page, title)
    human_pause(page)
    print(f"WeChat article {index} title filled.", flush=True)
    fill_article_body_with_local_uploads(page, article)
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
    page.mouse.wheel(0, -2000)
    human_pause(page, 800, 1500)
    for selector in ["text=新建内容", "text=添加图文", "text=添加", "text=新增"]:
        button = first_visible_locator(page, selector)
        if button is None:
            continue
        try:
            print(f"Adding WeChat article slot {index} via selector: {selector}", flush=True)
            button.click(timeout=5000)
        except Exception:
            continue
        human_pause(page, 1000, 2000)
        if click_write_new_article_option(page):
            human_pause(page, 1800, 3200)
        else:
            print("WeChat new article menu option not found; checking whether editor switched directly.", flush=True)
        wait_for_editor_ready(page)
        wait_for_blank_title(page, index)
        return
    if click_new_article_button_by_text(page):
        human_pause(page, 1000, 2000)
        if click_write_new_article_option(page):
            human_pause(page, 1800, 3200)
        wait_for_editor_ready(page)
        wait_for_blank_title(page, index)
        return
    raise RuntimeError(f"Could not add WeChat sub-article slot {index}.")


def wait_for_blank_title(page, index: int, timeout_seconds: int = 10) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        title = read_current_title(page)
        if not title or "请在这里输入标题" in title:
            return
        page.wait_for_timeout(500)
    raise RuntimeError(f"WeChat sub-article slot {index} did not become active before filling.")


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


def fill_article_body_with_local_uploads(page, article: WechatArticle) -> None:
    items = build_wechat_body_items(article)
    fill_body(page, "")
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
        if insert_local_body_image(page, image_path):
            print(f"WeChat body image uploaded: {image_path}", flush=True)
            human_pause(page, 1800, 3600)
        else:
            print(f"WeChat body image upload skipped or failed: {image_path}", flush=True)


def build_wechat_body_items(article: WechatArticle) -> list[tuple[str, str | Path]]:
    base_dir = article.source_path.parent
    items: list[tuple[str, str | Path]] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            items.append(("html", f"<p>{'<br>'.join(paragraph)}</p>"))
            paragraph.clear()

    for raw_line in article.body_markdown.splitlines():
        line = raw_line.rstrip()
        if not line:
            flush_paragraph()
            continue
        if is_channel_promo_line(line):
            flush_paragraph()
            continue

        image = re.match(r"!\[(?P<alt>.*?)\]\((?P<src>.*?)\)", line)
        if image:
            flush_paragraph()
            src = image.group("src")
            if src.startswith(("http://", "https://", "data:", "file://")):
                continue
            path = (base_dir / src).resolve()
            if path.exists():
                items.append(("image", path))
            continue

        video = re.match(r"(?:视频：)?\[(?P<label>.*?)\]\((?P<src>.*?\.(?:mp4|mov|webm))\)", line, re.I)
        if video:
            flush_paragraph()
            continue

        if line.startswith("# "):
            flush_paragraph()
            continue
        if line.startswith("## "):
            flush_paragraph()
            items.append(("html", f"<h2>{markdown_inline_to_html(line[3:].strip())}</h2>"))
            continue
        if line.startswith("- "):
            flush_paragraph()
            items.append(("html", f"<p>{markdown_inline_to_html(line[2:].strip())}</p>"))
            continue
        if is_duplicate_title(line, article.title):
            continue
        paragraph.append(markdown_inline_to_html(line))

    flush_paragraph()
    return items


def focus_body_editor(page, at_start: bool = False, at_end: bool = False) -> None:
    body = find_body_editor(page)
    if body is None:
        raise RuntimeError("No visible WeChat body editor found.")
    body.click(timeout=3000)
    assert_active_editor_is_not_title(page)
    if at_start or at_end:
        set_body_cursor(body, to_start=at_start)


def find_body_editor(page):
    try:
        found = page.evaluate(
            """() => {
                    document.querySelectorAll('[data-codex-body-editor]').forEach((node) => {
                        node.removeAttribute('data-codex-body-editor');
                    });
                    const visible = (node) => {
                        const rect = node.getBoundingClientRect();
                        const style = window.getComputedStyle(node);
                        return rect.width > 20 &&
                            rect.height > 10 &&
                            rect.bottom > 0 &&
                            rect.right > 0 &&
                            rect.top < window.innerHeight &&
                            rect.left < window.innerWidth &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none';
                    };
                    const attrText = (node) => [
                        node.id,
                        node.className,
                        node.getAttribute('placeholder'),
                        node.getAttribute('data-placeholder'),
                        node.getAttribute('aria-label'),
                        node.getAttribute('name'),
                        node.getAttribute('role'),
                        node.innerText,
                        node.textContent,
                    ].filter(Boolean).join(' ');
                    const titleLike = (text) => /标题|请输入作者/.test(text) && !/正文|从这里开始|写正文/.test(text);
                    const nodes = [...document.querySelectorAll('[contenteditable="true"], [contenteditable=true], [contenteditable], .ProseMirror')]
                        .filter(visible)
                        .map((node) => {
                            const rect = node.getBoundingClientRect();
                            const text = attrText(node);
                            const parentText = node.parentElement ? attrText(node.parentElement) : '';
                            const grandText = node.parentElement && node.parentElement.parentElement
                                ? attrText(node.parentElement.parentElement)
                                : '';
                            const context = `${text} ${parentText} ${grandText}`;
                            const hasBodyMarker = /正文|从这里开始|写正文|ueditor|ProseMirror/i.test(context);
                            const isTitle = titleLike(context) ||
                                node.closest('#title, .title, .js_title, [data-placeholder*="标题"], [placeholder*="标题"]');
                            return {node, rect, area: rect.width * rect.height, hasBodyMarker, isTitle};
                        })
                        .filter((item) => !item.isTitle)
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
                    editable.innerText,
                    editable.textContent,
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


def insert_local_body_image(page, image_path: Path) -> bool:
    if choose_local_image_from_toolbar(page, image_path):
        return True
    return set_visible_file_input(page, image_path)


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
        return False
    except Exception:
        return False


def choose_cover_from_body(page) -> bool:
    option = first_visible_locator(page, "text=从正文选择")
    if option is None:
        return False
    option.click(timeout=3000)
    human_pause(page, 1600, 2800)
    if not click_cover_thumbnail(page):
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
    return click_visible_text(page, "写新文章")


def click_new_article_button_by_text(page) -> bool:
    try:
        point = page.evaluate(
            """() => {
                    const labels = ['新建内容', '添加图文', '添加', '新增'];
                    const elements = [...document.querySelectorAll('button, a, span, div, li')]
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            const text = (el.innerText || el.textContent || '').trim();
                            return {el, rect, area: rect.width * rect.height, style, text};
                        })
                        .filter(({text, rect, area, style}) =>
                            labels.some((label) => text.includes(label)) &&
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
                }"""
        )
        if not point:
            return False
        page.mouse.click(point["x"], point["y"])
        return True
    except Exception:
        return False
