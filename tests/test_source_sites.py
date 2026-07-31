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
    assert "tii.imgix.net" in rule.resource_method


def test_source_rule_records_reuters_browser_image_strategy():
    rule = source_rule_for_url(
        "https://www.reuters.com/world/china/china-begins-making-homegrown-duv-chipmaking-tools-information-reports-2026-07-27/"
    )

    assert rule is not None
    assert rule.domain == "reuters.com"
    assert "正文新闻图 URL" in rule.resource_method
    assert "401" in rule.notes


def test_source_rule_records_ithome_placeholder_filter_strategy():
    rule = source_rule_for_url("https://www.ithome.com/0/983/943.htm")

    assert rule is not None
    assert rule.domain == "ithome.com"
    assert "newsuploadfiles" in rule.resource_method
    assert "t.png" in rule.notes


def test_source_rule_records_wsj_login_or_verification_strategy():
    rule = source_rule_for_url(
        "https://www.wsj.com/tech/ai/anthropic-ai-models-hacked-three-companies-during-tests-bd752c86"
    )

    assert rule is not None
    assert rule.domain == "wsj.com"
    assert "images.wsj.net" in rule.resource_method
    assert "人机验证" in rule.login_requirement


def test_source_rule_records_cls_body_screenshot_fallback():
    rule = source_rule_for_url("https://www.cls.cn/detail/2427193")

    assert rule is not None
    assert rule.domain == "cls.cn"
    assert "正文" in rule.resource_method
    assert rule.login_requirement == "通常无需登录"


def test_source_rule_records_fifa_lazy_image_strategy():
    rule = source_rule_for_url(
        "https://www.fifa.com/en/tournaments/mens/worldcup/canadamexicousa2026/articles/spain-argentina-final-report-highlights"
    )

    assert rule is not None
    assert rule.domain == "fifa.com"
    assert "digitalhub.fifa.com" in rule.resource_method
    assert "懒加载" in rule.notes


def test_source_rule_records_axios_browser_image_strategy():
    rule = source_rule_for_url("https://www.axios.com/2026/07/20/ai-us-china-open-source-kimi")

    assert rule is not None
    assert rule.domain == "axios.com"
    assert "images.axios.com" in rule.resource_method
    assert "403" in rule.notes


def test_source_rule_records_moe_education_source():
    rule = source_rule_for_url("http://www.moe.gov.cn/jyb_xwfb/gzdt_gzdt/")

    assert rule is not None
    assert rule.domain == "moe.gov.cn"
    assert "教育部" in rule.name
    assert "春晖学府" in rule.fallback_method


def test_source_rule_returns_none_for_unknown_domain():
    assert source_rule_for_url("https://example.invalid/article") is None
