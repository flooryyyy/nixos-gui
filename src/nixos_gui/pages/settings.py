# settings page - theme, activation mode, config path, cache, bootstrap info
import logging

import hyperdiv as hd

from nixos_gui import apply, state, styles
from nixos_gui.search import invalidate_cache

logger = logging.getLogger(__name__)


def settings():
    s, st, reader = state.page_setup(with_reader=True)

    with hd.box(gap=1.5):
        hd.text("Settings", font_size="x-large", font_weight="bold",
                font_color=s["text"])
        hd.box(height=0.5)

        bootstrap_msg = apply.check_bootstrap(config_reader=reader) if reader.is_nixos else None
        if bootstrap_msg:
            # not yet imported - show instructions + auto-import
            with styles.card(s, padding=(1.25, 1.75, 1.25, 1.75)):
                hd.text("NixOS Integration", font_weight="bold", font_color=s["text"])
                hd.text("To use nixos-gui with your system, add this import "
                        "to your configuration:", font_color=s["text_muted"])
                with hd.box(
                    background_color=s["bg_item"],
                    padding=(0.75, 1, 0.75, 1),
                    border_radius="small",
                ):
                    hd.text(
                        "  ./nixos-gui.nix",
                        font_color=s["accent"],
                        font_size="small",
                    )
                hd.text("Place it in your imports list in configuration.nix or "
                        "flake.nix modules.", font_size="small",
                        font_color=s["text_muted"])
                hd.box(height=0.5)
                hd.text("nixos-gui.nix automatically imports generated.nix "
                        "which contains your GUI-configured settings.",
                        font_size="small", font_color=s["text_muted"])
                hd.box(height=0.5)
                imp = hd.state(import_done=False, import_msg="")
                if hd.button(
                    "Auto-import nixos-gui.nix",
                    background_color=s["accent"],
                    font_color="#ffffff",
                ).clicked:
                    result = apply.bootstrap_import(config_reader=reader)
                    imp.import_done = result.success
                    imp.import_msg = result.message
                if imp.import_msg:
                    color = s["success"] if imp.import_done else s["warning"]
                    hd.text(imp.import_msg, font_size="small", font_color=color)
        else:
            # already imported - show success status
            with styles.card(s, padding=(1.25, 1.75, 1.25, 1.75),
                             background_color="#1a3d2e", border=f"1px solid {s['success']}"):
                with hd.hbox(gap=1, align="center"):
                    hd.text("✓", font_color=s["success"])
                    with hd.box(grow=1):
                        hd.text("NixOS Integration active", font_weight="bold", font_color=s["text"])
                        hd.text("nixos-gui.nix is imported and syncing your settings.",
                                font_size="small", font_color=s["text_muted"])

        with styles.card(s, padding=(1.25, 1.75, 1.25, 1.75)):
            hd.text("Theme", font_weight="bold", font_color=s["text"])
            names = styles.style_names()
            with hd.hbox(gap=1):
                for idx, name in enumerate(names):
                    is_active = idx == styles.current_index()
                    with hd.scope(f"theme-{idx}"):
                        if hd.button(
                            name,
                            background_color=s["accent"] if is_active else s["bg_item"],
                            font_color="#ffffff" if is_active else s["text"],
                            size="medium",
                        ).clicked:
                            styles.set_style(idx)

        with styles.card(s, padding=(1.25, 1.75, 1.25, 1.75)):
            hd.text("Activation Mode", font_weight="bold", font_color=s["text"])
            hd.text("Controls nixos-rebuild behavior on Apply.",
                    font_color=s["text_muted"])
            modes = [
                ("switch", "Switch: build and activate"),
                ("test", "Test: build only, don't switch"),
                ("save", "Save: write config only, no rebuild"),
            ]
            with hd.hbox(gap=1):
                for mode, label in modes:
                    is_active = st.activation_mode == mode
                    with hd.scope(f"mode-{mode}"):
                        if hd.button(
                            mode.capitalize(),
                            background_color=s["accent"] if is_active else s["bg_item"],
                            font_color="#ffffff" if is_active else s["text"],
                            size="medium",
                        ).clicked:
                            st.activation_mode = mode
                            state.persist()

        with styles.card(s, padding=(1.25, 1.75, 1.25, 1.75)):
            hd.text("Config Path", font_weight="bold", font_color=s["text"])
            hd.text(f"Current: {reader.config_path}",
                    font_color=s["text_muted"])
            hd.text("Set via NIXOSGUI_CONFIG_PATH environment variable.",
                    font_size="small", font_color=s["text_muted"])

        with styles.card(s, padding=(1.25, 1.75, 1.25, 1.75)):
            hd.text("Cache", font_weight="bold", font_color=s["text"])
            hd.text("Clear package cache and config cache to force re-fetch.",
                    font_color=s["text_muted"])
            if hd.button(
                "Reset Cache",
                background_color=s["danger"],
                font_color="#ffffff",
                size="medium",
            ).clicked:
                invalidate_cache()
                reader.invalidate()
                hd.text("✓ Cache cleared", font_color=s["success"])
