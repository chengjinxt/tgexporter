from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

BLOCKED_PAGE_MARKERS = (
    "sina visitor system",
    "visitor system",
    "passport.weibo",
    "detected unusual activity",
    "not a robot",
    "i am not a robot",
    "captcha",
    "cloudflare",
    "ray id",
    "访问暂时受限",
    "我不是机器人",
    "请验证您是真人",
    "正在进行安全验证",
    "验证您不是自动程序",
    "block reference id",
)

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

IMAGE_CANDIDATE_SCRIPT = """
(limit) => {
    const ignored = /(logo|avatar|qrcode|qr-code|icon|sprite|blank|placeholder|weixin|wechat|ad-|ads|advert|banner|promo|sponsor|tousu|heimao|sinaads|slider|appendqr)/i;
    const h1 = document.querySelector('h1');
    const h1Box = h1 ? h1.getBoundingClientRect() : null;
    const h1Bottom = h1Box ? h1Box.bottom + window.scrollY : 0;
    const candidates = Array.from(document.images).map((img, index) => {
        const rect = img.getBoundingClientRect();
        const src = img.currentSrc || img.src || '';
        const parentAnchor = img.closest('a');
        const href = parentAnchor ? parentAnchor.getAttribute('href') || '' : '';
        const inAdContainer = !!img.closest('[class*="ad_"], [class*="ad-"], [class*="advert"], [class*="banner"], [class*="sponsor"], [id*="ad_"], [id*="ads"], [class*="slider"], ins, footer, a[href*="tousu"], a[href*="heimao"], [class*="appendQr"]');
        const text = `${src} ${href} ${img.alt || ''} ${img.className || ''} ${img.id || ''}`;
        const naturalWidth = img.naturalWidth || rect.width;
        const naturalHeight = img.naturalHeight || rect.height;
        const area = naturalWidth * naturalHeight;
        const aspect = naturalWidth / (naturalHeight || 1);
        const isBannerRatio = aspect > 2.6 || aspect < 0.38;
        const visible = rect.width >= 220 && rect.height >= 120 && area >= 100000;
        const top = rect.top + window.scrollY;
        const titleDistance = h1Bottom ? Math.abs(top - h1Bottom) : Math.min(top, 3000);
        const articleBoost = !h1Bottom || top >= h1Bottom - 160 ? 900000 : 0;
        const tooHighPenalty = h1Bottom && top < h1Bottom - 220 ? 900000 : 0;
        const dataPenalty = src.startsWith('data:') ? 500000 : 0;
        const gifPenalty = /(?:\\.gif|%2egif|gif[?&]|format=gif)/i.test(src) ? 900000 : 0;
        const score = area + articleBoost - titleDistance * 700 - tooHighPenalty - dataPenalty - gifPenalty;
        return { img, index, src, area, top, visible, ignored: ignored.test(text) || inAdContainer || isBannerRatio, score };
    }).filter(item => item.visible && !item.ignored && item.src && !item.src.startsWith('data:'));
    candidates.sort((a, b) => b.score - a.score);
    return candidates.slice(0, limit || 8).map(item => item.src);
}
"""


def capture_source_image(
    url: str,
    destination: Path,
    timeout_ms: int = 30000,
    profile_dir: Path | None = None,
) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    try:
        with sync_playwright() as playwright:
            context, close_context = create_browser_context(playwright, headless=True, profile_dir=profile_dir)
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
                load_lazy_media(page)
                if is_blocked_page(page):
                    return False
                if prefers_article_screenshot(url) and screenshot_article_region(page, destination):
                    return True
                if screenshot_largest_image(page, destination):
                    return True
                return screenshot_article_region(page, destination)
            finally:
                close_context()
    except Exception:
        destination.unlink(missing_ok=True)
        return False


