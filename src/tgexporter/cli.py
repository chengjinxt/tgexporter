from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from .collector import TelegramCollector, is_transient_telegram_error, telegram_network_hint
from .config import load_config, mask_secret, require_bot_token
from .markdown_renderer import MarkdownRenderer
from .source_sites import source_rule_for_url
from .state import StateStore
from .telegram_bot import TelegramBotClient, TelegramBotError
from .web_capture import open_source_login_browser
from .web_sources import WebSourceCollector, load_web_source_group, run_web_source_loop
from .wechat_publisher import MAX_WECHAT_ARTICLES, WechatPublisher

MARKDOWN_ASSET_RE = re.compile(r"!?\[[^\]]*]\((?P<target>[^)]+)\)")
BATCH_DIR_RE = re.compile(r"^第(?P<index>\d+)批$")

def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Stopped.")
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tgexporter")
    parser.add_argument("--root", type=Path, default=default_root(), help="Project root directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check local config and Telegram bot token.")
    add_channel_argument(doctor)
    doctor.set_defaults(func=cmd_doctor)

    stats = subparsers.add_parser(
        "stats-domains",
        aliases=["stats-domainsstats-domains"],
        help="Show source domain statistics from rendered articles.",
    )
    stats.add_argument("--limit", type=int, default=50, help="Maximum number of domains to show.")
    stats.set_defaults(func=cmd_stats_domains)

    source_login = subparsers.add_parser(
        "source-login",
        help="Open a persistent browser for logging into source websites.",
    )
    source_login.add_argument("--url", required=True, help="Source website URL to open.")
    source_login.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Seconds to keep the login browser open. Use 0 to wait for Enter.",
    )
    source_login.set_defaults(func=cmd_source_login)

    collect_web = subparsers.add_parser(
        "collect-web",
        help="Collect public web education sources into Markdown articles.",
    )
    collect_web.add_argument(
        "--group",
        default=None,
        help="Web source group to collect. Defaults to WEB_SOURCE_GROUP or chunhui-xuefu.",
    )
    collect_web.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum new articles to render per run. Defaults to WEB_SOURCE_LIMIT or 1.",
    )
    collect_web.add_argument(
        "--interval-seconds",
        type=int,
        default=None,
        help="Run forever and collect every N seconds. Omit or set 0 to run once.",
    )
    collect_web.add_argument(
        "--backfill",
        action="store_true",
        help="Continue scanning older unprocessed source articles after the newest processed URL.",
    )
    collect_web.add_argument("--save-dir", type=Path, default=None, help="Base directory for rendered Markdown and media.")
    collect_web.set_defaults(func=cmd_collect_web)

    listen = subparsers.add_parser("listen", help="Listen for new Telegram channel posts.")
    add_channel_argument(listen)
    listen.add_argument("--once", action="store_true", help="Poll once and exit.")
    listen.add_argument("--latest-only", action="store_true", help="Process only the newest pending update.")
    listen.add_argument("--drop-pending", action="store_true", help="Mark current pending updates as consumed without rendering.")
    listen.add_argument("--timeout", type=int, default=None, help="Bot API long-poll timeout seconds.")
    listen.add_argument("--save-dir", type=Path, default=None, help="Base directory for rendered Markdown and media.")
    listen.set_defaults(func=cmd_listen)

    run = subparsers.add_parser("run", help="Listen and optionally open WeChat draft helper.")
    add_channel_argument(run)
    run.add_argument("--once", action="store_true", help="Poll once and exit.")
    run.add_argument("--latest-only", action="store_true", help="Process only the newest pending update.")
    run.add_argument("--drop-pending", action="store_true", help="Mark current pending updates as consumed without rendering.")
    run.add_argument("--draft", action="store_true", help="Open WeChat assisted draft flow for each rendered article.")
    run.add_argument("--timeout", type=int, default=None, help="Bot API long-poll timeout seconds.")
    run.add_argument("--save-dir", type=Path, default=None, help="Base directory for rendered Markdown and media.")
    run.set_defaults(func=cmd_run)

    publish = subparsers.add_parser("publish-wechat", help="Open WeChat assisted draft flow for Markdown articles.")
    publish_source = publish.add_mutually_exclusive_group(required=True)
    publish_source.add_argument(
        "--article",
        type=Path,
        nargs="+",
        help="Markdown article path(s). The first path is the main WeChat article; the rest are sub-articles.",
    )
    publish_source.add_argument(
        "--article-dir",
        type=Path,
        help="Directory containing Markdown articles. Direct child files are auto-filled in filename order and batched by 8.",
    )
    publish.add_argument("--auto-fill", action="store_true", help="Try filling WeChat editor automatically. Article directories use this by default.")
    publish.add_argument("--no-playwright", action="store_true", help="Use default browser instead of Playwright.")
    publish.add_argument("--headless", action="store_true", help="Run Playwright headless.")
    publish.add_argument("--login-timeout", type=int, default=180, help="Seconds to wait for WeChat browser login.")
    publish.add_argument("--review-timeout", type=int, default=0, help="Seconds to keep the browser open after auto-fill.")
    publish.set_defaults(func=cmd_publish_wechat)

    return parser


