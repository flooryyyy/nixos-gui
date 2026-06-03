# search page - fuzzy package search with add/remove queuing
import logging
import shutil
from urllib.parse import quote_plus

import hyperdiv as hd

from nixos_gui import search as pkg_search
from nixos_gui import state, styles
from nixos_gui.state import Intent, IntentType

logger = logging.getLogger(__name__)


def _get_url_query() -> str:
    # reads q= param from URL
    try:
        raw = str(getattr(hd.location(), "query_args", ""))
        if raw and raw.startswith("q="):
            import urllib.parse
            return urllib.parse.unquote(raw[2:].split("&")[0])
    except Exception:
        pass
    return ""


def search():
    # package search page - renders results with add/remove/install-status buttons
    s, st, reader = state.page_setup(with_reader=True)

    inp_state = hd.state(text="", last_url_q="")

    url_q = _get_url_query()
    if url_q and inp_state.last_url_q != url_q:
        inp_state.text = url_q
        inp_state.last_url_q = url_q
        srch_query = url_q
        searched = True
    else:
        srch_query = inp_state.text
        searched = bool(inp_state.last_url_q)

    with hd.box(gap=1):
        with hd.scope("search-area"):
            with hd.hbox(gap=1, align="center"):
                inp = hd.text_input(
                    placeholder="Search nixpkgs packages...",
                    value=srch_query,
                    grow=1,
                )
                search_btn = hd.button(
                    "Search",
                    background_color=s["accent"],
                    font_color="#ffffff",
                    size="medium",
                )
                if hasattr(inp, "value"):
                    inp_state.text = inp.value

                if search_btn.clicked and str(inp_state.text).strip():
                    q = str(inp_state.text).strip()
                    hd.location().go(path="/search", query_args=f"q={quote_plus(q)}")
                    inp_state.last_url_q = f"q={quote_plus(q)}"
                    srch_query = q
                    searched = True

        if not searched:
            return

        loading_state = hd.state(loading=True, results=None, error="")
        try:
            loading_state.results = pkg_search.search_packages(
                srch_query, config_path=str(reader.config_path))
        except Exception as e:
            logger.error("Search failed: %s", e)
            loading_state.error = str(e)
            loading_state.results = []
        loading_state.loading = False

        if loading_state.loading:
            hd.spinner()
            return

        results = loading_state.results or []
        if not results:
            with styles.card(s, padding=(2, 2, 2, 2)):
                hd.text("No packages found", font_color=s["text_muted"])
            return

        # determine which packages are installed (system packages + module-enabled programs) to show correct button state
        current_pkgs_state = hd.state(pkgs=None, enabled_progs=None)
        if current_pkgs_state.pkgs is None:
            try:
                current_pkgs_state.pkgs = reader.get_packages()
            except Exception:
                current_pkgs_state.pkgs = []
        if current_pkgs_state.enabled_progs is None:
            try:
                current_pkgs_state.enabled_progs = reader.get_enabled_programs()
            except Exception:
                current_pkgs_state.enabled_progs = []
        current_pkgs = set(current_pkgs_state.pkgs or [])
        enabled_progs = set(current_pkgs_state.enabled_progs or [])
        all_installed = current_pkgs | enabled_progs

        pending_adds = {
            i.target for i in st.pending_intents
            if i.intent_type == IntentType.ADD_PACKAGE
        }
        pending_removes = {
            i.target for i in st.pending_intents
            if i.intent_type == IntentType.REMOVE_PACKAGE
        }

        # button priority - pending add → pending remove → installed → enabled program → in PATH → add
        with hd.box(gap=1):
            for r in results[:30]:
                with hd.scope(r.name):
                    with styles.card(s):
                        with hd.hbox(gap=1, align="center"):
                            with hd.box(grow=1):
                                hd.text(r.name, font_weight="bold",
                                        font_color=s["text"])
                                if r.description:
                                    hd.box(height=0.25)
                                    hd.text(r.description, font_size="small",
                                            font_color=s["text_muted"])
                                if r.version:
                                    hd.box(height=0.25)
                                    hd.badge(r.version,
                                             background_color=s["bg_item"],
                                             font_color=s["text_muted"])
                            hd.box(width=1)

                            if r.name in pending_adds:
                                if hd.button(
                                    "Remove",
                                    background_color=s["danger"],
                                    font_color="#ffffff",
                                    size="medium",
                                ).clicked:
                                    st.remove_intent(IntentType.ADD_PACKAGE, r.name)
                                    state.persist()
                            elif r.name in pending_removes:
                                hd.text("Will remove", font_color=s["danger"],
                                        font_size="small")
                            elif r.name in current_pkgs:
                                if hd.button(
                                    "Remove",
                                    background_color=s["danger"],
                                    font_color="#ffffff",
                                    size="medium",
                                ).clicked:
                                    st.add_intent(Intent(
                                        intent_type=IntentType.REMOVE_PACKAGE,
                                        target=r.name,
                                    ))
                                    state.persist()
                            elif r.name in enabled_progs:
                                if hd.button(
                                    "Disable",
                                    background_color=s["danger"],
                                    font_color="#ffffff",
                                    size="medium",
                                ).clicked:
                                    st.add_intent(Intent(
                                        intent_type=IntentType.SET_OPTION,
                                        target=f"programs.{r.name}.enable",
                                        value=False,
                                    ))
                                    state.persist()
                            elif shutil.which(r.name) is not None:
                                hd.text("Installed", font_color=s["text_muted"],
                                        font_size="small")
                            else:
                                if hd.button(
                                    "+ Add",
                                    background_color=s["accent"],
                                    font_color="#ffffff",
                                    size="medium",
                                ).clicked:
                                    st.add_intent(Intent(
                                        intent_type=IntentType.ADD_PACKAGE,
                                        target=r.name,
                                    ))
                                    state.persist()
