from __future__ import annotations

import base64
import html
import mimetypes
import re
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .filename import sanitize_title


WECHAT_HOME_URL = "https://mp.weixin.qq.com/"
WECHAT_EDITOR_URL = "https://mp.weixin.qq.com/cgi-bin/appmsg"


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
        preview_path = self.prepare_preview(article_path)
        article = parse_markdown_article(article_path)
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
                editor_url = build_wechat_editor_url(page)
                print(f"Opening WeChat editor: {editor_url}", flush=True)
                page.goto(editor_url, wait_until="domcontentloaded")
                wait_for_editor_ready(page)
                print(f"WeChat editor ready: {page.url}", flush=True)
                save_stage_screenshot(page, self.screenshot_dir, "wechat-03-editor-ready.png")
                fill_title(page, article.title)
                print("WeChat title filled.", flush=True)
                fill_article_body_with_local_uploads(page, article)
                print("WeChat body filled.", flush=True)
                cover_image = first_local_image_path(article)
                if cover_image:
                    if upload_cover(page, cover_image):
                        print(f"WeChat cover uploaded: {cover_image}", flush=True)
                    else:
                        print(f"WeChat cover upload skipped or failed: {cover_image}", flush=True)

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
            blocks.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
            continue
        if line.startswith("- "):
            flush_paragraph()
            blocks.append(f"<p>{html.escape(line[2:].strip())}</p>")
            continue
        if skip_duplicate_intro and is_duplicate_title(line, article.title):
            continue
        paragraph.append(html.escape(line))

    flush_paragraph()
    return "\n".join(blocks)


def normalize_title_text(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()


def is_duplicate_title(value: str, title: str) -> bool:
    left = normalize_title_text(value)
    right = normalize_title_text(title)
    return bool(left and right and (left == right or left in right or right in left))


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


def build_wechat_editor_url(page) -> str:
    token = extract_wechat_token(page)
    query = {
        "t": "media/appmsg_edit",
        "action": "edit",
        "type": "10",
        "isNew": "1",
        "isMul": "0",
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


def fill_body(page, body_html: str) -> None:
    body_placeholder = first_visible_locator(page, "text=从这里开始写正文")
    if body_placeholder is not None:
        try:
            set_nearest_editable_html(body_placeholder, body_html)
        except Exception:
            paste_html_at_locator(page, body_placeholder, body_html)
        return
    selectors = [
        "#ueditor_0",
        "iframe#ueditor_0",
        "iframe[id*='ueditor']",
        "iframe",
        "[contenteditable='true']:not(:has-text('请在这里输入标题'))",
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
            page.wait_for_timeout(500)
            continue
        image_path = Path(value)
        if insert_local_body_image(page, image_path):
            print(f"WeChat body image uploaded: {image_path}", flush=True)
            page.wait_for_timeout(1500)
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
            items.append(("html", f"<h2>{html.escape(line[3:].strip())}</h2>"))
            continue
        if line.startswith("- "):
            flush_paragraph()
            items.append(("html", f"<p>{html.escape(line[2:].strip())}</p>"))
            continue
        if is_duplicate_title(line, article.title):
            continue
        paragraph.append(html.escape(line))

    flush_paragraph()
    return items


def focus_body_editor(page, at_start: bool = False, at_end: bool = False) -> None:
    body = first_visible_locator(page, "[contenteditable='true']:not([data-placeholder*='标题'])")
    if body is None:
        body = first_visible_locator(page, ".ProseMirror") or first_visible_locator(page, "text=从这里开始写正文")
    if body is None:
        raise RuntimeError("No visible WeChat body editor found.")
    body.click(timeout=3000)
    if at_start or at_end:
        set_body_cursor(body, to_start=at_start)


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
        page.wait_for_timeout(5000)
        return True
    except Exception:
        pass
    try:
        image_button.click(timeout=3000)
        page.wait_for_timeout(500)
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
            page.wait_for_timeout(5000)
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
            page.wait_for_timeout(5000)
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
    page.keyboard.press("Control+A")
    page.keyboard.insert_text(text)


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
        cover_locator.click(timeout=3000)
        page.wait_for_timeout(1000)
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
    page.wait_for_timeout(2000)
    if not click_cover_thumbnail(page):
        return False
    page.wait_for_timeout(800)
    print("WeChat cover thumbnail selected.", flush=True)
    if not click_visible_button(page, "下一步"):
        return False
    print("WeChat cover next clicked.", flush=True)
    page.wait_for_timeout(1500)
    confirm_cover_dialog(page)
    page.wait_for_timeout(2000)
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
            page.wait_for_timeout(3000)
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
                page.wait_for_timeout(1200)
                clicked = True
                break
            if click_visible_text(page, text):
                page.wait_for_timeout(1200)
                clicked = True
                break
        if not clicked:
            return


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