def add_channel_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--channel", default=None, help="Telegram channel username. Overrides TG_CHANNEL for this run.")


def default_root() -> Path:
    if not getattr(sys, "frozen", False):
        return Path.cwd()
    executable_dir = Path(sys.executable).resolve().parent
    candidates = [
        Path.cwd().resolve(),
        executable_dir,
        executable_dir.parent,
        executable_dir.parent.parent,
    ]
    for candidate in candidates:
        if any((candidate / name).exists() for name in (".env", "config.local.toml", "pyproject.toml")):
            return candidate
    return executable_dir


def cmd_doctor(args) -> int:
    config = load_config(args.root)
    channel = args.channel or config.telegram.channel
    require_bot_token(config)
    client = TelegramBotClient(config.telegram.bot_token, proxy_url=config.telegram.proxy_url)
    try:
        me = client.get_me()
    except TelegramBotError as exc:
        if is_transient_telegram_error(exc):
            print(telegram_network_hint(exc, config.telegram.proxy_url), file=sys.stderr)
        raise
    try:
        updates = client.get_updates(timeout=2, allowed_updates=["channel_post", "edited_channel_post"])
    except TelegramBotError as exc:
        if is_transient_telegram_error(exc):
            print("Bot API getUpdates short poll failed.", file=sys.stderr)
            print(telegram_network_hint(exc, config.telegram.proxy_url), file=sys.stderr)
        raise
    print(f"Bot: @{me.get('username')} ({me.get('first_name')})")
    print(f"Token: {mask_secret(config.telegram.bot_token)}")
    print(f"Channel: @{channel.lstrip('@')}")
    print(f"Proxy: {config.telegram.proxy_url or 'direct'}")
    print(f"Bot API getUpdates short poll: ok ({len(updates)} pending update(s) visible)")
    print(f"Output: {config.output.base_dir}")
    print(f"WeChat profile: {config.wechat.profile_dir}")
    return 0


def cmd_listen(args) -> int:
    collector, config = build_collector(args.root, channel_override=args.channel, save_dir=args.save_dir)
    timeout = args.timeout or config.telegram.poll_timeout_seconds
    if args.drop_pending:
        count = collector.drop_pending(timeout=1)
        print(f"Dropped {count} pending update(s).")
        return 0
    if args.once:
        paths = collector.poll_once(timeout=timeout, latest_only=args.latest_only)
        print_rendered(paths)
        return 0
    collector.listen_forever(timeout=timeout)
    return 0


