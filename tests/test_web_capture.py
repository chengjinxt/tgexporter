from tgexporter.web_capture import prefers_article_screenshot


def test_prefers_article_screenshot_for_social_posts():
    assert prefers_article_screenshot("https://x.com/thsottiaux/status/2082317452755751098")
    assert prefers_article_screenshot("https://weibo.com/2406952997/RaLRYxcZE")
    assert prefers_article_screenshot("https://m.weibo.cn/detail/5321259015733846")
    assert not prefers_article_screenshot("https://www.reuters.com/world/example")
