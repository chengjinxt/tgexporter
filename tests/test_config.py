from tgexporter.config import load_config


def test_config_loads_movie4k_route_overrides(tmp_path):
    (tmp_path / "config.local.toml").write_text(
        """
[wechat_accounts.movie4k]
profile_dir = "runtime/custom-movie-profile"

[routes.movie4k]
output_subdir = "电影稿"
account = "movie4k"
cover_brand = "剪辑探索者"
batch_size = 2
match_usernames = ["dianying4K"]
match_titles = ["4K影视屋"]
""",
        encoding="utf-8",
    )

    config = load_config(tmp_path)
    route = config.routes["movie4k"]

    assert config.wechat_accounts["movie4k"].profile_dir == tmp_path / "runtime" / "custom-movie-profile"
    assert route.output_subdir == "电影稿"
    assert route.account == "movie4k"
    assert route.cover_brand == "剪辑探索者"
    assert route.batch_size == 2
    assert route.match_usernames == ("dianying4k",)
    assert route.match_titles == ("4K影视屋",)


def test_env_sources_do_not_override_config_local(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "system-env-token")
    monkeypatch.setenv("TG_CHANNEL", "SystemEnvChannel")
    (tmp_path / ".env").write_text(
        """
TG_BOT_TOKEN=env-file-token
TG_CHANNEL=EnvFileChannel
""",
        encoding="utf-8",
    )
    (tmp_path / "config.local.toml").write_text(
        """
[telegram]
bot_token = "toml-token"
channel = "TomlChannel"
""",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.telegram.bot_token == "toml-token"
    assert config.telegram.channel == "TomlChannel"