def cmd_stats_domains(args) -> int:
    state = StateStore(args.root / "data" / "state.sqlite")
    rows = state.domain_stats(limit=args.limit)
    if not rows:
        print("No reference domains recorded yet.")
        return 0
    for domain, count in rows:
        rule = source_rule_for_url(f"https://{domain}/")
        if rule:
            print(
                f"{domain}\t{count}\t{rule.name}\t{rule.resource_method}"
                f"\t兜底：{rule.fallback_method}\t地址：{rule.link_display}"
                f"\t登录：{rule.login_requirement}"
            )
        else:
            print(f"{domain}\t{count}")
    return 0


def cmd_source_login(args) -> int:
    config = load_config(args.root)
    open_source_login_browser(args.url, config.source.profile_dir, timeout_seconds=args.timeout)
    return 0


def cmd_collect_web(args) -> int:
    config = load_config(args.root)
    group = args.group or config.web_sources.default_group
    limit = args.limit if args.limit is not None else config.web_sources.max_articles_per_run
    interval = args.interval_seconds if args.interval_seconds is not None else 0
    sources = load_web_source_group(group)
    state = StateStore(args.root / "data" / "state.sqlite")
    renderer = MarkdownRenderer((args.save_dir or config.output.base_dir).resolve())
    collector = WebSourceCollector(
        state=state,
        renderer=renderer,
        timezone=config.output.timezone,
        proxy_url=config.telegram.proxy_url,
    )
    run_web_source_loop(collector, sources=sources, limit=limit, interval_seconds=interval, backfill=args.backfill)
    return 0


def cmd_run(args) -> int:
    collector, config = build_collector(args.root, channel_override=args.channel, save_dir=args.save_dir)
    timeout = args.timeout or config.telegram.poll_timeout_seconds
    publisher = WechatPublisher(config.wechat.profile_dir) if args.draft else None
    if args.drop_pending:
        count = collector.drop_pending(timeout=1)
        print(f"Dropped {count} pending update(s).")
        return 0

    def on_article(path: Path) -> None:
        print(f"Rendered: {path}")
        if publisher:
            preview = publisher.open_assisted(path, use_playwright=True)
            print(f"WeChat preview: {preview}")

    if args.once:
        paths = collector.poll_once(timeout=timeout, latest_only=args.latest_only)
        for path in paths:
            on_article(path)
        if not paths:
            print("No new channel posts.")
        return 0
    collector.listen_forever(timeout=timeout, on_article=on_article)
    return 0


def cmd_publish_wechat(args) -> int:
    config = load_config(args.root)
    publisher = WechatPublisher(config.wechat.profile_dir)
    articles = resolve_publish_articles(args.article, args.article_dir)
    if args.article_dir is not None:
        previews = publish_article_dir_batches(
            publisher=publisher,
            article_dir=args.article_dir.resolve(),
            articles=articles,
            auto_fill=True,
            headless=args.headless,
            login_timeout_seconds=args.login_timeout,
            review_timeout_seconds=args.review_timeout,
        )
        for preview in previews:
            print(f"WeChat preview: {preview}")
        return 0
    if not args.auto_fill:
        preview = publisher.open_assisted(
            articles[0],
            use_playwright=not args.no_playwright,
            headless=args.headless,
        )
        print(f"WeChat preview: {preview}")
        return 0
    preview = publisher.try_auto_fill_many(
        articles,
        headless=args.headless,
        login_timeout_seconds=args.login_timeout,
        review_timeout_seconds=args.review_timeout,
    )
    print(f"WeChat preview: {preview}")
    return 0


def resolve_publish_articles(article_paths: list[Path] | None, article_dir: Path | None) -> list[Path]:
    if article_dir is not None:
        directory = article_dir.resolve()
        if not directory.exists():
            raise RuntimeError(f"Article directory does not exist: {directory}")
        if not directory.is_dir():
            raise RuntimeError(f"Article directory is not a directory: {directory}")
        articles = sorted(path.resolve() for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".md")
        if not articles:
            raise RuntimeError(f"No Markdown articles found in directory: {directory}")
        return articles
    return [article.resolve() for article in article_paths or []]


