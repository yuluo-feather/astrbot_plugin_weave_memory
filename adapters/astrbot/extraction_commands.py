"""AstrBot command surface for V1.5 extraction consent and candidates."""

from __future__ import annotations

from typing import Any

from ...app.consent_service import (
    ConsentService,
    ExtractionConsentType,
)
from ...app.extraction_service import ExtractionService
from ...app.scope_service import PersonalScopeService
from ...app.sensitive_filter import KNOWN_CATEGORIES
from ...core.errors import AuthorizationDenied, NotFound, ValidationError
from .command_text import tail_after_command
from .identity import AstrBotIdentityAdapter

_ABILITY = {
    "capture": ExtractionConsentType.CAPTURE,
    "auto_extract": ExtractionConsentType.AUTO_EXTRACT,
    "external_model": ExtractionConsentType.EXTERNAL_MODEL,
    "store_candidate": ExtractionConsentType.STORE_CANDIDATE,
    "捕获": ExtractionConsentType.CAPTURE,
    "自动提取": ExtractionConsentType.AUTO_EXTRACT,
    "外部模型": ExtractionConsentType.EXTERNAL_MODEL,
    "保存候选": ExtractionConsentType.STORE_CANDIDATE,
}
_ABILITY_LABEL = {
    ExtractionConsentType.CAPTURE: "捕获",
    ExtractionConsentType.AUTO_EXTRACT: "自动提取",
    ExtractionConsentType.EXTERNAL_MODEL: "外部模型",
    ExtractionConsentType.STORE_CANDIDATE: "保存候选",
}
_ALL_ABILITIES = (
    ExtractionConsentType.CAPTURE,
    ExtractionConsentType.AUTO_EXTRACT,
    ExtractionConsentType.EXTERNAL_MODEL,
    ExtractionConsentType.STORE_CANDIDATE,
)
_CATEGORY_ALIAS = {
    "凭据": "credential",
    "密码": "credential",
    "证件": "id_number",
    "身份证": "id_number",
    "银行卡": "bank_card",
    "住址": "address",
    "地址": "address",
}

_USAGE = (
    "用法：/织忆 提取 开启 <capture|auto_extract|external_model|store_candidate|全部>\n"
    "/织忆 提取 关闭 <能力>\n"
    "/织忆 提取 敏感 <类别...>（清空表示不保存敏感类别）\n"
    "/织忆 提取 取消 <任务ID>\n"
    "/织忆 提取 状态\n"
    "/织忆 候选 列表\n"
    "/织忆 候选 确认 <候选ID>\n"
    "/织忆 候选 拒绝 <候选ID> [原因]"
)


