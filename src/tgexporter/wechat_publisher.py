from __future__ import annotations

import html
import re
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from .filename import sanitize_title


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

    def try_auto_fill(self, article_path: Path, headless: bool = False) -> Path:
        preview_path = self.prepare_preview(article_path)
        article = parse_markdown_article(article_path)
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://mp.weixin.qq.com/", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            editor_url = "https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit&type=10&lang=zh_CN"
            page.goto(editor_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            try:
                fill_title(page, article.title)
                fill_body(page, markdown_to_wechat_html(article))
            except PlaywrightTimeoutError as exc:
                screenshot = self.screenshot_dir / "wechat-auto-fill-failed.png"
                page.screenshot(path=str(screenshot), full_page=True)
                context.close()
                raise RuntimeError(f"WeChat auto fill failed. Screenshot: {screenshot}") from exc

            screenshot = self.screenshot_dir / "wechat-auto-filled.png"
            page.screenshot(path=str(screenshot), full_page=True)
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
            page.goto("https://mp.weixin.qq.com/", wait_until="domcontentloaded")
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


def markdown_to_wechat_html(article: WechatArticle) -> str:
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
            src = resolve_markdown_asset(base_dir, image.group("src"))
            alt = html.escape(image.group("alt"))
            blocks.append(f'<p><img src="{html.escape(src)}" alt="{alt}"></p>')
            continue
        video = re.match(r"(?:视频：)?\[(?P<label>.*?)\]\((?P<src>.*?\.(?:mp4|mov|webm))\)", line, re.I)
        if video:
            flush_paragraph()
            src = resolve_markdown_asset(base_dir, video.group("src"))
            label = html.escape(video.group("label"))
            blocks.append(f'<p>{label}</p><video controls src="{html.escape(src)}"></video>')
            continue
        if line.startswith("# "):
            flush_paragraph()
            blocks.append(f"<h1>{html.escape(line[2:].strip())}</h1>")
            continue
        if line.startswith("## "):
            flush_paragraph()
            blocks.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
            continue
        if line.startswith("- "):
            flush_paragraph()
            blocks.append(f"<p>{html.escape(line[2:].strip())}</p>")
            continue
        paragraph.append(html.escape(line))

    flush_paragraph()
    return "\n".join(blocks)


def resolve_markdown_asset(base_dir: Path, value: str) -> str:
    if value.startswith(("http://", "https://", "file://")):
        return value
    return (base_dir / value).resolve().as_uri()


def fill_title(page, title: str) -> None:
    selectors = [
        "input[placeholder*='标题']",
        "textarea[placeholder*='标题']",
        "[contenteditable='true'][placeholder*='标题']",
        ".title input",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count():
            locator.fill(title, timeout=3000)
            return
    page.locator("input, textarea, [contenteditable='true']").first.fill(title, timeout=3000)


def fill_body(page, body_html: str) -> None:
    selectors = [
        "#ueditor_0",
        "iframe[id*='ueditor']",
        "[contenteditable='true']",
        ".ProseMirror",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        if not locator.count():
            continue
        tag_name = locator.evaluate("node => node.tagName.toLowerCase()", timeout=3000)
        if tag_name == "iframe":
            frame = locator.element_handle(timeout=3000).content_frame()
            if frame:
                frame.locator("body").evaluate("(node, html) => node.innerHTML = html", body_html)
                return
        else:
            locator.evaluate("(node, html) => node.innerHTML = html", body_html, timeout=3000)
            return
    raise RuntimeError("No editable WeChat body area found.")