def publish_article_dir_batches(
    publisher: WechatPublisher,
    article_dir: Path,
    articles: list[Path],
    auto_fill: bool,
    headless: bool,
    login_timeout_seconds: int,
    review_timeout_seconds: int,
) -> list[Path]:
    previews: list[Path] = []
    batches = chunk_articles(articles, MAX_WECHAT_ARTICLES)
    should_archive = len(articles) > MAX_WECHAT_ARTICLES
    next_index = next_batch_index(article_dir)
    for offset, batch in enumerate(batches):
        batch_number = next_index + offset
        print(f"Publishing batch {offset + 1}/{len(batches)}: {len(batch)} article(s).")
        if auto_fill:
            preview = publisher.try_auto_fill_many(
                batch,
                headless=headless,
                login_timeout_seconds=login_timeout_seconds,
                review_timeout_seconds=review_timeout_seconds,
            )
        else:
            preview = publisher.open_assisted(batch[0], use_playwright=True, headless=headless)
        previews.append(preview)
        if should_archive:
            batch_dir = article_dir / f"第{batch_number}批"
            moved = move_published_batch(batch, batch_dir)
            print(f"Moved published batch to {batch_dir}: {len(moved)} file(s).")
    return previews


def chunk_articles(articles: list[Path], size: int = MAX_WECHAT_ARTICLES) -> list[list[Path]]:
    if size <= 0:
        raise ValueError("Batch size must be positive.")
    return [articles[index : index + size] for index in range(0, len(articles), size)]


def next_batch_index(article_dir: Path) -> int:
    indexes = []
    if article_dir.exists():
        for child in article_dir.iterdir():
            if not child.is_dir():
                continue
            match = BATCH_DIR_RE.match(child.name)
            if match:
                indexes.append(int(match.group("index")))
    return (max(indexes) + 1) if indexes else 1


def move_published_batch(article_paths: list[Path], batch_dir: Path) -> list[Path]:
    batch_dir.mkdir(parents=True, exist_ok=False)
    moved: list[Path] = []
    for article in article_paths:
        for path in [article, *collect_markdown_local_assets(article)]:
            if not path.exists() or not path.is_file():
                continue
            destination = batch_dir / path.name
            shutil.move(str(path), str(destination))
            moved.append(destination)
    return moved


def collect_markdown_local_assets(article_path: Path) -> list[Path]:
    base_dir = article_path.parent
    assets: list[Path] = []
    for match in MARKDOWN_ASSET_RE.finditer(article_path.read_text(encoding="utf-8")):
        target = normalize_markdown_asset_target(match.group("target"))
        if not target or is_external_asset_target(target):
            continue
        path = (base_dir / target).resolve()
        if path.is_file() and path not in assets:
            assets.append(path)
    return assets


def normalize_markdown_asset_target(target: str) -> str:
    value = target.strip()
    if not value:
        return ""
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")].strip()
    return value.strip('"').strip("'")


def is_external_asset_target(target: str) -> bool:
    lower = target.lower()
    return lower.startswith(("http://", "https://", "data:", "file://", "#"))


def build_collector(
    root: Path,
    channel_override: str | None = None,
    save_dir: Path | None = None,
) -> tuple[TelegramCollector, object]:
    config = load_config(root)
    require_bot_token(config)
    state = StateStore(root / "data" / "state.sqlite")
    renderer = MarkdownRenderer((save_dir or config.output.base_dir).resolve())
    client = TelegramBotClient(config.telegram.bot_token, proxy_url=config.telegram.proxy_url)
    collector = TelegramCollector(
        client=client,
        state=state,
        renderer=renderer,
        channel=channel_override or config.telegram.channel,
        timezone=config.output.timezone,
        proxy_url=config.telegram.proxy_url,
        source_profile_dir=config.source.profile_dir,
    )
    return collector, config


def print_rendered(paths: list[Path]) -> None:
    if not paths:
        print("No new channel posts.")
        return
    for path in paths:
        print(f"Rendered: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