class AstrBotExtractionCommands:
    def __init__(self, scopes: PersonalScopeService, consent: ConsentService,
                 extraction: ExtractionService) -> None:
        self._scopes = scopes
        self._consent = consent
        self._extraction = extraction
        self._identity = AstrBotIdentityAdapter()

    def _scope(self, event: Any):
        identity = self._identity.resolve_identity(event)
        scope = self._scopes.find(identity)
        if scope is None:
            raise NotFound("尚未建立个人记忆空间，请先发送 /织忆 授权")
        return scope

    def _operator(self, event: Any, scope) -> str:
        return self._identity.resolve_operator_id(event) or scope.identity.subject_id

    def _ensure_enabled(self, scope) -> None:
        if not self._scopes.is_enabled(scope):
            raise AuthorizationDenied("个人记忆空间已关闭（当前版本不支持重新开启）")

    async def handle_extract(self, event: Any) -> str:
        scope = self._scope(event)
        operator_id = self._operator(event, scope)
        parts = tail_after_command(event, "提取").split()
        if not parts:
            return _USAGE
        subcommand = parts[0]
        if subcommand == "状态":
            return await self._status(scope)
        if subcommand in {"开启", "关闭"}:
            if len(parts) < 2:
                return _USAGE
            return self._toggle(scope, operator_id, subcommand, parts[1])
        if subcommand == "敏感":
            return self._sensitive(scope, operator_id, parts[1:])
        if subcommand == "取消":
            if len(parts) != 2:
                return "用法：/织忆 提取 取消 <任务ID>"
            job = self._extraction.cancel_job(
                scope, parts[1], operator_id=operator_id,
            )
            return f"已取消提取任务 {job.id}，后续不会再调用模型。"
        return _USAGE

    def _toggle(self, scope, operator_id: str, action: str, ability: str) -> str:
        self._ensure_enabled(scope)
        if ability == "全部":
            if action == "关闭":
                return "关闭全部提取授权请逐项使用 /织忆 提取 关闭 <能力>。"
            granted: list[str] = []
            failed: list[str] = []
            for consent_type in _ALL_ABILITIES:
                try:
                    self._consent.grant(scope, consent_type, operator_id=operator_id)
                    granted.append(_ABILITY_LABEL[consent_type])
                except AuthorizationDenied:
                    failed.append(_ABILITY_LABEL[consent_type])
            text = f"已开启：{'、'.join(granted) if granted else '无'}。"
            if failed:
                text += f" 未开启：{'、'.join(failed)}。"
            return text + "\n外部模型未开启时不会执行自动提取。"
        consent_type = _ABILITY.get(ability)
        if consent_type is None:
            return _USAGE
        if action == "开启":
            self._consent.grant(scope, consent_type, operator_id=operator_id)
            return f"已开启提取能力：{_ABILITY_LABEL[consent_type]}。"
        self._consent.revoke(scope, consent_type, operator_id=operator_id)
        return f"已关闭提取能力：{_ABILITY_LABEL[consent_type]}。"

    def _sensitive(self, scope, operator_id: str, raw_categories: list[str]) -> str:
        self._ensure_enabled(scope)
        if not raw_categories or raw_categories == ["清空"]:
            categories: list[str] = []
        else:
            categories = []
            for raw in raw_categories:
                for token in raw.replace("，", ",").split(","):
                    category = _CATEGORY_ALIAS.get(token, token).strip()
                    if not category:
                        continue
                    if category not in KNOWN_CATEGORIES:
                        raise ValidationError(
                            f"未知敏感类别：{token}；可用：{', '.join(KNOWN_CATEGORIES)}"
                        )
                    if category not in categories:
                        categories.append(category)
        self._consent.grant(
            scope, ExtractionConsentType.SENSITIVE_CATEGORIES,
            operator_id=operator_id, metadata={"categories": categories},
        )
        if not categories:
            return "已清空敏感类别白名单；敏感内容不会进入候选。"
        return f"已允许敏感类别进入候选：{'、'.join(categories)}；仍需手动确认候选。"

    async def _status(self, scope) -> str:
        gates = self._extraction.gates(scope)
        counts = self._extraction.job_summary(scope)
        _, candidate_total = await self._extraction.list_candidates(scope, limit=1)
        def state(value: bool) -> str:
            return "开" if value else "关"
        lines = [
            f"提取总开关：{state(gates.global_enabled)}",
            f"捕获：{state(gates.capture_consent)}；自动提取：{state(gates.auto_extract_consent)}；"
            f"外部模型：{state(gates.external_model_consent)}；保存候选：{state(gates.store_candidate_consent)}",
            f"敏感白名单：{'、'.join(gates.sensitive_categories) if gates.sensitive_categories else '无'}",
            "当前有效：捕获={}；提取={}；保存候选={}".format(
                state(gates.can_capture), state(gates.can_extract), state(gates.can_store),
            ),
        ]
        missing = gates.missing()
        if missing:
            lines.append(f"缺失条件：{'、'.join(missing)}")
        job_text = "，".join(
            f"{key}={value}" for key, value in counts.items() if value
        ) or "无任务"
        lines.append(f"提取任务：{job_text}")
        lines.append(f"待确认候选：{candidate_total} 条")
        return "\n".join(lines)

    async def handle_candidate(self, event: Any) -> str:
        scope = self._scope(event)
        parts = tail_after_command(event, "候选").split()
        if not parts:
            return _USAGE
        subcommand = parts[0]
        if subcommand == "列表":
            return await self._list_candidates(scope)
        if subcommand in {"确认", "拒绝"}:
            if len(parts) < 2:
                return _USAGE
            candidate_id = parts[1]
            candidate = self._extraction.get_candidate(scope, candidate_id)
            operator_id = self._operator(event, scope)
            if subcommand == "确认":
                await self._extraction.confirm_candidate(
                    scope, candidate_id,
                    expected_revision=candidate.current_revision,
                    operator_id=operator_id,
                )
                return f"已确认候选 {candidate_id}，现在会参与检索与注入。"
            reason = " ".join(parts[2:]).strip()
            await self._extraction.reject_candidate(
                scope, candidate_id,
                expected_revision=candidate.current_revision, reason=reason,
                operator_id=operator_id,
            )
            return f"已拒绝候选 {candidate_id}。"
        return _USAGE

    async def _list_candidates(self, scope) -> str:
        candidates, total = await self._extraction.list_candidates(scope, limit=20)
        if total == 0:
            return "当前没有待确认的提取候选。"
        lines = [f"待确认候选：{total} 条（显示 {len(candidates)} 条）"]
        for candidate in candidates:
            lines.append(
                f"{candidate.id} · {candidate.memory_type.value} · "
                f"rev {candidate.current_revision} · {candidate.content}"
            )
        return "\n".join(lines)