def prefers_article_screenshot(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").removeprefix("www.").lower()
    return hostname in {
        "x.com",
        "twitter.com",
        "weibo.com",
        "m.weibo.cn",
        "finance.sina.com.cn",
        "sina.com.cn",
        "sina.cn",
        "cls.cn",
    }


def extract_source_image_urls(
    url: str,
    timeout_ms: int = 30000,
    profile_dir: Path | None = None,
    limit: int = 8,
) -> list[str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    try:
        with sync_playwright() as playwright:
            context, close_context = create_browser_context(playwright, headless=True, profile_dir=profile_dir)
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
                load_lazy_media(page)
                if is_blocked_page(page):
                    return []
                urls = page.evaluate(IMAGE_CANDIDATE_SCRIPT, limit)
                return [item for item in urls if isinstance(item, str)]
            finally:
                close_context()
    except Exception:
        return []


def open_source_login_browser(url: str, profile_dir: Path, timeout_seconds: int = 600) -> None:
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context, close_context = create_browser_context(playwright, headless=False, profile_dir=profile_dir)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            print(f"Source login browser opened: {url}", flush=True)
            print(f"Profile: {profile_dir}", flush=True)
            if timeout_seconds > 0:
                print(f"Keep this browser open for {timeout_seconds} seconds, or press Enter here to close it.", flush=True)
                wait_for_enter_or_timeout(page, timeout_seconds)
            else:
                input("Log in in the opened browser, then press Enter to close it...")
        finally:
            close_context()


def create_browser_context(playwright, headless: bool, profile_dir: Path | None = None):
    launch_args = ["--disable-blink-features=AutomationControlled"]
    context_options = {
        "viewport": {"width": 1365, "height": 900},
        "device_scale_factor": 1,
        "locale": "zh-CN",
        "user_agent": DESKTOP_USER_AGENT,
    }
    if profile_dir is not None:
        profile_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            args=launch_args,
            **context_options,
        )
        return context, context.close
    browser = playwright.chromium.launch(headless=headless, args=launch_args)
    context = browser.new_context(**context_options)
    return context, browser.close


def wait_for_enter_or_timeout(page, timeout_seconds: int) -> None:
    import msvcrt
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if msvcrt.kbhit():
            msvcrt.getwch()
            return


def load_lazy_media(page) -> None:
    try:
        height = int(page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)") or 0)
    except Exception:
        return
    positions = [0, 450, 900, 1400, 2200, 3200]
    for y in positions:
        if height and y > height + 300:
            break
        try:
            page.evaluate("(y) => window.scrollTo(0, y)", y)
            page.wait_for_timeout(650)
        except Exception:
            break


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
    close_selectors = (
        "button[aria-label*='close' i]",
        "button[aria-label*='关闭']",
        "[role='button'][aria-label*='close' i]",
        "[role='button'][aria-label*='关闭']",
        "button:has-text('×')",
        "button:has-text('✕')",
        "button:has-text('Close')",
    )
    for selector in close_selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible(timeout=800):
                locator.click(timeout=1500)
                page.wait_for_timeout(500)
        except Exception:
            continue


def is_blocked_page(page) -> bool:
    try:
        title = page.title().strip()
        body_text = page.locator("body").inner_text(timeout=2000).strip()
    except Exception:
        return False
    hostname = (urlparse(page.url).hostname or "").removeprefix("www.").lower()
    if hostname == "reuters.com" and title.lower() in {"reuters.com", "www.reuters.com"} and len(body_text) < 30:
        return True
    if hostname == "wsj.com" and title.lower() in {"wsj.com", "www.wsj.com"} and len(body_text) < 30:
        return True
    marker = f"{page.url}\n{title}\n{body_text[:800]}".lower()
    return any(item in marker for item in BLOCKED_PAGE_MARKERS)


def screenshot_largest_image(page, destination: Path) -> bool:
    candidate_urls = page.evaluate(IMAGE_CANDIDATE_SCRIPT, 1)
    if not candidate_urls:
        return False
    page.evaluate(
        """
        (src) => {
            document.querySelectorAll('[data-tgexporter-capture]').forEach(node => node.removeAttribute('data-tgexporter-capture'));
            const target = Array.from(document.images).find(img => (img.currentSrc || img.src || '') === src);
            if (target) target.setAttribute('data-tgexporter-capture', 'image');
        }
        """,
        candidate_urls[0],
    )
    locator = page.locator('[data-tgexporter-capture="image"]').first
    try:
        locator.scroll_into_view_if_needed(timeout=5000)
        locator.screenshot(path=str(destination), timeout=10000)
        return destination.exists() and destination.stat().st_size > 0
    except Exception:
        destination.unlink(missing_ok=True)
        return False


def screenshot_article_region(page, destination: Path) -> bool:
    try:
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
    except Exception:
        pass

    result = page.evaluate(
        r"""
        () => {
            // 截图前清理页面内所有常见广告、弹窗、投诉条及二维码，保证正文截图纯净无广告
            const removeSelectors = [
                '.SNP-layer', '#SFA_NV_POP_ZW', '[id*="SFA_NV_POP"]', '[class*="SNP-"]',
                '.top-banner', '.ad_content_bottom', '#article-botton-slide', '#artice_bottom_slider_dot',
                '.slider-item', '.appendQr_wrap', '.appendQr_normal', '.appendQr_normal_txt',
                'ins.sinaads', '[class*="sinaads"]', '[class*="ad_"]', '[class*="ad-"]',
                '[id*="ad_"]', '[id*="ads"]', '[class*="advert"]', '[class*="banner"]',
                '[class*="sponsor"]', 'a[href*="tousu"]', 'a[href*="heimao"]',
                '.article-content-right', '.right-content', '.blk_container', '#right_fixed',
                '.tool-box', '#bottom_tool', '.comment-box', '.bottom_tools', '.article-bottom',
                '.search', '#search', '.path-search-r', '.wb-share', 'footer'
            ];
            for (const sel of removeSelectors) {
                try {
                    document.querySelectorAll(sel).forEach(el => {
                        el.style.display = 'none';
                        el.remove();
                    });
                } catch (e) {}
            }
            // 移除所有 fixed 浮动弹窗与遮罩
            document.querySelectorAll('*').forEach(el => {
                try {
                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' && parseInt(style.zIndex || 0) > 50) {
                        el.remove();
                    }
                } catch (e) {}
            });

            const hostname = location.hostname.replace(/^www\./, '').toLowerCase();

            // 针对新浪财经等页面，进行精确定位（从顶部导航/标题一直到正文最后一个段落，宽度1024）
            if (hostname.includes('sina.com.cn') || hostname.includes('sina.cn')) {
                const path = document.querySelector('.path') || document.querySelector('.path-search') || document.querySelector('h1.main-title') || document.querySelector('h1');
                const art = document.querySelector('#artibody') || document.querySelector('.article') || document.querySelector('.article-content-left');
                if (path && art) {
                    const pList = Array.from(art.querySelectorAll('p'));
                    const lastP = pList.length ? pList[pList.length - 1] : art;
                    const topRect = path.getBoundingClientRect();
                    const bottomRect = lastP.getBoundingClientRect();
                    return {
                        mode: 'clip',
                        clip: {
                            x: Math.max(0, topRect.left - 15),
                            y: Math.max(0, topRect.top - 15),
                            width: 1024,
                            height: Math.round((bottomRect.bottom - topRect.top) + 35)
                        }
                    };
                }
            }

            const domainSelectors = {
                'x.com': ['article[data-testid="tweet"]', '[data-testid="tweet"]', 'article'],
                'twitter.com': ['article[data-testid="tweet"]', '[data-testid="tweet"]', 'article'],
                'weibo.com': [
                    '[class*="detail_wbtext"]',
                    '[class*="Feed_detail"]',
                    '[class*="card-wrap"]',
                    '[class*="woo-box-flex"][class*="woo-box-alignCenter"]',
                    'article',
                    'main'
                ],
                'm.weibo.cn': ['article', '[class*="card"]', '[class*="weibo"]', 'main'],
                'reuters.com': ['article', '[data-testid*="Article"]', 'main'],
                'wsj.com': ['article', '[data-testid*="article"]', '[class*="article"]', 'main', 'figure'],
                'theinformation.com': ['article', 'main', '[class*="article"]'],
                'cls.cn': ['.detail-content', '.article-content', '.article', '.detail', 'main'],
                'finance.sina.com.cn': ['#artibody', '.article-content', '.main-content', '.article', 'article'],
                'sina.com.cn': ['#artibody', '.article-content', '.main-content', '.article', 'article'],
                'sina.cn': ['#artibody', '.article-content', '.main-content', '.article', 'article'],
            };
            const specificSelectors = domainSelectors[hostname] || [];
            const defaultSelectors = [
                'article',
                'main',
                '[class*="article"]',
                '[class*="content"]',
                '[class*="post"]',
                '[id*="article"]',
                '[id*="content"]',
                'body'
            ];
            const selectors = [...specificSelectors, ...defaultSelectors];
            const seen = new Set();
            const candidates = [];
            for (const selector of selectors) {
                for (const node of Array.from(document.querySelectorAll(selector))) {
                    if (seen.has(node)) continue;
                    seen.add(node);
                    const rect = node.getBoundingClientRect();
                    const text = (node.innerText || node.textContent || '').trim();
                    if (rect.width < 280 || rect.height < 60 || text.length < 40) continue;
                    const isSpecific = specificSelectors.includes(selector);
                    candidates.push({ node, textLength: text.length, area: rect.width * rect.height, top: rect.top + window.scrollY, isSpecific });
                }
            }
            const preferred = candidates.some(item => item.isSpecific) ? candidates.filter(item => item.isSpecific) : candidates;
            preferred.sort((a, b) => (b.textLength + b.area / 2000 - Math.min(b.top, 3000)) - (a.textLength + a.area / 2000 - Math.min(a.top, 3000)));
            document.querySelectorAll('[data-tgexporter-capture]').forEach(node => node.removeAttribute('data-tgexporter-capture'));
            if (!preferred.length) return null;
            preferred[0].node.setAttribute('data-tgexporter-capture', 'article');
            return { mode: 'locator' };
        }
        """
    )
    if not result:
        return False

    try:
        if isinstance(result, dict) and result.get("mode") == "clip":
            clip = result["clip"]
            page.screenshot(path=str(destination), clip=clip, timeout=10000)
            return destination.exists() and destination.stat().st_size > 0

        locator = page.locator('[data-tgexporter-capture="article"]').first
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
                "height": min(max(box["height"], 200), 900),
            },
            timeout=10000,
        )
        return destination.exists() and destination.stat().st_size > 0
    except Exception:
        destination.unlink(missing_ok=True)
        return False
