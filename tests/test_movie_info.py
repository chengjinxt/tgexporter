from tgexporter.movie_info import (
    build_movie_article_text,
    build_movie_publish_title,
    clean_search_url,
    extract_movie_source_urls,
    fetch_page_metadata_from_html,
    movie_context_search_queries,
    movie_info_from_json_ld,
    parse_movie_title,
)


def test_parse_movie_title_removes_resource_tags():
    title = parse_movie_title("名称：侵略机器(2026)【4K.SDR&DV双版本】【高码率】【内封简繁英】【科幻、动作】")

    assert title.name == "侵略机器"
    assert title.year == "2026"


def test_build_movie_publish_title_removes_name_prefix_and_adds_hook():
    title = build_movie_publish_title(
        "名称：侵略机器(2026)【4K.SDR&DV双版本】【高码率】【内封简繁英】【科幻、动作】",
        "描述：在美国陆军游骑兵选拔的最后阶段，一支精英团队的训练演习变成了与一种难以想象的威胁之间的生存之战。",
    )

    assert title.startswith("侵略机器(2026)：")
    assert "名称：" not in title
    assert "4K" not in title
    assert "精英部队" in title


def test_build_movie_publish_title_uses_genres_when_description_is_too_long():
    title = build_movie_publish_title(
        "名称：泰迪熊 剧版(两季合集)【WEB-DL.1080p】【内封简繁英】【剧情、喜剧】",
        "描述：泰迪熊声名大噪的时期已经过去，现在他和好基友、16岁的约翰·贝内特住在一起，后者来自波士顿的工薪家庭，和父母以及堂姐住一起。",
    )

    assert title == "泰迪熊 剧版(两季合集)：剧情/喜剧新片资源整理"


def test_build_movie_article_text_rewrites_description_label():
    text = build_movie_article_text(
        "名称：侵略机器(2026)【4K.SDR&DV双版本】\n\n"
        "描述：在美国陆军游骑兵选拔的最后阶段，一支精英团队的训练演习变成了与一种难以想象的威胁之间的生存之战。\n\n"
        "夸克：https://pan.quark.cn/s/example\n"
        "百度：https://pan.baidu.com/s/example?pwd=Yu66\n\n"
        "资源搜索机器人bot👉:点击搜索",
        "侵略机器(2026)：精英部队训练突变生存战",
    )

    assert "影片看点" in text
    assert "资源信息" in text
    assert "描述：" not in text
    assert "名称：" not in text
    assert "资源搜索机器人" not in text
    assert "训练演习变成了与一种难以想象的威胁之间的生存之战" not in text
    assert "夸克网盘：https://pan.quark.cn/s/example" in text


def test_movie_context_search_queries_use_description_line():
    queries = movie_context_search_queries("名称：测试片\n\n描述：在美国陆军游骑兵选拔的最后阶段，一支精英团队遭遇未知威胁。")

    assert queries == [
        "在美国陆军游骑兵选拔的最后阶段，一支精英团队遭遇未知威胁。 电影 IMDb",
        "在美国陆军游骑兵选拔的最后阶段，一支精英团队遭遇未知威胁。 电影 豆瓣",
    ]


def test_extract_movie_source_urls_keeps_professional_sites():
    html = """
    <a href="https://www.imdb.com/title/tt1234567/">IMDb</a>
    <a href="https://tmioe.com/movie/war-machine">TMIOE</a>
    <a href="https://example.com/post">Other</a>
    """

    assert extract_movie_source_urls(html) == [
        "https://www.imdb.com/title/tt1234567/",
        "https://tmioe.com/movie/war-machine",
    ]


def test_extract_movie_source_urls_unescapes_script_urls():
    html = '{"url":"https:\\/\\/www.imdb.com\\/title\\/tt1234567\\/"}'

    assert extract_movie_source_urls(html) == ["https://www.imdb.com/title/tt1234567/"]


def test_clean_search_url_decodes_bing_a1_redirect():
    url = clean_search_url("https://www.bing.com/ck/a?u=a1aHR0cHM6Ly93d3cuaW1kYi5jb20vdGl0bGUvdHQxMjM0NTY3Lw")

    assert url == "https://www.imdb.com/title/tt1234567/"


def test_clean_search_url_decodes_google_redirect():
    url = clean_search_url("https://www.google.com/url?q=https%3A%2F%2Fwww.rottentomatoes.com%2Fm%2Fwar_machine")

    assert url == "https://www.rottentomatoes.com/m/war_machine"


def test_movie_info_from_json_ld_extracts_movie_fields():
    html = """
    <script type="application/ld+json">
    {
      "@type": "Movie",
      "name": "War Machine",
      "datePublished": "2026-03-06",
      "genre": ["Action", "Sci-Fi"],
      "actor": [{"name": "Alan Ritchson"}, {"name": "Dennis Quaid"}],
      "description": "A team faces an unimaginable threat.",
      "image": ["https://example.com/poster.jpg"]
    }
    </script>
    """

    info = movie_info_from_json_ld(html, "https://www.imdb.com/title/tt1234567/")

    assert info is not None
    assert info.name == "War Machine"
    assert info.release_date == "2026-03-06"
    assert info.genres == ("Action", "Sci-Fi")
    assert info.cast == ("Alan Ritchson", "Dennis Quaid")
    assert info.image_urls == ("https://example.com/poster.jpg",)


def test_fetch_page_metadata_from_html_reads_description_and_image():
    html = """
    <html><head>
      <title>侵略机器 - TMIOE</title>
      <meta name="description" content="中文剧情简介">
      <meta property="og:image" content="/poster.jpg">
    </head></html>
    """

    metadata = fetch_page_metadata_from_html(html, "https://tmioe.com/movie/war-machine")

    assert metadata.title == "侵略机器 - TMIOE"
    assert metadata.overview == "中文剧情简介"
    assert metadata.image_urls == ("https://tmioe.com/poster.jpg",)
