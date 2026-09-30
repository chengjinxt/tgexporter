from __future__ import annotations

import json
import urllib.parse
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tgexporter.cli import build_parser
from tgexporter.config import TechDigestTranslationConfig, load_config
from tgexporter.tech_digest import (
    BACKUP_TECH_SOURCES,
    BilingualArticle,
    CORE_TECH_SOURCES,
    TechCandidate,
    TechSourceCollector,
    _fetch_text,
    TechDigestRunner,
    format_tech_digest_run_result,
    next_scheduled_run,
    parse_article_metadata,
    parse_ranked_page,
    render_telegram_message,
    run_tech_digest_clear_published_command,
    select_digest_candidates,
)
from tgexporter.tech_digest_store import TechDigestArchive, TechDigestState
from tgexporter.tech_digest_translation import (
    LibreTranslateTranslator,
    OpenAICompatibleTranslator,
    TranslationError,
    build_tech_digest_translator,
)
from tgexporter.models import MediaAsset
from tgexporter.telegram_bot import TelegramBotClient, TelegramBotError, telegram_text_length


def candidate(source_key: str, title: str, url: str, rank: int) -> TechCandidate:
    return TechCandidate(
        source_key=source_key,
        source_name=source_key,
        title=title,
        url=url,
        ranking_method="most_popular",
        ranking_position=rank,
    )


def test_tech_source_catalog_has_eight_core_and_four_backup_sites():
    assert [source.name for source in CORE_TECH_SOURCES] == [
        "The Verge",
        "TechCrunch",
        "Ars Technica",
        "Wired",
        "Engadget",
        "Tom's Hardware",
        "ServeTheHome",
        "Hacker News",
    ]
    assert [source.name for source in BACKUP_TECH_SOURCES] == [
        "IT之家",
        "TechSpot",
        "The Register",
        "BleepingComputer",
    ]


def test_selection_takes_two_per_core_then_uses_backup_for_shortfall():
    core = {
        "verge": [
            candidate("verge", "Verge A", "https://theverge.com/a", 1),
            candidate("verge", "Verge B", "https://theverge.com/b", 2),
        ],
        "techcrunch": [candidate("techcrunch", "TC A", "https://techcrunch.com/a", 1)],
    }
    backup = {
        "ithome": [
            candidate("ithome", "IT A", "https://ithome.com/a", 1),
            candidate("ithome", "IT B", "https://ithome.com/b", 2),
        ]
    }

    selected = select_digest_candidates(core, backup, per_core=2, daily_limit=4)

    assert [item.url for item in selected] == [
        "https://theverge.com/a",
        "https://theverge.com/b",
        "https://techcrunch.com/a",
        "https://ithome.com/a",
    ]


def test_selection_deduplicates_tracking_urls_history_and_similar_events():
    core = {
        "verge": [
            candidate("verge", "OpenAI launches GPT 6 model", "https://theverge.com/gpt?utm_source=x", 1),
            candidate("verge", "Second story", "https://theverge.com/second", 2),
        ],
        "techcrunch": [
            candidate("techcrunch", "OpenAI unveils the GPT 6 model", "https://techcrunch.com/gpt6", 1),
            candidate("techcrunch", "Already sent", "https://techcrunch.com/old", 2),
            candidate("techcrunch", "Fresh story", "https://techcrunch.com/fresh", 3),
        ],
    }

    selected = select_digest_candidates(
        core,
        {},
        processed_urls={"https://techcrunch.com/old?ref=home"},
        per_core=2,
        daily_limit=4,
    )

    assert [item.title for item in selected] == [
        "OpenAI launches GPT 6 model",
        "Second story",
        "Fresh story",
    ]


