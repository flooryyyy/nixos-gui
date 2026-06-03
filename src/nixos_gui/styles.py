# theme presets - 2 styles with light/dark palettes
import logging

import hyperdiv as hd

logger = logging.getLogger(__name__)

PRESETS = [
    {
        "name": "NixOS Blue",
        "light": {
            "accent": "#5277c3",
            "accent2": "#7ebae4",
            "text": "#1a1a2e",
            "text_muted": "#6b7280",
            "bg_card": "#f9fafb",
            "bg_item": "#f3f4f6",
            "border": "#d1d5db",
            "danger": "#ef4444",
            "success": "#22c55e",
            "warning": "#f59e0b",
        },
        "dark": {
            "accent": "#5277c3",
            "accent2": "#7ebae4",
            "text": "#fafafa",
            "text_muted": "#9ca3af",
            "bg_card": "#2a2a35",
            "bg_item": "#32323e",
            "border": "#3e3e4a",
            "danger": "#ef4444",
            "success": "#22c55e",
            "warning": "#f59e0b",
        },
    },
    {
        "name": "Night Purple",
        "light": {
            "accent": "#8b5cf6",
            "accent2": "#a78bfa",
            "text": "#1a1a2e",
            "text_muted": "#6b7280",
            "bg_card": "#f9fafb",
            "bg_item": "#f3f4f6",
            "border": "#d1d5db",
            "danger": "#ef4444",
            "success": "#22c55e",
            "warning": "#f59e0b",
        },
        "dark": {
            "accent": "#8b5cf6",
            "accent2": "#a78bfa",
            "text": "#fafafa",
            "text_muted": "#9d8eb4",
            "bg_card": "#2a2a35",
            "bg_item": "#32323e",
            "border": "#3e3e4a",
            "danger": "#ef4444",
            "success": "#22c55e",
            "warning": "#f59e0b",
        },
    },
]

_current_idx = 1


@hd.global_state
class _ThemeVersionState(hd.BaseState):
    # reactive theme version counter, bumps when set_style() runs
    count = hd.Prop(hd.Int, 0)


def get_style() -> dict:
    preset = PRESETS[_current_idx]
    try:
        dark = hd.theme().is_dark
    except Exception:
        dark = True
    return preset["dark"] if dark else preset["light"]


def set_style(idx: int):
    global _current_idx
    if 0 <= idx < len(PRESETS):
        _current_idx = idx
        _ThemeVersionState().count += 1


def theme_version() -> int:
    return _ThemeVersionState().count


def current_index() -> int:
    return _current_idx


def card(s: dict, **kwargs):
    # card-styled box with sensible defaults
    defaults = {
        "background_color": s["bg_card"],
        "border": f"1px solid {s['border']}",
        "border_radius": "medium",
        "padding": (1, 1.5, 1, 1.5),
    }
    defaults.update(kwargs)
    return hd.box(**defaults)


def style_names() -> list[str]:
    return [p["name"] for p in PRESETS]


def intent_color(intent_type) -> str:
    s = get_style()
    mapping = {
        "add_package": s["success"],
        "remove_package": s["danger"],
        "set_option": s["accent"],
    }
    return mapping.get(
        intent_type.value if hasattr(intent_type, "value") else str(intent_type),
        s["text_muted"],
    )