"""Custom accent colour derivation and theme saving."""

import pytest

from agent_server import templating
from agent_server.routes import settings as settings_mod


def _lum(rgb):
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255.0


def _hex_to_rgb(value):
    h = value.lstrip("#")
    return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))


def test_theme_vars_clamps_text_accent_to_min_brightness():
    v = templating.theme_vars("#000000")
    text = _hex_to_rgb(v["accent"])
    btn = _hex_to_rgb(v["accent_btn"])
    assert _lum(text) >= 0.5, "text accent must stay readable even from black"
    assert _lum(btn) < _lum(text), "buttons stay darker than text"


def test_theme_vars_bright_colour_keeps_accent_and_dims_buttons():
    v = templating.theme_vars("#ffffff")
    assert v["accent"] == "#ffffff"
    assert v["accent_rgb"] == "255, 255, 255"
    assert _lum(_hex_to_rgb(v["accent_btn"])) < 1.0
    assert _lum(_hex_to_rgb(v["accent_dim"])) < _lum(_hex_to_rgb(v["accent_btn"]))


def test_theme_vars_dims_buttons_and_dim_below_text():
    v = templating.theme_vars("#8ea24f")
    text = _hex_to_rgb(v["accent"])
    btn = _hex_to_rgb(v["accent_btn"])
    dim = _hex_to_rgb(v["accent_dim"])
    assert v["accent"] == "#8ea24f"
    assert _lum(btn) < _lum(text)
    assert _lum(dim) < _lum(btn)


def _form_holder(form_dict):
    class _Form:
        def get(self, key, default=""):
            return form_dict.get(key, default)

    class _Request:
        async def form(self):
            return _Form()

    return _Request()


@pytest.mark.asyncio
async def test_save_theme_accepts_preset(monkeypatch):
    calls = {}

    async def fake_set(k, v):
        calls[k] = v

    monkeypatch.setattr(settings_mod.db, "set_setting", fake_set)
    monkeypatch.setattr(settings_mod, "set_theme", lambda v: calls.__setitem__("theme_seen", v))
    monkeypatch.setattr(settings_mod, "set_custom_color", lambda v: calls.__setitem__("custom_seen", v))

    resp = await settings_mod.save_theme(_form_holder({"theme": "blue"}))
    assert resp == {"ok": True}
    assert calls["theme"] == "blue"
    assert calls["theme_seen"] == "blue"


@pytest.mark.asyncio
async def test_save_theme_custom_requires_valid_hex(monkeypatch):
    calls = {}

    async def fake_set(k, v):
        calls[k] = v

    monkeypatch.setattr(settings_mod.db, "set_setting", fake_set)
    monkeypatch.setattr(settings_mod, "set_theme", lambda v: None)
    monkeypatch.setattr(settings_mod, "set_custom_color", lambda v: calls.__setitem__("custom", v))

    assert await settings_mod.save_theme(_form_holder({"theme": "custom", "custom": "#ff8800"})) == {"ok": True}
    assert calls["theme"] == "custom"
    assert calls["custom"] == "#ff8800"

    calls.clear()
    assert await settings_mod.save_theme(_form_holder({"theme": "custom", "custom": "not-a-hex"})) == {"ok": False}
    assert "theme" not in calls, "invalid custom must not write anything"


@pytest.mark.asyncio
async def test_save_theme_rejects_unknown(monkeypatch):
    calls = {}

    async def fake_set(k, v):
        calls[k] = v

    monkeypatch.setattr(settings_mod.db, "set_setting", fake_set)
    monkeypatch.setattr(settings_mod, "set_theme", lambda v: None)
    monkeypatch.setattr(settings_mod, "set_custom_color", lambda v: None)

    assert await settings_mod.save_theme(_form_holder({"theme": "purple"})) == {"ok": False}
    assert "theme" not in calls
