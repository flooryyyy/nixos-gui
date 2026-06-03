# application shell - sidebar, topbar, routing
import logging

import hyperdiv as hd

from nixos_gui import state, styles

logger = logging.getLogger(__name__)


def _pending_count() -> int:
    return len(state.get_state().pending_intents)

def _sidebar_link(template, icon_name: str, label: str, href: str):
    # active-state check matches both exact paths and sub-paths so /options/foo highlights the "Options" link too
    path = str(hd.location().path)
    active = path == href or path.startswith(href + "/")
    s = styles.get_style()

    with template.sidebar:
        with hd.link(
            href=href,
            direction="horizontal",
            align="center",
            gap=0.8,
            font_color=s["accent"] if active else s["text_muted"],
            font_weight="bold" if active else "normal",
            font_size="medium",
            hover_background_color=s["bg_item"],
            background_color=hd.lighten(s["accent"], 0.85) if active else None,
            border_radius="medium",
            padding=(0.6, 1, 0.6, 1),
            border_left=f"3px solid {s['accent']}" if active else None,
        ):
            hd.icon(icon_name)
            hd.text(label)


def main():
    state.get_state()

    template = hd.template(
        title="nixos-gui",
        sidebar=True,
        theme_switcher=True,
        # inline sidebar when window >= 1000px, drawer below
        # set explicitly so it's obvious why the sidebar sometimes collapses
        responsive_threshold=1000,
    )

    # sidebar
    _sidebar_link(template, "house", "Home", "/")
    _sidebar_link(template, "search", "Search", "/search")
    _sidebar_link(template, "sliders", "Options", "/options")

    with template.sidebar:
        hd.box(grow=1)
        hd.divider(spacing=0.5)
    _sidebar_link(template, "gear", "Settings", "/settings")

    # topbar - apply button
    with template.topbar_links:
        hd.box(width=1)
        with hd.hbox(gap=1, align="center"):
            count = _pending_count()
            s = styles.get_style()
            if hd.button(
                f"Apply ({count})" if count > 0 else "Apply",
                size="medium",
                background_color=s["accent"],
                font_color=s["text"],
            ).clicked:
                hd.location.path = "/apply"
        hd.box(width=1)

    # page routing
    with template.body:
        # reading these into `_` subscribes the page to reactive state changes (theme switches, intent count updates)
        _ = styles.theme_version()
        _ = state.intent_count_version()

        path = str(hd.location().path)
        if path in ("/", ""):
            from nixos_gui.pages.home import home
            home()
        elif path == "/search":
            from nixos_gui.pages.search import search
            search()
        elif path == "/options":
            from nixos_gui.pages.options import options
            options()
        elif path == "/apply":
            from nixos_gui.pages.apply import apply_page
            apply_page()
        elif path == "/settings":
            from nixos_gui.pages.settings import settings
            settings()
        else:
            s = styles.get_style()
            hd.text("Page not found", font_color=s["text_muted"])
