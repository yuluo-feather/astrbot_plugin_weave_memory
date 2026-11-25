"""V2.1 writable Settings: validated, allow-listed, never secrets.

The host gives a plugin its ``AstrBotConfig`` object; ``save_config()`` persists
to disk but does not hot-reload the plugin. Values are therefore written and
reported as "reload required" unless the running code reads them live.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..core.errors import AuthorizationDenied, ValidationError

# Keys whose change can weaken the safety boundary: administrator name lists,
# provider opt-in/allowlist, safety switches, retention/purge thresholds and
# backup windows. ADR-0031 requires an administrator plus a one-time nonce.
IMPORTANT_KEYS: frozenset[str] = frozenset({
    "command_admin_ids",
    "api_admin_usernames",
    "group_admin_ids",
    "external_provider_opt_in",
    "external_provider_allowlist",
    "ledger_first_enabled",
    "rejected_keep_body",
    "auto_enroll",
    "agent_write_enabled",
    "archive_retention_days",
    "operation_retention_days",
    "provider_call_audit_retention_days",
    "backup_interval_hours",
    "backup_keep_last",
    "backup_retention_days",
    "group_retention_days",
    "group_destroy_backup_retention_days",
    "recall_diagnostics_retention_days",
    "webui_enabled",
    "group_personal_enabled",
})

# Keys that must never be written through the WebUI even if named in a request.
SECRET_KEYS: frozenset[str] = frozenset({
    "provider_api_key",
    "embedding_api_key",
    "rerank_api_key",
})

# Values read live by the running plugin, so a save takes effect immediately.
HOT_APPLY_KEYS: frozenset[str] = frozenset({
    "webui_enabled",
    "group_personal_enabled",
})


class SettingsWriteService:
    def __init__(self, schema: Mapping[str, object], *,
                 writer: Callable[[dict[str, object]], None]) -> None:
        self._schema = dict(schema)
        self._writer = writer

    def describe(self) -> dict[str, object]:
        items = []
        for key, spec in self._schema.items():
            if not isinstance(spec, Mapping):
                continue
            items.append({
                "key": key,
                "type": spec.get("type", "string"),
                "default": spec.get("default"),
                "important": key in IMPORTANT_KEYS,
                "writable": key not in SECRET_KEYS,
                "hot_apply": key in HOT_APPLY_KEYS,
            })
        return {"items": items}

    def update(self, username: str, values: Mapping[str, object], *,
               current: Mapping[str, object],
               require_nonce: Callable[[str], None]) -> dict[str, object]:
        if not values:
            raise ValidationError("no settings were provided")
        prepared: dict[str, object] = {}
        important: list[str] = []
        for key, value in values.items():
            if key in SECRET_KEYS:
                raise AuthorizationDenied(f"{key} is environment-only")
            spec = self._schema.get(key)
            if not isinstance(spec, Mapping):
                raise ValidationError(f"unknown setting: {key}")
            prepared[key] = self._validate(key, spec, value)
            if key in IMPORTANT_KEYS:
                important.append(key)
        if important:
            require_nonce(",".join(sorted(important)))
        merged = dict(current)
        merged.update(prepared)
        self._writer(merged)
        return {
            "saved": sorted(prepared),
            "important": sorted(important),
            "reload_required": sorted(
                key for key in prepared if key not in HOT_APPLY_KEYS
            ),
            "actor": username,
        }

    @staticmethod
    def _validate(key: str, spec: Mapping[str, object], value: object) -> object:
        kind = spec.get("type", "string")
        if kind == "bool":
            if not isinstance(value, bool):
                raise ValidationError(f"{key} must be a boolean")
            return value
        if kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValidationError(f"{key} must be an integer")
            if value < 0:
                raise ValidationError(f"{key} must be non-negative")
            return value
        if kind == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"{key} must be a number")
            return float(value)
        if kind == "list":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValidationError(f"{key} must be a list of strings")
            return [v.strip() for v in value if v.strip()]
        if kind == "dict":
            if not isinstance(value, Mapping):
                raise ValidationError(f"{key} must be an object")
            return dict(value)
        if not isinstance(value, str):
            raise ValidationError(f"{key} must be a string")
        if len(value) > 512:
            raise ValidationError(f"{key} must be at most 512 characters")
        return value


def load_schema(path: str) -> dict[str, Any]:
    import json
    from pathlib import Path

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("plugin config schema must be a JSON object")
    return payload
