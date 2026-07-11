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

