import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).parents[2] / "_conf_schema.json"
SUPPORTED_TYPES = {
    "int", "float", "bool", "string", "text", "list", "file",
    "object", "template_list", "dict",
}


def test_config_schema_uses_astrbot_supported_types():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for key, spec in schema.items():
        assert spec["type"] in SUPPORTED_TYPES, key
        if spec["type"] == "object":
            assert "items" in spec, f"{key}: AstrBot object schema requires items"
