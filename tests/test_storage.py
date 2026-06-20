from app.storage import Storage


def test_repeated_config_migration_keeps_default_destination(tmp_path) -> None:
    storage = Storage(tmp_path / "state.sqlite3")

    storage.add_telegram_destination(
        "Main", "@main", "token", is_default=True, replace=False
    )
    storage.add_telegram_destination(
        "Main", "@main", "token", is_default=True, replace=False
    )

    assert storage.telegram_destination().chat_id == "@main"
    assert storage.telegram_destination().is_default


def test_deleted_config_destination_does_not_return(tmp_path) -> None:
    storage = Storage(tmp_path / "state.sqlite3")
    storage.add_telegram_destination("Main", "@main", "token", is_default=True)
    storage.add_telegram_destination("Other", "@other", "token")

    storage.delete_telegram_destination("@other")
    storage.add_telegram_destination("Other", "@other", "token", replace=False)

    assert [item.chat_id for item in storage.telegram_destinations()] == ["@main"]


def test_destinations_can_be_ordered_and_include_chat_type(tmp_path) -> None:
    storage = Storage(tmp_path / "state.sqlite3")
    storage.add_telegram_destination("Channel", "@channel", "token", is_default=True)
    storage.add_telegram_destination(
        "Chat", "-123", "token", destination_type="supergroup"
    )

    storage.move_telegram_destination("-123", -1)

    destinations = storage.telegram_destinations()
    assert [item.chat_id for item in destinations] == ["-123", "@channel"]
    assert destinations[0].destination_type == "supergroup"


def test_monitored_tiktok_channels_and_interval_are_persisted(tmp_path) -> None:
    storage = Storage(tmp_path / "state.sqlite3")
    storage.add_monitored_tiktok_channel("author")
    storage.set_setting("poll_interval_seconds", "120")

    assert storage.monitored_tiktok_channels() == ("author",)
    assert storage.setting("poll_interval_seconds", "300") == "120"

    storage.delete_monitored_tiktok_channel("author")
    assert storage.monitored_tiktok_channels() == ()


def test_admin_user_is_created_and_settings_are_user_scoped(tmp_path) -> None:
    storage = Storage(tmp_path / "state.sqlite3")

    admin = storage.get_user(1)
    assert admin is not None
    assert admin.username == "boyd"
    assert admin.is_admin
    assert admin.must_set_password

    user = storage.create_user("alice", "hash")
    storage.add_telegram_destination("Admin", "@admin", "token", user_id=admin.id)
    storage.add_telegram_destination("Alice", "@alice", "token", user_id=user.id)
    storage.add_monitored_tiktok_channel("admin_channel", user_id=admin.id)
    storage.add_monitored_tiktok_channel("alice_channel", user_id=user.id)

    assert [item.chat_id for item in storage.telegram_destinations(admin.id)] == ["@admin"]
    assert [item.chat_id for item in storage.telegram_destinations(user.id)] == ["@alice"]
    assert storage.monitored_tiktok_channels(admin.id) == ("admin_channel",)
    assert storage.monitored_tiktok_channels(user.id) == ("alice_channel",)
