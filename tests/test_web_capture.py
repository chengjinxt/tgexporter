from tgexporter.web_capture import is_blocked_page, prefers_article_screenshot


class FakeBodyLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self, timeout=2000) -> str:
        return self.text


class FakePage:
    def __init__(self, url: str, title: str, body: str) -> None:
        self.url = url
        self._title = title
        self._body = body

    def title(self) -> str:
        return self._title

    def locator(self, selector: str) -> FakeBodyLocator:
        assert selector == "body"
        return FakeBodyLocator(self._body)


def test_prefers_article_screenshot_for_social_posts():
    assert prefers_article_screenshot("https://x.com/thsottiaux/status/2082317452755751098")
    assert prefers_article_screenshot("https://weibo.com/2406952997/RaLRYxcZE")
    assert prefers_article_screenshot("https://m.weibo.cn/detail/5321259015733846")
    assert not prefers_article_screenshot("https://www.reuters.com/world/example")


def test_wsj_empty_verification_page_is_blocked():
    page = FakePage(
        "https://www.wsj.com/tech/ai/anthropic-ai-models-hacked-three-companies-during-tests-bd752c86",
        "wsj.com",
        "",
    )

    assert is_blocked_page(page)


def test_image_candidate_script_contains_ad_and_banner_filters():
    # 验证候选图JS脚本已包含广告容器、黑猫投诉与极端Banner横幅拦截逻辑
    from tgexporter.web_capture import IMAGE_CANDIDATE_SCRIPT

    assert "tousu" in IMAGE_CANDIDATE_SCRIPT
    assert "heimao" in IMAGE_CANDIDATE_SCRIPT
    assert "isBannerRatio" in IMAGE_CANDIDATE_SCRIPT
    assert "inAdContainer" in IMAGE_CANDIDATE_SCRIPT
