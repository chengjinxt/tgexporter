from __future__ import annotations

import re
from pathlib import Path

BLOCKED_PAGE_MARKERS = (
    "sina visitor system",
    "visitor system",
    "passport.weibo",
    "detected unusual activity",
    "not a robot",
    "block reference id",
)

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def capture_source_image(url: str, destination: Path, timeout_ms: int = 30000) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(
                viewport={"width": 1365, "height": 900},
                device_scale_factor=1,
                locale="zh-CN",
                user_agent=DESKTOP_USER_AGENT,
            )
            page = context.new_page()
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined })")
            page.set_default_timeout(timeout_ms)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass
                dismiss_common_overlays(page)
                if is_blocked_page(page):
                    return False
                if screenshot_largest_image(page, destination):
                    return True
                return screenshot_article_region(page, destination)
            finally:
                browser.close()
    except Exception:
        destination.unlink(missing_ok=True)
        return False


def dismiss_common_overlays(page) -> None:
    button_texts = (
        "Accept",
        "Accept All",
        "I Agree",
        "同意",
        "接受",
        "知道了",
    )
    for text in button_texts:
        try:
            button = page.get_by_role("button", name=re.compile(rf"^{re.escape(text)}$", re.I)).first
            if button.count() > 0 and button.is_visible(timeout=800):
                button.click(timeout=1500)
                page.wait_for_timeout(500)
        except Exception:
            continue


def is_blocked_page(page) -> bool:
    try:
        marker = f"{page.url}\n{page.title()}\n{page.locator('body').inner_text(timeout=2000)[:800]}".lower()
    except Exception:
        return False
    return any(item in marker for item in BLOCKED_PAGE_MARKERS)


def screenshot_largest_image(page, destination: Path) -> bool:
    candidate = page.evaluate(
        """
        () => {
            const ignored = /(logo|avatar|qrcode|qr-code|icon|sprite|blank|placeholder|weixin|wechat|ad-|ads|advert|banner|promo|sponsor)/i;
            const h1 = document.querySelector('h1');
            const h1Box = h1 ? h1.getBoundingClientRect() : null;
            const h1Bottom = h1Box ? h1Box.bottom + window.scrollY : 0;
            const candidates = Array.from(document.images).map((img, index) => {
                const rect = img.getBoundingClientRect();
                const src = img.currentSrc || img.src || '';
                const text = `${src} ${img.alt || ''} ${img.className || ''} ${img.id || ''}`;
                const naturalWidth = img.naturalWidth || rect.width;
                const naturalHeight = img.naturalHeight || rect.height;
                const area = naturalWidth * naturalHeight;
                const visible = rect.width >= 260 && rect.height >= 150 && area >= 120000;
                const top = rect.top + window.scrollY;
                const titleDistance = h1Bottom ? Math.abs(top - h1Bottom) : Math.min(top, 3000);
                const articleBoost = !h1Bottom || top >= h1Bottom - 120 ? 800000 : 0;
                const tooHighPenalty = h1Bottom && top < h1Bottom - 180 ? 900000 : 0;
                const score = area + articleBoost - titleDistance * 60 - tooHighPenalty;
                return { img, index, src, area, top, visible, ignored: ignored.test(text), score };
            }).filter(item => item.visible && !item.ignored && item.src);
            candidates.sort((a, b) => b.score - a.score);
            document.querySelectorAll('[data-tgexporter-capture]').forEach(node => node.removeAttribute('data-tgexporter-capture'));
            if (!candidates.length) return null;
            candidates[0].img.setAttribute('data-tgexporter-capture', 'image');
            return { src: candidates[0].src, area: candidates[0].area };
        }
        """
    )
    if not candidate:
        return False
    locator = page.locator('[data-tgexporter-capture="image"]').first
    try:
        locator.scroll_into_view_if_needed(timeout=5000)
        locator.screenshot(path=str(destination), timeout=10000)
        return destination.exists() and destination.stat().st_size > 0
    except Exception:
        destination.unlink(missing_ok=True)
        return False


def screenshot_article_region(page, destination: Path) -> bool:
    selected = page.evaluate(
        """
        () => {
            const selectors = [
                'article',
                'main',
                '[class*="article"]',
                '[class*="content"]',
                '[class*="post"]',
                '[id*="article"]',
                '[id*="content"]',
                'body'
            ];
            const seen = new Set();
            const candidates = [];
            for (const selector of selectors) {
                for (const node of Array.from(document.querySelectorAll(selector))) {
                    if (seen.has(node)) continue;
                    seen.add(node);
                    const rect = node.getBoundingClientRect();
                    const text = (node.innerText || node.textContent || '').trim();
                    if (rect.width < 320 || rect.height < 180 || text.length < 80) continue;
                    candidates.push({ node, textLength: text.length, area: rect.width * rect.height, top: rect.top + window.scrollY });
                }
            }
            candidates.sort((a, b) => (b.textLength + b.area / 2000 - Math.min(b.top, 3000)) - (a.textLength + a.area / 2000 - Math.min(a.top, 3000)));
            document.querySelectorAll('[data-tgexporter-capture]').forEach(node => node.removeAttribute('data-tgexporter-capture'));
            if (!candidates.length) return false;
            candidates[0].node.setAttribute('data-tgexporter-capture', 'article');
            return true;
        }
        """
    )
    if not selected:
        return False
    locator = page.locator('[data-tgexporter-capture="article"]').first
    try:
        locator.scroll_into_view_if_needed(timeout=5000)
        box = locator.bounding_box(timeout=5000)
        if not box:
            return False
        page.screenshot(
            path=str(destination),
            clip={
                "x": max(box["x"], 0),
                "y": max(box["y"], 0),
                "width": min(box["width"], 1100),
                "height": min(box["height"], 900),
            },
            timeout=10000,
        )
        return destination.exists() and destination.stat().st_size > 0
    except Exception:
        destination.unlink(missing_ok=True)
        return False