def test_tech_digest_config_is_independent_and_resolves_output_path(tmp_path):
    (tmp_path / "config.local.toml").write_text(
        """
[tech_digest]
output_dir = "发布内容/科技日报"
target_channel = "@DailyTech"
schedule_at = "20:30"
timezone = "Asia/Shanghai"
articles_per_core = 2
daily_limit = 16
candidates_per_source = 10

[tech_digest.translation]
provider = "openai"
endpoint = "https://example.test/v1/chat/completions"
model = "translator-model"
api_key_env = "MY_TRANSLATION_KEY"
""",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.tech_digest.output_dir == tmp_path / "发布内容" / "科技日报"
    assert config.tech_digest.target_channel == "@DailyTech"
    assert config.tech_digest.schedule_at == "20:30"
    assert config.tech_digest.daily_limit == 16
    assert config.tech_digest.translation.provider == "openai"
    assert config.tech_digest.translation.model == "translator-model"
    assert config.tech_digest.translation.api_key_env == "MY_TRANSLATION_KEY"


def test_tech_digest_translation_defaults_to_bundled_libretranslate_models(tmp_path):
    config = load_config(tmp_path)

    assert config.tech_digest.translation.provider == "libretranslate"
    assert config.tech_digest.translation.model_dir == tmp_path / "runtime" / "translation-models"


def test_tech_digest_command_parses_once_watch_and_publish_flags():
    once = build_parser().parse_args(["tech-digest", "--once", "--dry-run"])
    watch = build_parser().parse_args(
        ["tech-digest", "--watch", "--at", "21:15", "--publish", "--channel", "@DailyTech"]
    )

    assert once.command == "tech-digest"
    assert once.once is True
    assert once.dry_run is True
    assert watch.watch is True
    assert watch.at == "21:15"
    assert watch.publish is True
    assert watch.channel == "@DailyTech"


def test_tech_digest_clear_published_command_requires_date_and_confirmation():
    args = build_parser().parse_args(
        ["tech-digest-clear-published", "--date", "2026-09-26", "--confirm"]
    )

    assert args.command == "tech-digest-clear-published"
    assert args.date == "2026-09-26"
    assert args.confirm is True


def test_tech_digest_clear_published_refuses_without_confirmation(tmp_path):
    args = SimpleNamespace(root=tmp_path, date="2026-09-26", confirm=False)

    with pytest.raises(RuntimeError, match="without --confirm"):
        run_tech_digest_clear_published_command(args)

    assert not (tmp_path / "data" / "tech_digest.sqlite").exists()


def test_tech_digest_clear_published_backs_up_and_only_deletes_the_target_day(tmp_path, capsys):
    target_date = date(2026, 9, 26)
    other_date = date(2026, 9, 25)
    state = TechDigestState(tmp_path / "data" / "tech_digest.sqlite")
    target = candidate("verge", "Target story", "https://theverge.com/target", 1)
    rendered = candidate("ars", "Rendered story", "https://arstechnica.com/rendered", 1)
    other = candidate("wired", "Other day", "https://wired.com/other", 1)
    state.record(target, tmp_path / "target.md", status="rendered", run_date=target_date)
    state.mark_published(target.url, message_id=101)
    state.record(rendered, tmp_path / "rendered.md", status="rendered", run_date=target_date)
    state.record(other, tmp_path / "other.md", status="rendered", run_date=other_date)
    state.mark_published(other.url, message_id=102)

    result = run_tech_digest_clear_published_command(
        SimpleNamespace(root=tmp_path, date=target_date.isoformat(), confirm=True)
    )

    assert result == 0
    assert state.published_count(target_date) == 0
    assert state.published_count(other_date) == 1
    assert state.publication_status(target.url) is None
    assert state.publication_status(rendered.url) == ("rendered", None)
    backups = list((tmp_path / "data").glob("tech_digest.sqlite.before-clear-*.bak"))
    assert len(backups) == 1
    assert TechDigestState(backups[0]).published_count(target_date) == 1
    output = capsys.readouterr().out
    assert "date=2026-09-26" in output
    assert "deleted=1" in output
    assert f"backup={backups[0]}" in output


def test_translation_check_command_parses_as_a_standalone_portable_diagnostic():
    args = build_parser().parse_args(["translation-check"])

    assert args.command == "translation-check"


def test_ranked_page_parser_prefers_links_after_the_sites_ranking_heading():
    source = CORE_TECH_SOURCES[0]
    html = """
    <nav><a href="/about-us-and-our-company">About The Verge Company</a></nav>
    <h2>Most Popular</h2>
    <ol>
      <li><a href="/2026/9/25/first-story?utm_source=home">First important technology story</a></li>
      <li><a href="https://www.theverge.com/2026/9/25/second-story">Second important technology story</a></li>
    </ol>
    """

    candidates = parse_ranked_page(source, html, limit=2)

    assert [item.title for item in candidates] == [
        "First important technology story",
        "Second important technology story",
    ]
    assert candidates[0].url == "https://theverge.com/2026/9/25/first-story"
    assert candidates[0].ranking_method == "most_popular"
    assert candidates[0].ranking_position == 1


def test_ranked_page_parser_uses_article_blocks_when_a_site_has_no_stable_popular_list():
    source = CORE_TECH_SOURCES[3]
    html = """
    <nav><a href="/about">A long navigation label that must be ignored</a></nav>
    <main>
      <article><a href="/story/alpha">A major science story from Wired today</a></article>
      <article><a href="/story/beta">A second technology story from Wired today</a></article>
    </main>
    """

    candidates = parse_ranked_page(source, html, limit=2)

    assert [item.url for item in candidates] == [
        "https://wired.com/story/alpha",
        "https://wired.com/story/beta",
    ]


def test_hacker_news_collector_uses_topstories_order_and_records_score():
    responses = {
        "https://hacker-news.firebaseio.com/v0/topstories.json": "[101, 102]",
        "https://hacker-news.firebaseio.com/v0/item/101.json": (
            '{"id":101,"type":"story","title":"First HN story","url":"https://example.com/one","score":350,"time":1789981200}'
        ),
        "https://hacker-news.firebaseio.com/v0/item/102.json": (
            '{"id":102,"type":"story","title":"Second HN story","score":200}'
        ),
    }
    collector = TechSourceCollector(fetch_text=lambda url: responses[url])

    candidates = collector.collect_source(CORE_TECH_SOURCES[7], limit=2)

    assert [item.ranking_position for item in candidates] == [1, 2]
    assert candidates[0].ranking_score == 350
    assert candidates[0].published_at == "2026-09-21T09:00:00Z"
    assert candidates[0].source_url == "https://news.ycombinator.com/"
    assert candidates[0].discussion_url == "https://news.ycombinator.com/item?id=101"
    assert candidates[1].url == "https://news.ycombinator.com/item?id=102"


def test_article_metadata_prefers_open_graph_title_and_description():
    item = candidate("verge", "Short list title", "https://theverge.com/story", 1)
    html = """
    <html><head>
      <meta property="og:title" content="The complete article title">
      <meta property="og:description" content="A concise source-provided description.">
      <meta property="article:published_time" content="2026-09-25T08:00:00Z">
    </head></html>
    """

    enriched = parse_article_metadata(item, html)

    assert enriched.title == "The complete article title"
    assert enriched.summary == "A concise source-provided description."
    assert enriched.published_at == "2026-09-25T08:00:00Z"


def test_article_metadata_reads_json_ld_publication_date_when_meta_is_missing():
    item = candidate("wired", "Story", "https://wired.com/story/example", 1)
    html = """
    <html><head>
      <script type="application/ld+json">
        {"@type":"NewsArticle","datePublished":"2026-09-25T10:00:00.000Z"}
      </script>
    </head></html>
    """

    enriched = parse_article_metadata(item, html)

    assert enriched.published_at == "2026-09-25T10:00:00.000Z"


def test_article_metadata_builds_a_two_paragraph_summary_from_article_facts():
    item = candidate("techcrunch", "PrismML brings tiny LLMs to smart glasses", "https://techcrunch.com/story", 1)
    html = """
    <html><head>
      <meta property="og:description" content="PrismML created tiny language models for smart glasses powered by Qualcomm Snapdragon chips.">
    </head><body><article>
      <p>PrismML created tiny language models for smart glasses powered by Qualcomm Snapdragon chips.</p>
      <p>Qualcomm demonstrated the 1-bit Bonsai model running locally on its Snapdragon AR1 Gen 1 platform.</p>
      <p>The company says its approach makes larger models about 4x smaller while retaining benchmark performance.</p>
      <p>The glasses model has 2 billion parameters and is tuned for vision and language so users can ask about what they see.</p>
      <p>However, no smart glasses using the PrismML model have been announced yet.</p>
    </article></body></html>
    """

    enriched = parse_article_metadata(item, html)

    paragraphs = enriched.summary.split("\n\n")
    assert len(paragraphs) == 2
    assert paragraphs[0].startswith("PrismML created tiny language models")
    assert sum(enriched.summary.count(mark) for mark in ".!?") >= 3
    assert "2 billion parameters" in enriched.summary
    assert "no smart glasses" in enriched.summary


def test_article_metadata_removes_the_source_name_from_the_page_title():
    item = replace(
        candidate("techcrunch", "Short title", "https://techcrunch.com/story", 1),
        source_name="TechCrunch",
    )
    html = '<meta property="og:title" content="A complete technology headline | TechCrunch">'

    enriched = parse_article_metadata(item, html)

    assert enriched.title == "A complete technology headline"


def test_real_collector_rejects_a_single_sentence_metadata_only_summary():
    item = candidate("techcrunch", "Short story", "https://techcrunch.com/story", 1)
    collector = TechSourceCollector(
        fetch_text=lambda _url: '<meta property="og:description" content="Only one short sentence is available.">'
    )

    with pytest.raises(ValueError, match="substantial multi-sentence summary"):
        collector.enrich(item)


def test_article_metadata_collects_original_images_and_direct_videos():
    item = candidate("verge", "Story", "https://theverge.com/story/page", 1)
    html = """
    <html><head>
      <meta property="og:image" content="https://cdn.example.test/hero.jpg">
      <meta property="og:video" content="/media/report.mp4">
    </head><body>
      <video poster="/media/poster.jpg"><source src="https://cdn.example.test/demo.webm" type="video/webm"></video>
    </body></html>
    """

    enriched = parse_article_metadata(item, html)

    assert enriched.image_urls == (
        "https://cdn.example.test/hero.jpg",
        "https://theverge.com/media/poster.jpg",
    )
    assert enriched.video_urls == (
        "https://theverge.com/media/report.mp4",
        "https://cdn.example.test/demo.webm",
    )


def test_ranked_parser_filters_author_category_and_subscription_links():
    techcrunch = CORE_TECH_SOURCES[1]
    html = """
    <h2>Most Popular</h2>
    <a href="/2026/09/25/real-story/">A real and important technology story</a>
    <a href="/author/some-reporter/">Some Reporter Name</a>
    <a href="/category/hardware/">Hardware Industry</a>
    <a href="/store/product/subscriptions/">Discover all the benefits of a subscription</a>
    """

    candidates = parse_ranked_page(techcrunch, html, limit=5)

    assert [item.title for item in candidates] == ["A real and important technology story"]


def test_wired_reads_editorial_order_from_json_ld_item_list():
    wired = CORE_TECH_SOURCES[3]
    html = """
    <script type="application/ld+json">
    {"@type":"ItemList","itemListElement":[
      {"@type":"ListItem","name":"First Wired science story","url":"https://www.wired.com/story/first/","position":1},
      {"@type":"ListItem","name":"Second Wired security story","url":"https://www.wired.com/story/second/","position":2}
    ]}
    </script>
    """

    candidates = parse_ranked_page(wired, html, limit=2)

    assert [item.title for item in candidates] == [
        "First Wired science story",
        "Second Wired security story",
    ]


def test_servethehome_starts_at_homepage_modules_and_skips_taxonomy_links():
    source = CORE_TECH_SOURCES[6]
    html = """
    <nav><a href="/buyers-guides/old-guide/">An evergreen buyer guide</a></nav>
    <div class="td_module_flex_6 td-big-grid-flex-post">
      <a href="/category/server-parts/">Server Motherboards</a>
      <h3><a href="/new-server-review/">A new enterprise server hardware review</a></h3>
      <a href="/author/reporter/">Reporter Name</a>
    </div>
    """

    candidates = parse_ranked_page(source, html, limit=3)

    assert [item.title for item in candidates] == ["A new enterprise server hardware review"]


def test_engadget_and_toms_hardware_start_from_story_sections_not_navigation():
    engadget = CORE_TECH_SOURCES[4]
    toms = CORE_TECH_SOURCES[5]

    assert engadget.ranking_labels == ("More Stories",)
    assert toms.ranking_labels == ("News Stream",)


def test_source_fetch_retries_a_transient_network_eof(monkeypatch):
    attempts = []

    class Headers:
        def get_content_charset(self):
            return "utf-8"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b"recovered"

    def fake_urlopen(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) == 1:
            raise OSError("unexpected EOF")
        return Response()

    monkeypatch.setattr("tgexporter.tech_digest.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("tgexporter.tech_digest.sleep", lambda _seconds: None)

    assert _fetch_text("https://example.test/news") == "recovered"
    assert len(attempts) == 2


class JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class RecordingOpener:
    def __init__(self, payload):
        self.payload = payload
        self.request = None
        self.timeout = None

    def open(self, request, timeout=90):
        self.request = request
        self.timeout = timeout
        return JsonResponse(self.payload)


class MultiRecordingOpener:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def open(self, request, timeout=90):
        self.requests.append(request)
        return JsonResponse(self.payloads.pop(0))


def test_openai_compatible_translator_requests_strict_bilingual_json(monkeypatch):
    response_content = {
        "zh_title": "新芯片发布",
        "en_title": "A new chip launches",
        "zh_summary": "新芯片提升了设备性能。\n\n它同时降低了运行功耗。",
        "en_summary": "The new chip improves device performance.\n\nIt also reduces operating power.",
    }
    opener = RecordingOpener(
        {"choices": [{"message": {"content": json.dumps(response_content, ensure_ascii=False)}}]}
    )
    monkeypatch.setenv("TEST_TRANSLATION_KEY", "secret-token")
    translator = OpenAICompatibleTranslator(
        endpoint="https://example.test/v1/chat/completions",
        model="translator-model",
        api_key_env="TEST_TRANSLATION_KEY",
        timeout_seconds=45,
        opener=opener,
    )
    item = candidate("verge", "A new chip launches", "https://theverge.com/chip", 1)

    result = translator.translate(item)

    request_body = json.loads(opener.request.data.decode("utf-8"))
    assert opener.request.headers["Authorization"] == "Bearer secret-token"
    assert request_body["model"] == "translator-model"
    assert request_body["response_format"] == {"type": "json_object"}
    assert "zh_title" in request_body["messages"][0]["content"]
    assert result.zh_title == "新芯片发布"
    assert result.en_summary == "The new chip improves device performance.\n\nIt also reduces operating power."


def test_translator_configuration_fails_before_collection_when_model_or_key_is_missing():
    translator = OpenAICompatibleTranslator(
        endpoint="https://example.test/v1/chat/completions",
        model="",
        api_key_env="MISSING_TRANSLATION_KEY",
        environ={},
    )

    with pytest.raises(TranslationError, match="translation.model"):
        translator.validate()


class DictionaryTranslationBackend:
    def __init__(self, values):
        self.values = values

    def validate(self):
        return None

    def translate(self, text, source, target):
        return self.values[(text, source, target)]


def test_libretranslate_translator_exposes_direct_text_translation_for_diagnostics(tmp_path):
    backend = DictionaryTranslationBackend(
        {("A new processor launches.", "en", "zh"): "新处理器发布。"}
    )
    translator = LibreTranslateTranslator(model_dir=tmp_path / "models", backend=backend)

    result = translator.translate_text("A new processor launches.", "en", "zh")

    assert result == "新处理器发布。"


def test_libretranslate_translator_repairs_near_miss_technical_names(tmp_path):
    source = "PrismML demonstrated its 1-bit model."
    backend = DictionaryTranslationBackend(
        {(source, "en", "zh"): "它展示了运行PrismL的 1 位Bonsai 模型。"}
    )
    translator = LibreTranslateTranslator(model_dir=tmp_path / "models", backend=backend)

    result = translator.translate_text(source, "en", "zh")

    assert result == "它展示了运行PrismML的 1-bit Bonsai 模型。"


def test_libretranslate_translator_preserves_english_and_translates_to_chinese(tmp_path):
    item = TechCandidate(
        source_key="verge",
        source_name="The Verge",
        title="A new processor launches",
        url="https://example.test/processor",
        ranking_method="most_popular",
        ranking_position=1,
        summary="The chip improves performance while using less power.",
    )
    backend = DictionaryTranslationBackend(
        {
            (item.title, "en", "zh"): "新处理器发布",
            (item.summary, "en", "zh"): "这款芯片在降低功耗的同时提升了性能。",
        }
    )
    translator = LibreTranslateTranslator(model_dir=tmp_path / "models", backend=backend)

    result = translator.translate(item)

    assert result.en_title == item.title
    assert result.en_summary == item.summary
    assert result.zh_title == "新处理器发布"
    assert result.zh_summary == "这款芯片在降低功耗的同时提升了性能。"


def test_local_translator_preserves_two_summary_paragraphs(tmp_path):
    item = TechCandidate(
        source_key="techcrunch",
        source_name="TechCrunch",
        title="A compact model reaches smart glasses",
        url="https://example.test/glasses",
        ranking_method="most_popular",
        ranking_position=1,
        summary=(
            "The model runs locally on smart glasses and handles visual questions.\n\n"
            "It has two billion parameters, while no commercial glasses have been announced."
        ),
    )
    backend = DictionaryTranslationBackend(
        {
            (item.title, "en", "zh"): "小型模型进入智能眼镜",
            (
                "The model runs locally on smart glasses and handles visual questions.",
                "en",
                "zh",
            ): "该模型可在智能眼镜上本地运行，并处理视觉问题。",
            (
                "It has two billion parameters, while no commercial glasses have been announced.",
                "en",
                "zh",
            ): "模型拥有二十亿参数，但尚未公布搭载它的商业眼镜。",
        }
    )
    translator = LibreTranslateTranslator(model_dir=tmp_path / "models", backend=backend)

    result = translator.translate(item)

    assert result.en_summary.count("\n\n") == 1
    assert result.zh_summary == (
        "该模型可在智能眼镜上本地运行，并处理视觉问题。\n\n"
        "模型拥有二十亿参数，但尚未公布搭载它的商业眼镜。"
    )


def test_libretranslate_translator_detects_chinese_and_translates_to_english(tmp_path):
    item = TechCandidate(
        source_key="ithome",
        source_name="IT之家",
        title="新款处理器正式发布",
        url="https://example.test/chip",
        ranking_method="daily_ranking",
        ranking_position=1,
        summary="新款处理器提升性能并降低功耗。",
    )
    backend = DictionaryTranslationBackend(
        {
            (item.title, "zh", "en"): "New processor officially launches",
            (item.summary, "zh", "en"): "The new processor improves performance and reduces power use.",
        }
    )
    translator = LibreTranslateTranslator(model_dir=tmp_path / "models", backend=backend)

    result = translator.translate(item)

    assert result.zh_title == item.title
    assert result.zh_summary == item.summary
    assert result.en_title == "New processor officially launches"
    assert result.en_summary == "The new processor improves performance and reduces power use."


def test_libretranslate_translator_rejects_a_portable_package_without_both_models(tmp_path):
    translator = LibreTranslateTranslator(model_dir=tmp_path / "missing-models")

    with pytest.raises(TranslationError, match="bundled English.*Chinese models"):
        translator.validate()


def test_translation_factory_uses_local_provider_without_an_api_key(tmp_path):
    config = TechDigestTranslationConfig(
        provider="libretranslate",
        model_dir=tmp_path / "models",
    )

    translator = build_tech_digest_translator(config)

    assert isinstance(translator, LibreTranslateTranslator)


def bilingual_article() -> BilingualArticle:
    return BilingualArticle(
        candidate=replace(
            candidate("verge", "A new chip launches", "https://theverge.com/chip", 1),
            source_name="The Verge",
            source_url="https://www.theverge.com/",
            published_at="2026-09-25T08:00:00Z",
        ),
        zh_title="新芯片发布",
        en_title="A new chip launches",
        zh_summary="这是一段中文摘要。",
        en_summary="This is a concise English summary.",
    )


def test_telegram_message_matches_bilingual_article_template():
    message = render_telegram_message(bilingual_article(), index=1, total=16)

    assert message == "\n".join(
        [
            "<b>新芯片发布</b>",
            "这是一段中文摘要。",
            "",
            "<b>A new chip launches</b>",
            "This is a concise English summary.",
            "",
            '<a href="https://www.theverge.com/">The Verge</a> | '
            '<a href="https://theverge.com/chip">原文 / Source</a>',
            "发布时间 / Published: 2026-09-25 08:00 UTC",
        ]
    )
    assert "科技日报" not in message
    assert "Ranking" not in message


def test_archive_and_state_are_separate_and_preserve_exact_telegram_message(tmp_path):
    article = bilingual_article()
    message = render_telegram_message(article, index=1, total=16)
    archive = TechDigestArchive(tmp_path / "科技日报")
    path = archive.write_article(article, message, date(2026, 9, 25), index=1)
    state = TechDigestState(tmp_path / "data" / "tech_digest.sqlite")

    state.record(article.candidate, path, status="rendered", run_date=date(2026, 9, 25))
    state.mark_published(article.candidate.url, message_id=987)

    content = path.read_text(encoding="utf-8")
    assert path.parent.name == "20260925"
    assert "新芯片发布" in content
    assert message in content
    assert state.processed_urls() == {"https://theverge.com/chip"}
    assert state.publication_status(article.candidate.url) == ("published", 987)
    assert state.published_count(date(2026, 9, 25)) == 1


def test_archive_embeds_downloaded_image_and_video_in_the_local_article(tmp_path):
    image = MediaAsset("image", "hero.jpg", tmp_path / "hero.jpg", "original_article_image", "原文配图")
    video = MediaAsset("video", "report.mp4", tmp_path / "report.mp4", "original_article_video", "原文视频")
    image.path.write_bytes(b"image")
    video.path.write_bytes(b"video")
    article = replace(bilingual_article(), media=(image, video))
    archive = TechDigestArchive(tmp_path / "科技日报")

    path = archive.write_article(article, "telegram body", date(2026, 9, 26), index=1)

    content = path.read_text(encoding="utf-8")
    assert "![原文配图](hero.jpg)" in content
    assert '<video controls src="report.mp4"></video>' in content


def test_telegram_send_message_uses_html_and_returns_message_id():
    opener = RecordingOpener({"ok": True, "result": {"message_id": 321}})
    client = TelegramBotClient("123:test")
    client.opener = opener

    result = client.send_message("@DailyTech", "<b>科技日报</b>")

    form = urllib.parse.parse_qs(opener.request.data.decode("utf-8"))
    assert form["chat_id"] == ["@DailyTech"]
    assert form["parse_mode"] == ["HTML"]
    assert form["text"] == ["<b>科技日报</b>"]
    assert result["message_id"] == 321


def test_telegram_send_message_adds_at_prefix_to_channel_username():
    opener = RecordingOpener({"ok": True, "result": {"message_id": 322}})
    client = TelegramBotClient("123:test")
    client.opener = opener

    client.send_message("DailyTech", "<b>科技日报</b>")

    form = urllib.parse.parse_qs(opener.request.data.decode("utf-8"))
    assert form["chat_id"] == ["@DailyTech"]


def test_telegram_text_length_counts_link_labels_instead_of_hidden_urls():
    message = '<a href="https://example.test/' + ("x" * 2000) + '">原文 / Source</a>'

    assert telegram_text_length(message) == len("原文 / Source")


def test_telegram_send_article_prefers_video_as_the_template_media(tmp_path):
    image_path = tmp_path / "hero.jpg"
    video_path = tmp_path / "report.mp4"
    image_path.write_bytes(b"jpeg-content")
    video_path.write_bytes(b"video-content")
    media = (
        MediaAsset("image", image_path.name, image_path, "original_article_image"),
        MediaAsset("video", video_path.name, video_path, "original_article_video"),
    )
    opener = MultiRecordingOpener(
        [
            {"ok": True, "result": {"message_id": 501}},
            {"ok": True, "result": {"message_id": 502}},
        ]
    )
    client = TelegramBotClient("123:test")
    client.opener = opener

    result = client.send_article("@DailyTech", "<b>双语科技文章</b>", media)

    assert [request.full_url.rsplit("/", 1)[-1] for request in opener.requests] == ["sendVideo"]
    video_body = opener.requests[0].data
    assert b'name="video"; filename="report.mp4"' in video_body
    assert b'name="supports_streaming"' in video_body
    assert b'name="caption"' in video_body
    assert "双语科技文章".encode("utf-8") in video_body
    assert result["message_id"] == 501
    assert result["media_errors"] == []


def test_telegram_send_article_falls_back_to_image_when_video_upload_fails(tmp_path, monkeypatch):
    image_path = tmp_path / "hero.jpg"
    video_path = tmp_path / "report.mp4"
    image_path.write_bytes(b"jpeg-content")
    video_path.write_bytes(b"video-content")
    media = (
        MediaAsset("image", image_path.name, image_path, "original_article_image"),
        MediaAsset("video", video_path.name, video_path, "original_article_video"),
    )
    client = TelegramBotClient("123:test")
    calls = []

    def fail_video(chat_id, path, caption=""):
        calls.append(("video", path.name, caption))
        raise TelegramBotError("video rejected")

    def send_photo(chat_id, path, caption=""):
        calls.append(("photo", path.name, caption))
        return {"message_id": 503}

    monkeypatch.setattr(client, "send_video", fail_video)
    monkeypatch.setattr(client, "send_photo", send_photo)

    result = client.send_article("@DailyTech", "<b>双语科技文章</b>", media)

    assert [call[0] for call in calls] == ["video", "photo"]
    assert result["message_id"] == 503
    assert result["media_errors"] == ["video rejected"]


class FakeCollector:
    def collect_source(self, source, limit=12):
        if source.key == "techcrunch":
            raise OSError("site temporarily unavailable")
        if source.key == "verge":
            return [candidate("verge", "Core technology story", "https://theverge.com/core", 1)]
        if source.key == "ithome":
            return [candidate("ithome", "备用科技新闻报道", "https://ithome.com/backup", 1)]
        return []

    def enrich(self, item):
        return item


class FakeTranslator:
    def translate(self, item):
        return BilingualArticle(
            candidate=item,
            zh_title=f"中文：{item.title}",
            en_title=f"English: {item.title}",
            zh_summary="中文摘要。",
            en_summary="English summary.",
        )


class FakePublisher:
    def __init__(self):
        self.messages = []

    def send_message(self, channel, message):
        self.messages.append((channel, message))
        return {"message_id": 100 + len(self.messages)}


def test_runner_collects_media_before_archiving_and_publishes_the_article_with_it(tmp_path):
    class OneStoryCollector:
        def collect_source(self, source, limit=12):
            return [candidate("verge", "Story with media", "https://theverge.com/media-story", 1)]

        def enrich(self, item):
            return item

    class OneImageCollector:
        def collect(self, item, run_date, index, title):
            path = tmp_path / "科技日报" / run_date.strftime("%Y%m%d") / "hero.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"image")
            return (MediaAsset("image", path.name, path, "original_article_image", "原文配图"),)

    class ArticlePublisher:
        def __init__(self):
            self.articles = []

        def send_article(self, channel, message, media):
            self.articles.append((channel, message, tuple(media)))
            return {"message_id": 701, "media_errors": []}

    publisher = ArticlePublisher()
    runner = TechDigestRunner(
        collector=OneStoryCollector(),
        translator=FakeTranslator(),
        media_collector=OneImageCollector(),
        archive=TechDigestArchive(tmp_path / "科技日报"),
        state=TechDigestState(tmp_path / "data" / "tech_digest.sqlite"),
        publisher=publisher,
        core_sources=(CORE_TECH_SOURCES[0],),
        backup_sources=(),
    )

    result = runner.run_once(
        run_date=date(2026, 9, 26),
        candidates_per_source=2,
        per_core=1,
        daily_limit=1,
        publish=True,
        target_channel="@DailyTech",
    )

    assert result.published_count == 1
    assert publisher.articles[0][2][0].filename == "hero.jpg"
    assert "![原文配图](hero.jpg)" in result.article_paths[0].read_text(encoding="utf-8")


def test_runner_uses_backup_after_core_failure_and_publishes_only_after_archiving(tmp_path):
    publisher = FakePublisher()
    state = TechDigestState(tmp_path / "data" / "tech_digest.sqlite")
    runner = TechDigestRunner(
        collector=FakeCollector(),
        translator=FakeTranslator(),
        archive=TechDigestArchive(tmp_path / "科技日报"),
        state=state,
        publisher=publisher,
        core_sources=(CORE_TECH_SOURCES[0], CORE_TECH_SOURCES[1]),
        backup_sources=(BACKUP_TECH_SOURCES[0],),
    )

    result = runner.run_once(
        run_date=date(2026, 9, 25),
        candidates_per_source=5,
        per_core=1,
        daily_limit=2,
        publish=True,
        target_channel="@DailyTech",
    )

    assert result.selected_count == 2
    assert result.published_count == 2
    assert len(publisher.messages) == 2
    assert all(path.exists() for path in result.article_paths)
    assert result.manifest_path.exists()
    assert state.publication_status("https://theverge.com/core") == ("published", 101)
    assert any("TechCrunch" in error for error in result.errors)


def test_runner_never_exceeds_the_daily_publication_limit_on_a_second_run(tmp_path):
    class TrackingCollector:
        def __init__(self):
            self.calls = []

        def collect_source(self, source, limit=12):
            self.calls.append((source.key, limit))
            return [candidate(source.key, "Should not be collected", "https://example.com/unused", 1)]

    run_date = date(2026, 9, 25)
    state = TechDigestState(tmp_path / "data" / "tech_digest.sqlite")
    already_published = candidate("ars", "Earlier story", "https://arstechnica.com/earlier", 1)
    state.record(already_published, tmp_path / "earlier.md", status="rendered", run_date=run_date)
    state.mark_published(already_published.url, message_id=99)
    publisher = FakePublisher()
    collector = TrackingCollector()
    runner = TechDigestRunner(
        collector=collector,
        translator=FakeTranslator(),
        archive=TechDigestArchive(tmp_path / "科技日报"),
        state=state,
        publisher=publisher,
        core_sources=(CORE_TECH_SOURCES[0],),
        backup_sources=(BACKUP_TECH_SOURCES[0],),
    )

    result = runner.run_once(
        run_date=run_date,
        candidates_per_source=5,
        per_core=1,
        daily_limit=1,
        publish=True,
        target_channel="@DailyTech",
    )

    assert result.selected_count == 0
    assert result.published_count == 0
    assert publisher.messages == []
    assert collector.calls == []
    assert result.daily_limit_reached is True
    assert result.published_today == 1
    assert result.daily_limit == 1
    assert result.errors == ()
    assert format_tech_digest_run_result(result, publish=True).startswith(
        "Tech digest: daily limit reached (1/1), selected=0, published=0, manifest="
    )


def test_runner_replaces_a_cross_language_duplicate_with_a_backup_candidate(tmp_path):
    class DuplicateCollector:
        def collect_source(self, source, limit=12):
            mapping = {
                "verge": [candidate("verge", "Vendor announces project X", "https://theverge.com/a", 1)],
                "techcrunch": [
                    candidate("techcrunch", "A mysterious new product appears", "https://techcrunch.com/b", 1)
                ],
                "ithome": [candidate("ithome", "备用独家报道", "https://ithome.com/c", 1)],
            }
            return mapping.get(source.key, [])

        def enrich(self, item):
            return item

    class DuplicateAwareTranslator:
        def translate(self, item):
            duplicate = item.url.endswith(("/a", "/b"))
            return BilingualArticle(
                candidate=item,
                zh_title="厂商发布 X 项目" if duplicate else "备用独家报道",
                en_title="Vendor launches project X" if duplicate else "A unique backup report",
                zh_summary="摘要。",
                en_summary="Summary.",
            )

    publisher = FakePublisher()
    runner = TechDigestRunner(
        collector=DuplicateCollector(),
        translator=DuplicateAwareTranslator(),
        archive=TechDigestArchive(tmp_path / "科技日报"),
        state=TechDigestState(tmp_path / "data" / "tech_digest.sqlite"),
        publisher=publisher,
        core_sources=(CORE_TECH_SOURCES[0], CORE_TECH_SOURCES[1]),
        backup_sources=(BACKUP_TECH_SOURCES[0],),
    )

    result = runner.run_once(
        run_date=date(2026, 9, 25),
        candidates_per_source=5,
        per_core=1,
        daily_limit=2,
        publish=True,
        target_channel="@DailyTech",
    )

    assert result.published_count == 2
    assert "A unique backup report" in publisher.messages[1][1]
    assert any("bilingual duplicate" in error for error in result.errors)


def test_next_scheduled_run_uses_configured_timezone_and_rolls_to_tomorrow():
    timezone = ZoneInfo("Asia/Shanghai")

    before = next_scheduled_run(datetime(2026, 9, 25, 19, 30, tzinfo=timezone), "20:00", timezone)
    after = next_scheduled_run(datetime(2026, 9, 25, 20, 1, tzinfo=timezone), "20:00", timezone)

    assert before == datetime(2026, 9, 25, 20, 0, tzinfo=timezone)
    assert after == datetime(2026, 9, 26, 20, 0, tzinfo=timezone)
