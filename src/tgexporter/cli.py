from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .collector import TelegramCollector
from .config import load_config, mask_secret, require_bot_token
from .markdown_renderer import MarkdownRenderer
from .state import StateStore
from .telegram_bot import TelegramBotClient
from .wechat_publisher import WechatPublisher


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
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project root directory.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check local config and Telegram bot token.")
    doctor.set_defaults(func=cmd_doctor)

    stats = subparsers.add_parser("stats-domains", help="Show source domain statistics from rendered articles.")
    stats.add_argument("--limit", type=int, default=50, help="Maximum number of domains to show.")
    stats.set_defaults(func=cmd_stats_domains)

    listen = subparsers.add_parser("listen", help="Listen for new Telegram channel posts.")
    listen.add_argument("--once", action="store_true", help="Poll once and exit.")
    listen.add_argument("--latest-only", action="store_true", help="Process only the newest pending update.")
    listen.add_argument("--drop-pending", action="store_true", help="Mark current pending updates as consumed without rendering.")
    listen.add_argument("--timeout", type=int, default=None, help="Bot API long-poll timeout seconds.")
    listen.set_defaults(func=cmd_listen)

    run = subparsers.add_parser("run", help="Listen and optionally open WeChat draft helper.")
    run.add_argument("--once", action="store_true", help="Poll once and exit.")
    run.add_argument("--latest-only", action="store_true", help="Process only the newest pending update.")
    run.add_argument("--drop-pending", action="store_true", help="Mark current pending updates as consumed without rendering.")
    run.add_argument("--draft", action="store_true", help="Open WeChat assisted draft flow for each rendered article.")
    run.add_argument("--timeout", type=int, default=None, help="Bot API long-poll timeout seconds.")
    run.set_defaults(func=cmd_run)

    publish = subparsers.add_parser("publish-wechat", help="Open WeChat assisted draft flow for Markdown articles.")
    publish.add_argument(
        "--article",
        type=Path,
        nargs="+",
        required=True,
        help="Markdown article path(s). The first path is the main WeChat article; the rest are sub-articles.",
    )
    publish.add_argument("--auto-fill", action="store_true", help="Try filling WeChat editor automatically.")
    publish.add_argument("--no-playwright", action="store_true", help="Use default browser instead of Playwright.")
    publish.add_argument("--headless", action="store_true", help="Run Playwright headless.")
    publish.add_argument("--login-timeout", type=int, default=180, help="Seconds to wait for WeChat browser login.")
    publish.add_argument("--review-timeout", type=int, default=0, help="Seconds to keep the browser open after auto-fill.")
    publish.set_defaults(func=cmd_publish_wechat)

    return parser


def cmd_doctor(args) -> int:
    config = load_config(args.root)
    require_bot_token(config)
    client = TelegramBotClient(config.telegram.bot_token, proxy_url=config.telegram.proxy_url)
    me = client.get_me()
    print(f"Bot: @{me.get('username')} ({me.get('first_name')})")
    print(f"Token: {mask_secret(config.telegram.bot_token)}")
    print(f"Channel: @{config.telegram.channel.lstrip('@')}")
    print(f"Proxy: {config.telegram.proxy_url or 'direct'}")
    print(f"Output: {config.output.base_dir}")
    print(f"WeChat profile: {config.wechat.profile_dir}")
    return 0


def cmd_listen(args) -> int:
    collector, config = build_collector(args.root)
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
        print(f"{domain}\t{count}")
    return 0


def cmd_run(args) -> int:
    collector, config = build_collector(args.root)
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
    articles = [article.resolve() for article in args.article]
    if args.auto_fill:
        preview = publisher.try_auto_fill_many(
            articles,
            headless=args.headless,
            login_timeout_seconds=args.login_timeout,
            review_timeout_seconds=args.review_timeout,
        )
    else:
        preview = publisher.open_assisted(
            articles[0],
            use_playwright=not args.no_playwright,
            headless=args.headless,
        )
    print(f"WeChat preview: {preview}")
    return 0


def build_collector(root: Path) -> tuple[TelegramCollector, object]:
    config = load_config(root)
    require_bot_token(config)
    state = StateStore(root / "data" / "state.sqlite")
    renderer = MarkdownRenderer(config.output.base_dir)
    client = TelegramBotClient(config.telegram.bot_token, proxy_url=config.telegram.proxy_url)
    collector = TelegramCollector(
        client=client,
        state=state,
        renderer=renderer,
        channel=config.telegram.channel,
        timezone=config.output.timezone,
        proxy_url=config.telegram.proxy_url,
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
