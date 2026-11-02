from pr_achieve.sources import Achievement


def test_achievement_fields():
    a = Achievement(
        game_id="sm64",
        game_name="Super Mario 64",
        achievement_name="Big Bob-omb on the Summit",
        points=10,
        description="Defeat Big Bob-omb",
        icon_url=None,
        source="native_hook",
    )
    assert a.game_id == "sm64"
    assert a.points == 10
    assert a.source == "native_hook"
