# home page - dashboard with system overview and pending changes
import logging
from urllib.parse import quote_plus

import hyperdiv as hd

from nixos_gui import apply, state, styles

logger = logging.getLogger(__name__)

# track which intent rows are expanded
# module-level so it survives re-renders; state.persist() forces the UI to refresh after toggle
_expanded_intents: set[str] = set()


def _fmt_val(v, max_len=60):
    # compact formatting for pending changes display
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return f'"{v}"' if len(v) <= max_len else f'"{v[:max_len]}…"'
    if isinstance(v, list):
        if len(v) <= 3:
            return "[" + ", ".join(_fmt_val(i) for i in v) + "]"
        return f"[{len(v)} items]"
    if isinstance(v, dict):
        return f"{{ {len(v)} keys }}"
    return str(v)[:max_len]


def _option_change_color(old, new):
    # picks a color based on what kind of change it is
    s = styles.get_style()
    if isinstance(old, bool) and isinstance(new, bool):
        return s["success"] if new and not old else s["danger"]
    if old is None and new is not None:
        return s["success"]
    if old is not None and new is None:
        return s["danger"]
    if isinstance(old, list) and isinstance(new, list):
        return s["success"] if len(new) > len(old) else s["danger"]
    return s["accent"]


def home():
    s, st = state.page_setup()
    from nixos_gui.state import get_config_reader
    reader = get_config_reader(st)
    intents = st.pending_intents
    count = len(intents)

    # header
    with hd.hbox(gap=1, align="center", wrap="wrap"):
        hd.text("Dashboard", font_weight="bold", font_size="x-large",
                font_color=s["text"])
        hd.badge(f"🖥 {reader.get_hostname()}",
                 background_color=s["accent"],
                 font_color="#ffffff",
                 border_radius="medium",
                 font_size="medium")

    hd.box(height=0.4)

    # bootstrap warning + auto-import
    imp = hd.state(import_done=False, import_msg="")
    bootstrap_msg = apply.check_bootstrap(config_reader=reader) if reader.is_nixos else None
    if bootstrap_msg or imp.import_msg:
        if imp.import_done:
            # green success card - click to dismiss
            with styles.card(s, background_color="#1a3d2e", border=f"1px solid {s['success']}"):
                with hd.hbox(gap=1, align="center"):
                    hd.text("✓", font_color=s["success"])
                    with hd.box(grow=1):
                        hd.text("Imported", font_weight="bold", font_color=s["text"])
                        hd.text(imp.import_msg, font_size="small", font_color=s["text_muted"])
                    if hd.button("✕", size="small", background_color=s["bg_item"],
                                 font_color=s["text_muted"]).clicked:
                        imp.import_msg = ""
        else:
            # yellow warning card
            with styles.card(s, background_color="#3d2e1a", border=f"1px solid {s['warning']}"):
                with hd.hbox(gap=1, align="center"):
                    hd.text("⚠", font_color=s["warning"])
                    with hd.box(grow=1):
                        hd.text("Setup needed", font_weight="bold",
                                font_color=s["text"])
                        hd.text("nixos-gui.nix is not imported yet.",
                                font_size="small", font_color=s["text_muted"])
                        if imp.import_msg:
                            hd.text(imp.import_msg, font_size="small", font_color=s["warning"])
                    if hd.button(
                        "Auto-import", size="small",
                        background_color=s["accent"],
                        font_color="#ffffff",
                    ).clicked:
                        result = apply.bootstrap_import(config_reader=reader)
                        imp.import_done = result.success
                        imp.import_msg = result.message
        hd.box(height=1)

    # quick search
    with hd.scope("quicksearch"):
        with hd.hbox(gap=1, align="center"):
            search_inp = hd.text_input(
                placeholder="Search packages...",
                grow=1,
            )
            search_btn = hd.button(
                "Search",
                background_color=s["accent"],
                font_color=s["text"],
                size="medium",
            )
        if search_btn.clicked:
            q = search_inp.value.strip() if search_inp.value else ""
            if q:
                hd.location().go(path="/search", query_args=f"q={quote_plus(q)}")
            else:
                hd.location().go(path="/search")

    hd.box(height=0.4)

    # pending changes
    with styles.card(s, padding=(0.8, 1.25, 1, 1.25)):
        with hd.hbox(gap=1.5, align="center"):
            hd.text("Pending Changes", font_weight="bold", font_color=s["text"])
            if count > 0:
                hd.badge(str(count), background_color=s["accent"],
                         font_color="#ffffff")
            hd.box(grow=1)
            if count > 0:
                if hd.button(
                    "Discard",
                    background_color=s["bg_item"],
                    font_color=s["text"],
                    size="medium",
                ).clicked:
                    st.clear_intents()
                    state.persist()
                if hd.button(
                    "Apply",
                    background_color=s["accent"],
                    font_color=s["text"],
                    size="medium",
                ).clicked:
                    # route to the apply page which writes to the correct path and runs nixos-rebuild
                    hd.location().go(path="/apply")

        if count == 0:
            hd.text("No pending changes", font_color=s["text_muted"])
        else:
            hd.box(height=0.7)

            for intent in intents:
                scope_key = f"intent-{intent.intent_type.value}-{intent.target}"
                with hd.scope(scope_key):
                    itype = intent.intent_type.value
                    is_expanded = scope_key in _expanded_intents

                    if itype == "add_package":
                        color = s["success"]
                        sign = "+"
                        label = intent.target
                    elif itype == "remove_package":
                        color = s["danger"]
                        sign = "-"
                        label = intent.target
                    elif itype == "set_option":
                        sign = "±"
                        old_val = reader._values_cache.get(intent.target)
                        color = _option_change_color(old_val, intent.value)
                        label = intent.target
                    else:
                        color = s["text_muted"]
                        sign = "?"
                        label = intent.target

                    with hd.box(margin_bottom=0.7):
                        with hd.hbox(gap=0.5, align="center"):
                            if hd.icon_button(
                                "x",
                                background_color=s["bg_item"],
                                font_color=s["text"],
                            ).clicked:
                                st.remove_intent(intent.intent_type, intent.target)
                                state.persist()

                            # all labels are plain text - identical look
                            hd.text(f"{sign} {label}", font_color=color)

                            # only set_option rows get a tiny expand arrow
                            if itype == "set_option":
                                arrow = "▾" if is_expanded else "▸"
                                if hd.button(
                                    arrow,
                                    background_color=s["bg_card"],
                                    border="none",
                                    font_color=s["text_muted"],
                                    padding=0,
                                    border_radius=0,
                                    font_size="small",
                                ).clicked:
                                    if is_expanded:
                                        _expanded_intents.discard(scope_key)
                                    else:
                                        _expanded_intents.add(scope_key)
                                    state.persist()

                        # expanded diff - only for set_option, unified git-diff style
                        if is_expanded and itype == "set_option":
                            with hd.box(padding=(0.5, 0, 0, 2.5), gap=0.15):
                                old_val = reader._values_cache.get(intent.target)
                                if isinstance(old_val, list) and isinstance(intent.value, list):
                                    old_set = set(map(str, old_val))
                                    new_set = set(map(str, intent.value))
                                    removed = old_set - new_set
                                    added = new_set - old_set
                                    # only show removed and added lines
                                    for i, item in enumerate(old_val):
                                        with hd.scope(f"old-{i}"):
                                            item_str = str(item)
                                            if item_str in removed:
                                                hd.text(f'- "{item_str}"',
                                                        font_color=s["danger"],
                                                        font_size="small",
                                                        font_family="mono")
                                    for i, item in enumerate(intent.value):
                                        with hd.scope(f"new-{i}"):
                                            item_str = str(item)
                                            if item_str in added:
                                                hd.text(f'+ "{item_str}"',
                                                        font_color=s["success"],
                                                        font_size="small",
                                                        font_family="mono")
                                elif old_val is not None and old_val != intent.value:
                                    hd.text(f"- {_fmt_val(old_val)}",
                                            font_color=s["danger"],
                                            font_size="small",
                                            font_family="mono")
                                    hd.text(f"+ {_fmt_val(intent.value)}",
                                            font_color=s["success"],
                                            font_size="small",
                                            font_family="mono")
                                else:
                                    hd.text(f"+ {_fmt_val(intent.value)}",
                                            font_color=s["success"],
                                            font_size="small",
                                            font_family="mono")