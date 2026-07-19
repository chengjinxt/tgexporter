from pathlib import Path

from tgexporter.cli import build_parser, cmd_collect_web
from tgexporter.markdown_renderer import MarkdownRenderer
from tgexporter.state import StateStore
from tgexporter.web_sources import (
    WebSourceCollector,
    WebSourceDefinition,
    account_angle_lines,
    load_web_source_group,
    parse_moe_article,
    parse_moe_list,
)

LIST_URL = "http://www.moe.gov.cn/jyb_xwfb/gzdt_gzdt/"
ARTICLE_URL = "http://www.moe.gov.cn/jyb_xwfb/gzdt_gzdt/moe_1485/202607/t20260719_1444325.html"
SECOND_ARTICLE_URL = "http://www.moe.gov.cn/jyb_xwfb/gzdt_gzdt/moe_1485/202607/t20260718_1444258.html"

MOE_LIST_HTML = """
<html><body>
<ul id="list">
  <li><a href="./moe_1485/202607/t20260719_1444325.html" target="_blank"
      title="国门学校优秀青年教师培养支持计划正式启动实施">国门学校优秀青年教师培养支持计划正式启动实施</a><span>2026-07-19</span></li>
  <li><a href="./s78312/202607/t20260717_1444186.html" target="_blank"
      title="于祥成任华南理工大学党委书记">于祥成任华南理工大学党委书记</a><span>2026-07-17</span></li>
</ul>
</body></html>
"""

MOE_ARTICLE_HTML = """
<html><head>
<meta name="ArticleTitle" content="国门学校优秀青年教师培养支持计划正式启动实施">
<meta name="PubDate" content="2026-07-19 07:16">
<meta name="ContentSource" content="教育部">
</head><body>
<div class="moe-detail-box">
<h1>国门学校优秀青年教师培养支持计划正式启动实施</h1>
<div class=TRS_Editor>
<p>为深入贯彻全国教育大会精神，近日，国门学校优秀青年教师培养支持计划启动会在黑龙江省黑河市召开。</p>
<p>会议指出，边境地区教师是扎根国门一线、托举边疆未来的重要力量。</p>
<p>该计划通过资助与培养相结合方式，系统提升入选教师教育教学能力。</p>
</div>
</div>
</body></html>
"""

MOE_LIST_TWO_PUBLISHABLE_HTML = f"""
<html><body>
<ul id="list">
  <li><a href="{ARTICLE_URL}" title="国门学校优秀青年教师培养支持计划正式启动实施">国门学校优秀青年教师培养支持计划正式启动实施</a><span>2026-07-19</span></li>
  <li><a href="{SECOND_ARTICLE_URL}" title="国家智慧教育平台全面深化应用试点推进会召开">国家智慧教育平台全面深化应用试点推进会召开</a><span>2026-07-18</span></li>
</ul>
</body></html>
"""


def test_parse_moe_list_resolves_entries_and_urls():
    entries = parse_moe_list(MOE_LIST_HTML, LIST_URL)

    assert len(entries) == 2
    assert entries[0].title == "国门学校优秀青年教师培养支持计划正式启动实施"
    assert entries[0].url == ARTICLE_URL
    assert entries[0].date_text == "2026-07-19"


def test_parse_moe_article_extracts_metadata_and_body():
    article = parse_moe_article(
        MOE_ARTICLE_HTML,
        ARTICLE_URL,
        fallback_title="备用标题",
        fallback_date="2026-07-19",
        timezone=__import__("zoneinfo").ZoneInfo("Asia/Shanghai"),
    )

    assert article.title == "国门学校优秀青年教师培养支持计划正式启动实施"
    assert article.published_at.strftime("%Y-%m-%d %H:%M") == "2026-07-19 07:16"
    assert article.source_name == "教育部"
    assert article.paragraphs[1].startswith("会议指出，边境地区教师")
    assert "教师培养" in account_angle_lines(article)[0]


def test_web_source_collector_renders_markdown_and_skips_duplicate(tmp_path: Path):
    source = WebSourceDefinition(
        key="moe-work-updates",
        name="教育部工作动态",
        account="chunhui-xuefu",
        list_url=LIST_URL,
        parser="moe",
        brand_text="春晖学府",
    )
    pages = {LIST_URL: MOE_LIST_HTML, ARTICLE_URL: MOE_ARTICLE_HTML}

    def fake_fetch(url, proxy_url=None):
        return pages[url]

    state = StateStore(tmp_path / "state.sqlite")
    renderer = MarkdownRenderer(tmp_path / "发布内容")
    collector = WebSourceCollector(state=state, renderer=renderer, fetch_text=fake_fetch)

    paths = collector.collect_once([source], limit=2)
    duplicate_paths = collector.collect_once([source], limit=2)

    assert len(paths) == 1
    assert duplicate_paths == []
    assert paths[0].name == "20260719_001_国门学校优秀青年教师培养支持计划正式启动实施.md"
    text = paths[0].read_text(encoding="utf-8")
    assert "source: web" in text
    assert "channel: chunhui-xuefu" in text
    assert "春晖学府关注" in text
    assert "可转视频口播" in text
    assert "教育部\n\nhttp://www.moe.gov.cn/jyb_xwfb/gzdt_gzdt/moe_1485/202607/t20260719_1444325.html" in text
    assert (paths[0].parent / "20260719_001_PIC_001_国门学校优秀青年教师培养支持计划正式启动实施.png").exists()
    assert state.source_url_processed("chunhui-xuefu", ARTICLE_URL)


def test_web_source_collector_only_backfills_when_requested(tmp_path: Path):
    source = WebSourceDefinition(
        key="moe-work-updates",
        name="教育部工作动态",
        account="chunhui-xuefu",
        list_url=LIST_URL,
        parser="moe",
        brand_text="春晖学府",
    )
    second_article_html = MOE_ARTICLE_HTML.replace("国门学校优秀青年教师培养支持计划正式启动实施", "国家智慧教育平台全面深化应用试点推进会召开").replace(
        "2026-07-19 07:16",
        "2026-07-18 20:00",
    )
    pages = {
        LIST_URL: MOE_LIST_TWO_PUBLISHABLE_HTML,
        ARTICLE_URL: MOE_ARTICLE_HTML,
        SECOND_ARTICLE_URL: second_article_html,
    }

    def fake_fetch(url, proxy_url=None):
        return pages[url]

    state = StateStore(tmp_path / "state.sqlite")
    renderer = MarkdownRenderer(tmp_path / "发布内容")
    collector = WebSourceCollector(state=state, renderer=renderer, fetch_text=fake_fetch)

    collector.collect_once([source], limit=1)

    assert collector.collect_once([source], limit=1) == []
    assert len(collector.collect_once([source], limit=1, backfill=True)) == 1


def test_load_default_web_source_group_for_chunhui_xuefu():
    sources = load_web_source_group("chunhui-xuefu")

    assert sources[0].account == "chunhui-xuefu"
    assert sources[0].list_url == LIST_URL


def test_collect_web_command_parses_group_limit_and_interval(tmp_path: Path):
    args = build_parser().parse_args(
        [
            "--root",
            str(tmp_path),
            "collect-web",
            "--group",
            "chunhui-xuefu",
            "--limit",
            "1",
            "--interval-seconds",
            "0",
            "--backfill",
        ]
    )

    assert args.func == cmd_collect_web
    assert args.group == "chunhui-xuefu"
    assert args.limit == 1
    assert args.backfill is True
