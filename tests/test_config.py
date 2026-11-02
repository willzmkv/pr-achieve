from pathlib import Path

from pr_achieve.config import Config, OverlayPosition, Theme, default_config_path


def test_default_config():
    cfg = Config()
    assert cfg.position == OverlayPosition.TOP_RIGHT
    assert cfg.hold_seconds == 4.0
    assert cfg.theme.bg_color == "#1a1a2e"
    assert cfg.theme.accent_color == "#4ecdc4"


def test_config_from_dict():
    cfg = Config.from_dict({
        "position": "bottom_left",
        "hold_seconds": 6.0,
        "theme": {"bg_color": "#000000", "accent_color": "#ffffff"},
    })
    assert cfg.position == OverlayPosition.BOTTOM_LEFT
    assert cfg.hold_seconds == 6.0
    assert cfg.theme.bg_color == "#000000"


def test_from_dict_does_not_mutate_input():
    data = {"position": "top_left", "theme": {"bg_color": "#000000"}}
    Config.from_dict(data)
    assert data == {"position": "top_left", "theme": {"bg_color": "#000000"}}


def test_from_dict_ignores_unknown_keys():
    cfg = Config.from_dict({"hold_seconds": 2.0, "bogus": 1, "theme": {"nope": "x"}})
    assert cfg.hold_seconds == 2.0
    assert cfg.theme == Theme()


def test_load_toml_file(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        'position = "bottom_right"\n'
        "hold_seconds = 3\n"
        'ra_username = "player1"\n'
        "steam_app_ids = [620, 730]\n"
        "[theme]\n"
        'accent_color = "#f5a623"\n'
    )
    cfg = Config.load(path, environ={})
    assert cfg.position == OverlayPosition.BOTTOM_RIGHT
    assert cfg.hold_seconds == 3
    assert cfg.ra_username == "player1"
    assert cfg.steam_app_ids == [620, 730]
    assert cfg.theme.accent_color == "#f5a623"
    assert cfg.theme.bg_color == "#1a1a2e"


def test_load_missing_file_returns_defaults(tmp_path: Path):
    assert Config.load(tmp_path / "missing.toml", environ={}) == Config()


def test_load_invalid_file_falls_back_to_defaults(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('position = "middle"\n')
    assert Config.load(path, environ={}) == Config()
    path.write_text("not [valid toml")
    assert Config.load(path, environ={}) == Config()


def test_env_overrides_credentials(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('ra_username = "from_file"\nra_api_key = "file_key"\n')
    cfg = Config.load(path, environ={
        "PR_ACHIEVE_RA_USERNAME": "from_env",
        "PR_ACHIEVE_RA_API_KEY": "env_key",
        "PR_ACHIEVE_STEAM_API_KEY": "steam_key",
        "PR_ACHIEVE_STEAM_ID": "76561197960287930",
    })
    assert cfg.ra_username == "from_env"
    assert cfg.ra_api_key == "env_key"
    assert cfg.steam_api_key == "steam_key"
    assert cfg.steam_id == "76561197960287930"


def test_secrets_not_in_repr():
    cfg = Config(ra_api_key="SECRET-RA", steam_api_key="SECRET-STEAM")
    assert "SECRET" not in repr(cfg)


def test_default_config_path_honours_env():
    assert default_config_path({"PR_ACHIEVE_CONFIG": "/etc/x.toml"}) == Path("/etc/x.toml")
    assert default_config_path({"XDG_CONFIG_HOME": "/cfg"}) == Path("/cfg/pr-achieve/config.toml")
    assert default_config_path({}).parts[-2:] == ("pr-achieve", "config.toml")
