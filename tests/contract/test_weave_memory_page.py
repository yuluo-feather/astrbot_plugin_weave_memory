import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "pages" / "weave-memory"
SOURCE = ROOT / "webui" / "src"


def test_built_page_exists_with_relative_assets():
    entry = PAGE / "index.html"
    assert entry.is_file()
    html = entry.read_text(encoding="utf-8")
    assert "./assets/" in html
    assert 'src="/assets' not in html
    assert 'href="/assets' not in html
    assets = list((PAGE / "assets").iterdir())
    assert any(item.suffix == ".js" for item in assets)
    assert any(item.suffix == ".css" for item in assets)


def test_page_uses_host_bridge_and_does_not_touch_dashboard_storage():
    bridge = (SOURCE / "api" / "bridge.ts").read_text(encoding="utf-8")
    assert "window.AstrBotPluginView" in bridge
    for method in ("ready", "apiGet", "apiPost", "getContext", "onContext"):
        assert method in bridge
    for forbidden in ("localStorage", "sessionStorage", "document.cookie"):
        for path in SOURCE.rglob("*.ts"):
            assert forbidden not in path.read_text(encoding="utf-8"), path


def test_i18n_metadata_matches_page_name():
    payload = json.loads(
        (ROOT / ".astrbot-plugin" / "i18n" / "zh-CN.json").read_text(encoding="utf-8"),
    )
    assert payload["views"]["weave-memory"]["title"]


def test_webui_source_is_tracked_without_node_modules():
    package = json.loads((ROOT / "webui" / "package.json").read_text(encoding="utf-8"))
    assert package["dependencies"]["vuetify"]
    assert package["dependencies"]["vue"]
    assert (ROOT / "webui" / "vite.config.ts").is_file()
