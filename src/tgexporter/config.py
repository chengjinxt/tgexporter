from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    channel: str
    poll_timeout_seconds: int = 30
    proxy_url: str | None = None


@dataclass(frozen=True)
class OutputConfig:
    base_dir: Path
    timezone: str = "Asia/Shanghai"


@dataclass(frozen=True)
class WechatConfig:
    profile_dir: Path
    default_mode: str = "draft"
    author: str = "程锦学堂"


@dataclass(frozen=True)
class WechatAccountConfig:
    name: str
    profile_dir: Path


@dataclass(frozen=True)
class SourceConfig:
    profile_dir: Path


@dataclass(frozen=True)
class WebSourcesConfig:
    default_group: str = "chunhui-xuefu"
    interval_seconds: int = 3600
    max_articles_per_run: int = 1


@dataclass(frozen=True)
class ContentRouteConfig:
    name: str
    output_subdir: str = ""
    account: str = "default"
    cover_brand: str = "firemail 科技频道"
    batch_size: int = 8
    match_usernames: tuple[str, ...] = ()
    match_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    output: OutputConfig
    wechat: WechatConfig
    wechat_accounts: dict[str, WechatAccountConfig]
    routes: dict[str, ContentRouteConfig]
    source: SourceConfig
    web_sources: WebSourcesConfig


def default_wechat_accounts(root: Path, default_profile_dir: Path) -> dict[str, WechatAccountConfig]:
    return {
        "default": WechatAccountConfig(name="default", profile_dir=default_profile_dir),
        "movie4k": WechatAccountConfig(name="movie4k", profile_dir=root / "runtime/wechat-profile-movie4k"),
    }


def default_content_routes() -> dict[str, ContentRouteConfig]:
    return {
        "default": ContentRouteConfig(name="default"),
        "movie4k": ContentRouteConfig(
            name="movie4k",
            output_subdir="4K影视屋",
            account="movie4k",
            cover_brand="剪辑探索者",
            batch_size=2,
            match_usernames=("dianying4k",),
            match_titles=("4K影视屋",),
        ),
    }


def load_config(root: Path | None = None, config_path: Path | None = None) -> Config:
    root = (root or Path.cwd()).resolve()
    data = _read_toml(config_path or root / "config.local.toml")

    telegram = data.get("telegram", {})
    output = data.get("output", {})
    wechat = data.get("wechat", {})
    source = data.get("source", {})
    web_sources = data.get("web_sources", {})
    wechat_accounts_data = data.get("wechat_accounts", {})
    routes_data = data.get("routes", {})

    bot_token = telegram.get("bot_token", "")
    channel = telegram.get("channel", "TechnologyNewsSyncAssistant")
    timeout = int(telegram.get("poll_timeout_seconds", 30))
    proxy_url = telegram.get("proxy_url", "") or None
    output_base = output.get("base_dir", "发布内容")
    timezone = output.get("timezone", "Asia/Shanghai")
    profile_dir = wechat.get("profile_dir", "runtime/wechat-profile")
    default_mode = wechat.get("default_mode", "draft")
    author = wechat.get("author", "程锦学堂")
    source_profile_dir = source.get("profile_dir", "runtime/source-profile")
    web_source_group = web_sources.get("default_group", "chunhui-xuefu")
    web_source_interval = int(web_sources.get("interval_seconds", 3600))
    web_source_limit = int(web_sources.get("max_articles_per_run", 1))
    resolved_profile_dir = _resolve(root, profile_dir)
    wechat_accounts = build_wechat_accounts(root, resolved_profile_dir, wechat_accounts_data)
    routes = build_content_routes(routes_data)

    return Config(
        telegram=TelegramConfig(
            bot_token=bot_token,
            channel=channel,
            poll_timeout_seconds=timeout,
            proxy_url=proxy_url,
        ),
        output=OutputConfig(base_dir=_resolve(root, output_base), timezone=timezone),
        wechat=WechatConfig(
            profile_dir=resolved_profile_dir,
            default_mode=default_mode,
            author=author,
        ),
        wechat_accounts=wechat_accounts,
        routes=routes,
        source=SourceConfig(profile_dir=_resolve(root, source_profile_dir)),
        web_sources=WebSourcesConfig(
            default_group=web_source_group,
            interval_seconds=web_source_interval,
            max_articles_per_run=web_source_limit,
        ),
    )


def build_wechat_accounts(
    root: Path,
    default_profile_dir: Path,
    data: dict,
) -> dict[str, WechatAccountConfig]:
    accounts = default_wechat_accounts(root, default_profile_dir)
    for raw_name, raw_config in data.items():
        name = str(raw_name).strip() or "default"
        account_data = raw_config if isinstance(raw_config, dict) else {}
        profile_value = account_data.get("profile_dir", accounts.get(name, accounts["default"]).profile_dir)
        accounts[name] = WechatAccountConfig(name=name, profile_dir=_resolve(root, profile_value))
    return accounts


def build_content_routes(data: dict) -> dict[str, ContentRouteConfig]:
    routes = default_content_routes()
    for raw_name, raw_config in data.items():
        name = str(raw_name).strip() or "default"
        route_data = raw_config if isinstance(raw_config, dict) else {}
        base = routes.get(name, ContentRouteConfig(name=name))
        routes[name] = ContentRouteConfig(
            name=name,
            output_subdir=str(route_data.get("output_subdir", base.output_subdir)).strip(),
            account=str(route_data.get("account", base.account)).strip() or "default",
            cover_brand=str(route_data.get("cover_brand", base.cover_brand)).strip() or base.cover_brand,
            batch_size=normalize_batch_size(route_data.get("batch_size", base.batch_size)),
            match_usernames=normalize_usernames(route_data.get("match_usernames", base.match_usernames)),
            match_titles=normalize_strings(route_data.get("match_titles", base.match_titles)),
        )
    return routes


def normalize_batch_size(value) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = 8
    return max(1, min(8, size))


def normalize_usernames(value) -> tuple[str, ...]:
    return tuple(item.strip().removeprefix("@").lower() for item in normalize_strings(value) if item.strip())


def normalize_strings(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def require_bot_token(config: Config) -> None:
    if not config.telegram.bot_token:
        raise RuntimeError("Missing telegram.bot_token. Put it in config.local.toml under [telegram].")


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as file:
        return tomllib.load(file)



def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path
