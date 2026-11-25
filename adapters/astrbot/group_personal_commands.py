"""V2.4 group-personal consent command adapter."""

from typing import Any

from ...app.group_personal_service import GroupPersonalService
from ...app.scope_service import GroupScopeService
from ...core.errors import AuthorizationDenied
from .identity import AstrBotIdentityAdapter


class AstrBotGroupPersonalCommands:
    def __init__(
        self, service: GroupPersonalService, groups: GroupScopeService,
    ) -> None:
        self._service = service
        self._groups = groups
        self._identity = AstrBotIdentityAdapter()

    def execute(self, event: Any, action: str) -> str:
        group_identity = self._identity.resolve_group_identity(event)
        personal_identity = self._identity.resolve_personal_identity_for_group(event)
        operator_id = self._identity.resolve_group_operator_id(event)
        decision = self._groups.admin_decision(
            group_identity, operator_id, event,
        )
        normalized = action.strip()
        if normalized == "开启":
            self._service.set_group_enabled(
                group_identity, operator_id=operator_id,
                decision=decision, enabled=True,
            )
            return "已开启本群个人记忆共享。成员仍须各自发送 /织忆 群个人 授权；敏感记忆不会注入。"
        if normalized == "关闭":
            self._service.set_group_enabled(
                group_identity, operator_id=operator_id,
                decision=decision, enabled=False,
            )
            return "已关闭本群个人记忆共享，后续群聊不再注入任何成员的个人记忆；个人记忆数据不会删除。"
        if normalized == "授权":
            self._service.set_user_opt_in(
                group_identity, personal_identity,
                operator_id=operator_id, enabled=True,
            )
            return "已授权在本群使用你的非敏感个人记忆。你可以随时发送 /织忆 群个人 撤销。"
        if normalized in {"撤销", "关闭我的"}:
            self._service.set_user_opt_in(
                group_identity, personal_identity,
                operator_id=operator_id, enabled=False,
            )
            return "已撤销你在本群的个人记忆授权；其他群及你的个人记忆不受影响。"
        if normalized == "状态":
            state = self._service.status(group_identity, personal_identity)
            return (
                f"本群总开关：{'开启' if state['group_enabled'] else '关闭'}\n"
                f"你的个人记忆：{'已授权' if state['personal_memory_enabled'] else '未授权/已关闭'}\n"
                f"你在本群的共享授权：{'已授权' if state['user_opted_in'] else '未授权'}"
            )
        raise AuthorizationDenied("用法：/织忆 群个人 <开启|关闭|授权|撤销|状态>")
