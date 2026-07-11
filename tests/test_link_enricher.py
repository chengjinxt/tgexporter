from tgexporter.link_enricher import enrich_links, extract_urls


def test_extract_urls_strips_chinese_sentence_punctuation():
    assert extract_urls("来源：https://example.com/a?b=1。另见 https://openai.com/test,") == [
        "https://example.com/a?b=1",
        "https://openai.com/test",
    ]


def test_enrich_links_without_fetch_uses_domain_as_name():
    links = enrich_links("看这里 https://example.com/a", fetch_metadata=False)
    assert links[0].name == "example.com"
    assert links[0].url == "https://example.com/a"


def test_enrich_links_ignores_wechat_mp_domain():
    links = enrich_links(
        "公众号 https://mp.weixin.qq.com/s/a 原文 https://example.com/a",
        fetch_metadata=False,
    )
    assert [link.url for link in links] == ["https://example.com/a"]


def test_enrich_links_ignores_telegram_promo_domains():
    links = enrich_links(
        "来源 https://example.com/a 频道 http://t.me/ZaiHuaPd 群 https://telegram.me/group",
        fetch_metadata=False,
    )
    assert [link.url for link in links] == ["https://example.com/a"]
