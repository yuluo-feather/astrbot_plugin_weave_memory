"""Parse aiocqhttp group lifecycle notices without importing AstrBot."""

from typing import Any


def parse_group_lifecycle_event(raw_message: Any, self_id: Any) -> str | None:
    """Return ``group_left`` / ``group_dismissed`` when the bot lost a group.

    Only events that mean the bot itself can no longer see the group qualify:
    ``group_decrease`` with ``sub_type=kick_me`` or ``user_id == self_id``.
    Ordinary member leaves and kicks are ignored.
    """
    if not isinstance(raw_message, dict):
        return None
    if raw_message.get("post_type") != "notice":
        return None
    notice_type = raw_message.get("notice_type")
    if notice_type == "group_decrease":
        if raw_message.get("sub_type") == "kick_me":
            return "group_left"
        user_id = str(raw_message.get("user_id") or "")
        if self_id is not None and user_id and user_id == str(self_id):
            return "group_left"
        return None
    if notice_type in {"group_dismiss", "group_disband", "group_dismissed"}:
        return "group_dismissed"
    return None
