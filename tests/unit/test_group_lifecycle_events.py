from data.plugins.astrbot_plugin_weave_memory.adapters.astrbot.group_lifecycle_events import (
    parse_group_lifecycle_event,
)


def test_kick_me_notice_means_group_left():
    raw = {"post_type": "notice", "notice_type": "group_decrease",
           "sub_type": "kick_me", "user_id": 10001, "group_id": 20002}
    assert parse_group_lifecycle_event(raw, 99999) == "group_left"


def test_self_leave_notice_means_group_left():
    raw = {"post_type": "notice", "notice_type": "group_decrease",
           "sub_type": "leave", "user_id": 99999, "group_id": 20002}
    assert parse_group_lifecycle_event(raw, "99999") == "group_left"


def test_other_member_leave_is_ignored():
    raw = {"post_type": "notice", "notice_type": "group_decrease",
           "sub_type": "leave", "user_id": 10001, "group_id": 20002}
    assert parse_group_lifecycle_event(raw, 99999) is None


def test_group_dismiss_notice_is_recognized():
    raw = {"post_type": "notice", "notice_type": "group_dismiss",
           "group_id": 20002}
    assert parse_group_lifecycle_event(raw, 99999) == "group_dismissed"


def test_non_notice_and_missing_payload_are_ignored():
    assert parse_group_lifecycle_event(None, 1) is None
    assert parse_group_lifecycle_event("text", 1) is None
    assert parse_group_lifecycle_event({"post_type": "message"}, 1) is None
    assert parse_group_lifecycle_event(
        {"post_type": "notice", "notice_type": "group_increase"}, 1,
    ) is None
