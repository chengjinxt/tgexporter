from tgexporter.source_sites import source_rule_for_url


def test_source_rule_matches_known_root_domains():
    rule = source_rule_for_url(
        "https://www.bloomberg.com/news/articles/2026-07-14/deepseek-s-liang-tops-amodei-and-brockman-as-richest-ai-founder"
    )

    assert rule is not None
    assert rule.domain == "bloomberg.com"
    assert "浏览器" in rule.resource_method


def test_source_rule_matches_exact_subdomain():
    rule = source_rule_for_url("https://m.weibo.cn/status/R8Gem4OWq")

    assert rule is not None
    assert rule.domain == "m.weibo.cn"


def test_source_rule_matches_multi_part_domain():
    rule = source_rule_for_url("https://finance.sina.com.cn/tech/article.html")

    assert rule is not None
    assert rule.domain == "finance.sina.com.cn"


def test_source_rule_matches_wechat_article_domain():
    rule = source_rule_for_url("https://mp.weixin.qq.com/s/mdg66FvdwwRFsg20HHnr4g")

    assert rule is not None
    assert rule.domain == "mp.weixin.qq.com"
    assert "mmbiz.qpic.cn" in rule.resource_method
    assert "公众号超链接" in rule.link_display


def test_source_rule_records_x_login_requirement():
    rule = source_rule_for_url("https://x.com/SpaceXAI/status/2076692402442846289")

    assert rule is not None
    assert rule.domain == "x.com"
    assert "登录" in rule.login_requirement


def test_source_rule_records_theinformation_subscription_requirement():
    rule = source_rule_for_url("https://www.theinformation.com/articles/deepseeks-annualized-revenue-nears-500-million")

    assert rule is not None
    assert rule.domain == "theinformation.com"
    assert "订阅" in rule.login_requirement


def test_source_rule_records_cls_body_screenshot_fallback():
    rule = source_rule_for_url("https://www.cls.cn/detail/2427193")

    assert rule is not None
    assert rule.domain == "cls.cn"
    assert "正文" in rule.resource_method
    assert rule.login_requirement == "通常无需登录"


def test_source_rule_returns_none_for_unknown_domain():
    assert source_rule_for_url("https://example.invalid/article") is None
