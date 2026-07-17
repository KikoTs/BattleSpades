"""Match-lobby rotation, team presentation, and end-screen config tests."""

from pathlib import Path

from server.config import load_config


def test_lobby_end_screen_duration_and_rotation_are_loaded(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[lobby]
map_rotation = ["London", "CastleWars", "london.vxl"]
end_screen_seconds = 27.5
""".strip(),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.map_rotation == ["London", "CastleWars"]
    assert config.end_screen_seconds == 27.5


def test_lobby_end_screen_duration_is_bounded(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[lobby]\nend_screen_seconds = 999\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.end_screen_seconds == 120.0


def test_shipped_team_colors_match_the_retail_palette():
    """Do not regress to pure RGB, which over-brightens HUD and team blocks."""

    project_root = Path(__file__).resolve().parents[1]
    config = load_config(project_root / "config.toml")

    assert config.team1_color == (44, 117, 179)
    assert config.team2_color == (137, 179, 44)
