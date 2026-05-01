from pathlib import Path


DOC = Path("docs/discord-pacing.md")
README = Path("README.md")
OPS = Path("docs/ops.md")

ENV_VARS = [
    "DISCORD_PACING_ENABLED",
    "DISCORD_PACING_BURST_WINDOW_S",
    "DISCORD_PACING_MIN_WAIT_S",
    "DISCORD_PACING_MAX_WAIT_S",
    "DISCORD_PACING_TYPING_GRACE_S",
    "DISCORD_PACING_TYPING_EXTEND_S",
    "DISCORD_PACING_MAX_TYPING_WAIT_S",
    "DISCORD_PACING_ANSWER_TYPING_MIN_S",
    "DISCORD_PACING_ANSWER_TYPING_MAX_S",
    "DISCORD_PACING_ANSWER_CHARS_PER_S",
    "DISCORD_PACING_REACTIONS_ENABLED",
    "DISCORD_PACING_REACTION_COOLDOWN_S",
    "DISCORD_PACING_REACTION_DAILY_LIMIT",
    "DISCORD_PACING_SILENCE_COOLDOWN_S",
    "DISCORD_PACING_LLM_JUDGEMENT_ENABLED",
    "DISCORD_PACING_LLM_MIN_AMBIGUITY",
    "DISCORD_PACING_EVENT_RETENTION_DAYS",
]

PREFERENCE_KEYS = [
    "enabled",
    "burst_window_s",
    "min_wait_s",
    "max_wait_s",
    "typing_grace_s",
    "max_typing_wait_s",
    "answer_typing_min_s",
    "answer_typing_max_s",
    "answer_chars_per_s",
    "reactions_enabled",
    "reaction_daily_limit",
]


def test_discord_pacing_docs_cover_operator_surface() -> None:
    text = DOC.read_text()

    for env_var in ENV_VARS:
        assert env_var in text
    for key in PREFERENCE_KEYS:
        assert key in text

    for term in [
        "TYPING_START",
        "GatewayCallbacks.on_event",
        "pacing_events",
        "llm_spend_log",
        "send_typing_indicator=false",
        "users.pacing_preferences",
    ]:
        assert term in text

    for action in ["wait", "react", "silence", "answer"]:
        assert f"`{action}`" in text
    for source in ["live", "catch_up", "media", "recovery"]:
        assert f"`{source}`" in text


def test_readme_and_ops_link_discord_pacing_docs() -> None:
    readme = README.read_text()
    ops = OPS.read_text()

    assert "migrations/0008_discord_pacing.sql" in readme
    assert "docs/discord-pacing.md" in readme
    assert "pacing_events" in readme
    assert "discord-pacing.md" in ops
    assert "users.pacing_preferences" in ops
