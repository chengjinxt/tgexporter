from tgexporter.link_enricher import enrich_links, extract_urls, find_page_image_urls, is_wechat_article_url


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


def test_wechat_article_url_is_separate_from_source_links():
    assert is_wechat_article_url("https://mp.weixin.qq.com/s/Wp0PdV83btg8skL6ypfXHw")
    assert not is_wechat_article_url("https://example.com/s/a")


def test_find_page_image_urls_skips_logo_qrcode_and_keeps_article_images():
    html = """
    <meta property="og:image" content="https://www.qbitai.com/wp-content/uploads/imgs/qbitai-logo-1.png">
    <img src="/wp-content/uploads/2019/01/qrcode_QbitAI_1.jpg">
    <img src="http://www.qbitai.com/wp-content/themes/liangziwei/imagesnew/head.jpg">
    <img src="https://i.qbitai.com/wp-content/uploads/2026/07/a429490b5ed4bf0189b5e4e2701f7330.png">
    """

    assert find_page_image_urls(html, "https://www.qbitai.com/2026/07/447873.html") == (
        "https://i.qbitai.com/wp-content/uploads/2026/07/a429490b5ed4bf0189b5e4e2701f7330.png",
    )


def test_find_page_image_urls_skips_ithome_placeholder_image():
    html = """
    <meta property="og:image" content="https://img.ithome.com/images/v2/t.png">
    <img src="https://img.ithome.com/newsuploadfiles/2026/7/59545f80-d793-4c7f-ad16-fc34a3f424c2.jpg">
    """

    assert find_page_image_urls(html, "https://www.ithome.com/0/983/943.htm") == (
        "https://img.ithome.com/newsuploadfiles/2026/7/59545f80-d793-4c7f-ad16-fc34a3f424c2.jpg",
    )


def test_find_page_image_urls_skips_unescaped_space_in_image_url():
    html = '<meta property="og:image" content="https://example.com/image with spaces.jpg">'

    assert find_page_image_urls(html, "https://example.com/article") == ()


def test_find_page_image_urls_excludes_sina_bottom_ad_and_qrcode():
    # 测试新浪财经正文无真实图、但尾部有二维码和黑猫投诉广告时的过滤表现
    html = """
    <html>
    <body>
        <div id="artibody">
            <p>快手可灵AI宣布Kling 4.0将于10月正式上线。</p>
            <div class="appendQr_wrap">
                <div class="appendQr_normal"><img src="//n.sinaimg.cn/finance/qr123.png"></div>
            </div>
        </div>
        <div class="ad_content_bottom">
            <div class="article-botton-slide">
                <a href="http://tousu.sina.com.cn/" class="slider-item">
                    <img src="//n.sinaimg.cn/finance/heimao_banner.png">
                </a>
            </div>
        </div>
    </body>
    </html>
    """
    assert find_page_image_urls(html, "https://finance.sina.com.cn/test.shtml") == ()


def test_find_page_image_urls_keeps_real_article_image_and_drops_ad():
    # 测试新浪财经正文内有真实配图，但底部有广告时，仅采纳正文配图
    html = """
    <html>
    <body>
        <div id="artibody">
            <p>快手可灵AI宣布Kling 4.0发布。</p>
            <img src="https://n.sinaimg.cn/finance/kling_real_photo.jpg">
        </div>
        <div class="ad_content_bottom">
            <a href="http://tousu.sina.com.cn/">
                <img src="https://n.sinaimg.cn/finance/heimao_banner.png">
            </a>
        </div>
    </body>
    </html>
    """
    assert find_page_image_urls(html, "https://finance.sina.com.cn/test.shtml") == (
        "https://n.sinaimg.cn/finance/kling_real_photo.jpg",
    )
