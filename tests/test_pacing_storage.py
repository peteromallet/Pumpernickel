import json

from app.models.user import (
    fetch_user_by_id,
    fetch_user_pacing_preferences,
    record_pacing_event,
    update_user_pacing_preferences,
    upsert_user,
)


async def test_pacing_preferences_are_bounded_and_stored(fake_pool) -> None:
    user = await upsert_user(fake_pool, "Maya", "15555550100", "UTC")

    preferences = await update_user_pacing_preferences(
        fake_pool,
        user.id,
        {
            "enabled": True,
            "burst_window_s": 999,
            "min_wait_s": 999,
            "max_wait_s": 2,
            "typing_grace_s": -10,
            "max_typing_wait_s": 999,
            "answer_typing_min_s": 999,
            "answer_typing_max_s": 3,
            "answer_chars_per_s": 999,
            "reactions_enabled": False,
            "reaction_daily_limit": 999,
        },
    )

    assert preferences["enabled"] is True
    assert preferences["burst_window_s"] == 2.0
    assert preferences["min_wait_s"] == 2.0
    assert preferences["max_wait_s"] == 2.0
    assert preferences["typing_grace_s"] == 0.5
    assert preferences["max_typing_wait_s"] == 90.0
    assert preferences["answer_typing_min_s"] == 3.0
    assert preferences["answer_typing_max_s"] == 3.0
    assert preferences["answer_chars_per_s"] == 80.0
    assert preferences["reactions_enabled"] is False
    assert preferences["reaction_daily_limit"] == 100

    fetched = await fetch_user_pacing_preferences(fake_pool, user.id)
    assert fetched == preferences

    fetched_user = await fetch_user_by_id(fake_pool, user.id)
    assert fetched_user.pacing_preferences == preferences


async def test_pacing_preferences_accept_json_string_from_driver(fake_pool) -> None:
    user = await upsert_user(fake_pool, "Maya", "15555550100", "UTC")
    fake_pool.users[user.id]["pacing_preferences"] = json.dumps({"max_wait_s": 4, "enabled": True})

    fetched = await fetch_user_pacing_preferences(fake_pool, user.id)
    fetched_user = await fetch_user_by_id(fake_pool, user.id)

    assert fetched["max_wait_s"] == 4.0
    assert fetched["enabled"] is True
    assert fetched_user.pacing_preferences == {"max_wait_s": 4, "enabled": True}


async def test_record_pacing_event_uses_fake_pool_durable_store(fake_pool) -> None:
    user = await upsert_user(fake_pool, "Maya", "15555550100", "UTC")

    event_id = await record_pacing_event(
        fake_pool,
        user_id=user.id,
        message_ids=[],
        source="live",
        decision="wait",
        reason="user is still composing",
        signal_snapshot={"typing": True},
        preference_snapshot={"max_wait_s": 12},
        wait_ms=1500,
    )

    row = fake_pool.pacing_events[event_id]
    assert row["user_id"] == user.id
    assert row["decision"] == "wait"
    assert row["reason"] == "user is still composing"
    assert row["signal_snapshot"] == {"typing": True}
    assert row["preference_snapshot"] == {"max_wait_s": 12}
    assert row["wait_ms"] == 1500
