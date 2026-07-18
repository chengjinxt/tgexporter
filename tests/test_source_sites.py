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


def test_source_rule_returns_none_for_unknown_domain():
    assert source_rule_for_url("https://example.invalid/article") is None
