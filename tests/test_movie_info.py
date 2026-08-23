from tgexporter.movie_info import (
    build_movie_article_text,
    build_movie_publish_title,
    clean_search_url,
    extract_tmdb_search_urls,
    extract_movie_source_urls,
    fetch_page_metadata_from_html,
    movie_context_search_queries,
    movie_info_from_imdb_suggestions,
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

    assert "影片导读" in text
    assert "故事梗概" in text
    assert "值得关注" in text
    assert "资源信息" in text
    assert "描述：" not in text
    assert "名称：" not in text
    assert "资源搜索机器人" not in text
    assert "训练演习变成了与一种难以想象的威胁之间的生存之战" not in text
    assert "夸克网盘：https://pan.quark.cn/s/example" in text


def test_build_movie_article_text_adds_original_structure_and_verified_metadata():
    from tgexporter.movie_info import MovieInfo

    info = MovieInfo(
        name="War Machine",
        source_name="IMDb",
        source_url="https://www.imdb.com/title/tt1234567/",
        original_name="War Machine",
        release_date="2026-03-06",
        genres=("动作", "科幻"),
        directors=("Patrick Hughes",),
        cast=("Alan Ritchson", "Dennis Quaid"),
        countries=("美国",),
        duration="1小时45分钟",
        rating="7.2/10",
        overview="一支精英部队在训练演习中遭遇未知威胁。",
    )

    text = build_movie_article_text(
        "名称：侵略机器(2026)【4K.HDR】【高码率】【内封简繁英字幕】\n\n"
        "描述：在美国陆军游骑兵选拔的最后阶段，一支精英团队的训练演习变成了与未知威胁之间的生存之战。\n\n"
        "夸克：https://pan.quark.cn/s/example",
        "名称：侵略机器(2026)【4K.HDR】【高码率】【内封简繁英字幕】",
        info,
    )

    assert text.index("影片导读") < text.index("故事梗概") < text.index("值得关注")
    assert text.index("值得关注") < text.index("\n影片资料\n") < text.index("资源信息")
    assert "由Patrick Hughes执导" in text
    assert "类型看点：动作场面" in text
    assert "版本信息：当前整理版本包含4K.HDR、高码率、内封简繁英字幕" in text
    assert "片名：侵略机器(2026)" in text
    assert "导演：Patrick Hughes" in text
    assert "国家/地区：美国" in text
    assert "片长：1小时45分钟" in text
    assert "资料评分：7.2/10" in text
    assert "资料来源" in text
    assert "https://www.imdb.com/title/tt1234567/" not in text


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


def test_extract_movie_source_urls_decodes_duckduckgo_result_url():
    html = (
        '<a href="//duckduckgo.com/l/?uddg='
        'https%3A%2F%2Fwww.themoviedb.org%2Fmovie%2F1265609-war-machine%3Flanguage%3Dzh'
        '&amp;rut=1234567890abcdef">result</a>'
    )

    assert extract_movie_source_urls(html) == [
        "https://www.themoviedb.org/movie/1265609-war-machine?language=zh",
    ]


def test_extract_tmdb_search_urls_keeps_movie_and_tv_results_only():
    html = """
    <a href="/movie/1265609-war-machine?language=zh-CN">War Machine</a>
    <a href="/movie/now-playing?language=zh-CN">Now Playing</a>
    <a href="/tv/12345-made-in-korea?language=zh-CN">Made in Korea</a>
    """

    assert extract_tmdb_search_urls(html) == [
        "https://www.themoviedb.org/movie/1265609-war-machine?language=zh-CN",
        "https://www.themoviedb.org/tv/12345-made-in-korea?language=zh-CN",
    ]


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
      "director": [{"name": "Patrick Hughes"}],
      "actor": [{"name": "Alan Ritchson"}, {"name": "Dennis Quaid"}],
      "countryOfOrigin": {"name": "United States"},
      "duration": "PT1H45M",
      "aggregateRating": {"ratingValue": "7.2", "bestRating": "10"},
      "description": "A team faces an unimaginable threat.",
      "image": ["https://example.com/poster.jpg"]
    }
    </script>
    """

    info = movie_info_from_json_ld(html, "https://www.imdb.com/title/tt1234567/")

    assert info is not None
    assert info.name == "War Machine"
    assert info.release_date == "2026-03-06"
    assert info.genres == ("动作", "科幻")
    assert info.directors == ("Patrick Hughes",)
    assert info.cast == ("Alan Ritchson", "Dennis Quaid")
    assert info.countries == ("美国",)
    assert info.duration == "1小时45分钟"
    assert info.rating == "7.2/10"
    assert info.image_urls == ("https://example.com/poster.jpg",)


def test_movie_info_from_imdb_suggestions_prefers_matching_year():
    payload = {
        "d": [
            {"id": "tt0000001", "l": "Old Movie", "y": 2006},
            {
                "id": "tt15940132",
                "l": "War Machine",
                "y": 2026,
                "s": "Alan Ritchson, Stephan James",
                "i": {"imageUrl": "https://m.media-amazon.com/poster.jpg"},
            },
        ]
    }

    info = movie_info_from_imdb_suggestions(payload, parse_movie_title("名称：侵略机器(2026)"))

    assert info is not None
    assert info.name == "War Machine"
    assert info.release_date == "2026"
    assert info.cast == ("Alan Ritchson", "Stephan James")
    assert info.source_url == "https://www.imdb.com/title/tt15940132/"
    assert info.image_urls == ("https://m.media-amazon.com/poster.jpg",)


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
