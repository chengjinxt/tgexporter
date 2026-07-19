from __future__ import annotations

import os
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
class SourceConfig:
    profile_dir: Path


@dataclass(frozen=True)
class WebSourcesConfig:
    default_group: str = "chunhui-xuefu"
    interval_seconds: int = 3600
    max_articles_per_run: int = 1


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    output: OutputConfig
    wechat: WechatConfig
    source: SourceConfig
    web_sources: WebSourcesConfig


def load_config(root: Path | None = None, config_path: Path | None = None) -> Config:
    root = (root or Path.cwd()).resolve()
    env = _read_env(root / ".env")
    data = _read_toml(config_path or root / "config.local.toml")

    telegram = data.get("telegram", {})
    output = data.get("output", {})
    wechat = data.get("wechat", {})
    source = data.get("source", {})
    web_sources = data.get("web_sources", {})

    bot_token = _pick(env, "TG_BOT_TOKEN", telegram.get("bot_token", ""))
    channel = _pick(env, "TG_CHANNEL", telegram.get("channel", "TechnologyNewsSyncAssistant"))
    timeout = int(_pick(env, "TG_POLL_TIMEOUT_SECONDS", telegram.get("poll_timeout_seconds", 30)))
    proxy_url = _pick(env, "TELEGRAM_PROXY_URL", telegram.get("proxy_url", "")) or None
    output_base = _pick(env, "OUTPUT_BASE_DIR", output.get("base_dir", "发布内容"))
    timezone = _pick(env, "OUTPUT_TIMEZONE", output.get("timezone", "Asia/Shanghai"))
    profile_dir = _pick(env, "WECHAT_PROFILE_DIR", wechat.get("profile_dir", "runtime/wechat-profile"))
    default_mode = _pick(env, "WECHAT_DEFAULT_MODE", wechat.get("default_mode", "draft"))
    author = _pick(env, "WECHAT_AUTHOR", wechat.get("author", "程锦学堂"))
    source_profile_dir = _pick(env, "SOURCE_PROFILE_DIR", source.get("profile_dir", "runtime/source-profile"))
    web_source_group = _pick(env, "WEB_SOURCE_GROUP", web_sources.get("default_group", "chunhui-xuefu"))
    web_source_interval = int(_pick(env, "WEB_SOURCE_INTERVAL_SECONDS", web_sources.get("interval_seconds", 3600)))
    web_source_limit = int(_pick(env, "WEB_SOURCE_LIMIT", web_sources.get("max_articles_per_run", 1)))

    return Config(
        telegram=TelegramConfig(
            bot_token=bot_token,
            channel=channel,
            poll_timeout_seconds=timeout,
            proxy_url=proxy_url,
        ),
        output=OutputConfig(base_dir=_resolve(root, output_base), timezone=timezone),
        wechat=WechatConfig(
            profile_dir=_resolve(root, profile_dir),
            default_mode=default_mode,
            author=author,
        ),
        source=SourceConfig(profile_dir=_resolve(root, source_profile_dir)),
        web_sources=WebSourcesConfig(
            default_group=web_source_group,
            interval_seconds=web_source_interval,
            max_articles_per_run=web_source_limit,
        ),
    )


def require_bot_token(config: Config) -> None:
    if not config.telegram.bot_token:
        raise RuntimeError("Missing TG_BOT_TOKEN. Put it in .env or config.local.toml.")


def mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as file:
        return tomllib.load(file)


def _pick(env: dict[str, str], key: str, fallback):
    return os.environ.get(key) or env.get(key) or fallback


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path
