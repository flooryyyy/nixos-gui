# data flow - page is fully reactive
# on every render - step 1 reads the cached options tree (loaded once on first visit, persisted to ~/.cache/nixos-gui/options-tree-v2.json keyed by nixpkgs revision)
# step 2 looks up current values for visible leaves in the reader's generation-keyed in-memory cache no nix call
# if a leaf isn't cached render "(loading)" and kick one hd.task() to fill the missing ones when the task finishes hyperdiv re-renders and the cache lookup returns new values
# we deliberately do NOT call reader.get_options_batch() during render as that function persists cache to disk on every miss which on a reactive loop becomes a noticeable perf cost
# reading the cache dict directly keeps the hot path zero-side-effect, only the background task touches disk
# for metadata - one on-demand task fetches both metadata and (if heavy) the value when the user clicks View
# a background meta warmup fills the cache so View clicks on visible options are instant
import json
import logging

import hyperdiv as hd

from nixos_gui import state, styles
from nixos_gui.config import CACHE_DIR, _FETCH_FAILED, _path_exists_in_tree
from nixos_gui.state import Intent, IntentType

logger = logging.getLogger(__name__)
TREE_CACHE_FILE = CACHE_DIR / "options-tree-v2.json"

# module-level caches survive across hyperdiv re-renders in the same process
_tree_cache: dict | None = None
_flat_cache: list[str] | None = None
# full_path -> metadata dict, populated by the background meta_warm_task
_meta_cache: dict[str, dict] = {}
# guard - track the last subtree/filter key we kicked off a meta warmup for
# hd.task() instances are recreated on every render, so we can't store this on the task itself
# without this guard, meta_warm_task re-fires on every re-render when meta fetches fail/times out, creating a subprocess storm
_fetch_target_path: str | None = None
_last_warmup_subtree_key: str | None = None
_last_warmup_filter_key: str | None = None



def _load_tree(reader) -> dict:
    # loads the full options tree
    # structure only - no values
    # branches = nested dicts, leaves = {type, description}
    # cached in memory and on disk so we only pay the ~30s nix eval once per nixpkgs revision
    global _tree_cache, _flat_cache
    if _tree_cache is not None:
        return _tree_cache
    if TREE_CACHE_FILE.exists():
        try:
            data = json.loads(TREE_CACHE_FILE.read_text())
            if isinstance(data, dict) and "_generation" in data:
                # generation-keyed cache format
                if data["_generation"] == reader._nixpkgs_key():
                    _tree_cache = data["tree"]
                    _flat_cache = None
                    return _tree_cache
        except (json.JSONDecodeError, OSError):
            pass
    logger.info("Fetching full options tree (this may take ~30s)...")
    try:
        import subprocess
        # recursive nix expression - leaves get type+description, branches recurse
        expr = (
            'let '
            '  process = set: builtins.mapAttrs (n: v: '
            '    if builtins.isAttrs v && v ? "type" then '
            '      { type = if builtins.hasAttr "name" v.type then v.type.name else "?"; '
            '        description = if builtins.hasAttr "description" v then v.description else ""; '
            '      } '
            '    else if builtins.isAttrs v then process v '
            '    else {} '
            '  ) set; '
            'in process (builtins.getFlake "' + str(reader.config_path)
            + '").nixosConfigurations.' + reader.config_name + '.options'
        )
        cmd = [
            "/run/current-system/sw/bin/nix", "eval",
            "--impure", "--json",
            "--expr", expr,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=300, cwd=str(reader.config_path))
        if result.returncode == 0 and result.stdout.strip():
            tree = json.loads(result.stdout)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            nixpkgs_key = reader._nixpkgs_key() or ""
            TREE_CACHE_FILE.write_text(json.dumps({
                "_generation": nixpkgs_key,
                "tree": tree,
            }))
            _tree_cache = tree
            _flat_cache = None
            return tree
        else:
            logger.warning("nix eval failed: %s", result.stderr[:300])
    except Exception as e:
        logger.warning("Could not load options tree: %s", e)
    _tree_cache = {}
    _flat_cache = None
    return {}


def _get_subtree(tree: dict, path: list[str]) -> dict | None:
    # gets the subtree at path from the in-memory tree
    node = tree
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _flatten(tree: dict, prefix: str = "") -> list[str]:
    # returns all leaf option paths as dot-separated strings
    paths = []
    for key, val in tree.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(val, dict) and "type" not in val:
            paths.extend(_flatten(val, full))
        else:
            paths.append(full)
    return paths


# these are plain functions invoked by hd.task.run()
# they receive whatever extra args we pass and return whatever hd.task.result should hold

def _fetch_values(reader, paths):
    # fetches values for the given paths and return {path - value}
    # uses the reader's persistent cache, so a hit returns instantly and a miss triggers a single chunked nix eval that persists the result
    # heavy paths are filtered out by get_options_batch (allow_heavy=False)
    return reader.get_options_batch(paths)


def _fetch_on_demand(reader, full_path):
    # fetches both metadata and value for a single option on demand
    # called when the user clicks View on an option that isn't fully cached
    # heavy paths get their meta and value fetched too (allow_heavy=True)
    # the result is cached to disk - next visit is instant
    # meta and value are fetched in parallel via threads so the total wait is max(meta_time, value_time) instead of meta_time + value_time
    from concurrent.futures import ThreadPoolExecutor

    result = {}
    is_heavy = reader.is_heavy(full_path)

    def _fetch_meta():
        try:
            meta = reader.get_option_meta(full_path, allow_heavy=is_heavy)
            if meta:
                result["meta"] = meta
        except Exception as e:
            logger.debug("On-demand meta fetch for %s failed: %s", full_path, e)

    def _fetch_value():
        try:
            values = reader.get_options_batch([full_path], allow_heavy=True)
            if full_path in values:
                result["value"] = values[full_path]
        except Exception as e:
            logger.debug("On-demand value fetch for %s failed: %s", full_path, e)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_fetch_meta)
        pool.submit(_fetch_value)
        pool.shutdown(wait=True)

    return result


def _warm_meta(reader, paths):
    # pre-fetches metadata for visible options in the background
    # runs alongside the values fetch so View clicks on visible options are instant (meta already on disk, no "Loading metadata..." flash)
    # filters stale paths that don't exist in the actual options tree - a missing attribute in a batch eval aborts the whole batch
    global _tree_cache
    if _tree_cache is not None:
        paths = [p for p in paths if _path_exists_in_tree(_tree_cache, p)]
    try:
        return reader.get_options_meta_batch(paths) or {}
    except Exception as e:
        logger.debug("Meta warmup failed: %s", e)
        return {}


# _path_exists_in_tree imported from nixos_gui.config
def _to_nix(value, indent=0) -> str:
    # formats a Python value as nix syntax for display
    # nixos literal expressions - the text field IS nix code, show as-is
    if (isinstance(value, dict)
            and value.get("_type") in ("literalExpression", "literalExample")
            and "text" in value):
        return str(value["text"])
    prefix = "  " * indent
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        if not value:
            return "[ ]"
        items = [_to_nix(v, indent + 1) for v in value]
        inner = "\n".join(f"{prefix}  {item}" for item in items)
        return f"[\n{inner}\n{prefix}]"
    if isinstance(value, dict):
        if not value:
            return "{ }"
        lines = []
        for k, v in value.items():
            key_str = k if k.isidentifier() else json.dumps(k)
            val_str = _to_nix(v, indent + 1)
            lines.append(f"{prefix}  {key_str} = {val_str};")
        inner = "\n".join(lines)
        return f"{{\n{inner}\n{prefix}}}"
    return str(value)


def _value_summary(value) -> str:
    # short, human-readable summary of a value for the tree row
    # truncation - strings cut at 120 chars (with ...) instead of 50, so the user can see meaningful content without expanding
    # full content is always available via the View button's expanded panel
    if value is None:
        return "(not set)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return (value[:120] + "…") if len(value) > 120 else value
    if isinstance(value, list):
        # show short lists inline; longer lists as item count
        if len(value) <= 5 and all(isinstance(v, (str, int, float, bool)) for v in value):
            items = ", ".join(_value_summary(v) for v in value)
            return f"[{items}]"
        return f"[{len(value)} items: click View to expand]"
    if isinstance(value, dict):
        # show small dicts inline; larger ones as count
        if len(value) <= 4 and all(isinstance(v, (str, int, float, bool)) for v in value.values()):
            items = ", ".join(f"{k} = {_value_summary(v)}" for k, v in value.items())
            return "{ " + items + " }"
        return f"{{ {len(value)} keys: click View to expand }}"
    return str(value)[:120] + ("…" if len(str(value)) > 120 else "")



# sentinel returned by _get_current_value when nothing is cached yet
_LOADING = object()
# sentinel for options whose nix eval failed or timed out
# the value is cached as _FETCH_FAILED in the reader; we translate it to this UI sentinel so the row shows
# "(unavailable)" instead of "(loading...)"
_FAILED = object()


def _get_current_value(pending, reader, full_path: str):
    # resolves the value for an option
    # order of precedence - step 1 pending intent (most recent user action wins over fetched value)
    # step 2 cached value (no nix call) returns the value, _FAILED if the eval failed/timed out, or _LOADING if nothing is available yet
    # heavy paths (boot.kernelPackages, disko.devices, etc.) are not batch-fetched in the background, so they'll be _LOADING until the user clicks View, which triggers an on-demand fetch
    # once fetched, the result is cached to disk and instant on next load
    intent = next(
        (i for i in pending
         if i.intent_type == IntentType.SET_OPTION
         and i.target == full_path),
        None
    )
    if intent is not None:
        return intent.value
    if full_path in reader._values_cache:
        cached = reader._values_cache[full_path]
        if cached == _FETCH_FAILED:
            return _FAILED
        return cached
    return _LOADING


def _render_option_row(
    s, st, reader, opt, fetch_task, values_task,
    pending, full_path: str, name: str, val,
):
    # renders a single option row card
    # works for both tree-view leaves and filter-view results
    # shows the path, type, value (or '(loading...)'), and a View button that opens the expanded detail panel with metadata + editor
    opt_type = val.get("type", "?") if isinstance(val, dict) else "?"
    desc = val.get("description", "") if isinstance(val, dict) else ""

    current = _get_current_value(pending, reader, full_path)
    is_loading = current is _LOADING
    is_failed = current is _FAILED

    with hd.scope(f"row-{full_path}"):
        with styles.card(s, padding=(0.75, 1, 0.75, 1)):
            with hd.hbox(gap=1, align="center"):
                hd.text("📄", font_size="small")
                with hd.box(grow=1):
                    with hd.hbox(gap=1.5, align="center"):
                        hd.text(full_path, font_weight="bold",
                                font_color=s["text"])
                        if is_loading:
                            hd.text("(loading…)", font_size="small",
                                    font_color=s["text_muted"])
                        elif is_failed:
                            hd.text("(unavailable)", font_size="small",
                                    font_color=s["text_muted"])
                        else:
                            hd.text(
                                _value_summary(current),
                                font_size="small",
                                font_color=s["text_muted"],
                            )
                    hd.text(
                        f"type: {opt_type}",
                        font_size="small",
                        font_color=s["text_muted"],
                    )
                is_expanded = opt.expanded == full_path
                btn_label = "Hide" if is_expanded else "View"
                if hd.button(
                    btn_label, size="small",
                    background_color=s["accent"],
                    font_color="#ffffff",
                ).clicked:
                    if is_expanded:
                        opt.expanded = ""
                    else:
                        opt.expanded = full_path
                        # fast path - use cached meta from disk (previous visit)
                        cached = reader.get_cached_meta(full_path)
                        if cached is not None:
                            _meta_cache[full_path] = cached
                        # on-demand fetch if meta or value missing
                        # also re-fetch if the value was previously cached as _FETCH_FAILED (timed out) - give it another shot
                        # always clear + restart even if a previous fetch is still running for a different option - the
                        # user explicitly clicked View on THIS option
                        needs_meta = full_path not in _meta_cache
                        cached_val = reader._values_cache.get(full_path)
                        needs_value = (cached_val is None
                                       or cached_val == _FETCH_FAILED)
                        if needs_meta or needs_value:
                            # clear stale _FETCH_FAILED so get_options_batch actually retries instead of returning the cached failure
                            if cached_val == _FETCH_FAILED:
                                del reader._values_cache[full_path]
                            global _fetch_target_path
                            _fetch_target_path = full_path
                            fetch_task.clear()
                            fetch_task.run(_fetch_on_demand, reader, full_path)

            # expanded detail panel
            if is_expanded:
                _render_detail_panel(
                    s, st, reader, full_path, opt_type, desc,
                    current, opt, fetch_task,
                )


def _render_detail_panel(s, st, reader, full_path, opt_type, desc, current, opt, fetch_task):
    # renders the expanded detail panel for one option
    # the panel always shows tree-cached data (description, type) immediately
    # meta fields (default, example, enum) load in asynchronously without blocking the whole panel behind a spinner
    hd.box(height=0.5)
    with hd.box(gap=0.75, padding=(0.75, 0, 0.75, 0)):
        # drain completed fetch_task into the meta cache - but only if the result is for THIS option
        # the fetch_task is shared, so a result from a previous option's fetch must not be attributed to the current one
        global _fetch_target_path
        if (full_path not in _meta_cache
                and fetch_task.done
                and _fetch_target_path == full_path):
            result = fetch_task.result
            if isinstance(result, dict) and "meta" in result:
                _meta_cache[full_path] = result["meta"]
        # if the task finished but the value was never cached (exception during fetch), mark it as failed so the UI
        # shows "(unavailable)" instead of spinning forever
        if (fetch_task.done
                and _fetch_target_path == full_path
                and full_path not in reader._values_cache):
            reader._values_cache[full_path] = _FETCH_FAILED
        meta = _meta_cache.get(full_path, {})
        meta_still_loading = (opt.expanded == full_path
                              and fetch_task.running
                              and not meta)

        # always show description from the tree - it's free, no fetch needed
        if desc:
            hd.text("Description:", font_weight="bold",
                    font_size="small", font_color=s["text"])
            hd.text(desc, font_size="small",
                    font_color=s["text_muted"])

        # meta fields (default, example, enum) - show if available, show a small "loading" hint if not, but don't block the panel
        if meta_still_loading:
            with hd.hbox(gap=0.5, align="center"):
                hd.spinner(track_width=0.3)
                hd.text("Loading defaults...", font_size="small",
                        font_color=s["text_muted"])
        else:
            enum = meta.get("enum")
            if enum:
                hd.text("Allowed values:", font_weight="bold",
                        font_size="small", font_color=s["text"])
                hd.text(str(enum), font_size="small",
                        font_color=s["text_muted"])
            default = meta.get("default")
            if default is not None:
                hd.text("Default:", font_weight="bold",
                        font_size="small", font_color=s["text"])
                hd.code(_to_nix(default), language="nix", font_size="small")
                if hd.button(
                    "Reset to default", size="small",
                    background_color=s["accent"],
                    font_color="#ffffff",
                ).clicked:
                    st.add_intent(Intent(
                        intent_type=IntentType.SET_OPTION,
                        target=full_path,
                        value=default,
                    ))
                    state.persist()
            example = meta.get("example")
            if example is not None:
                hd.text("Example:", font_weight="bold",
                        font_size="small", font_color=s["text"])
                hd.code(_to_nix(example), language="nix", font_size="small")

        # current value section
        # layout - label + Edit button on one line, then code/editor below
        # clicking Edit replaces the code block with an inline editor and shows Cancel next to the label
        is_heavy = reader.is_heavy(full_path)
        is_editing = opt.editing == full_path

        with hd.hbox(gap=0.5, align="center"):
            hd.text("Current value:", font_weight="bold",
                    font_size="small", font_color=s["text"])
            if current is _LOADING:
                hd.spinner(track_width=0.3)
            elif current is _FAILED and is_heavy:
                hd.text("(too large to evaluate)", font_size="small",
                        font_color=s["text_muted"])
            elif current is _FAILED:
                hd.text("(unavailable)", font_size="small",
                        font_color=s["text_muted"])
            hd.box(grow=1)
            if is_editing:
                if hd.button(
                    "Cancel", size="small",
                    background_color=s["bg_item"],
                    font_color=s["text"],
                ).clicked:
                    opt.editing = ""
            elif current is not _LOADING:
                if hd.button(
                    "Edit", size="small",
                    background_color=s["accent"],
                    font_color="#ffffff",
                ).clicked:
                    opt.editing = full_path

        # code block or editor - below the label, full width
        if current is _LOADING:
            hd.text("(loading…)", font_size="small",
                    font_color=s["text_muted"])
        elif is_editing:
            editor_value = None if current is _FAILED else current
            _render_value_editor(s, st, opt, full_path, opt_type, editor_value)
        elif current is not _FAILED:
            with hd.box(width="100%"):
                hd.code(_to_nix(current), language="nix", font_size="small")



def _render_value_editor(s, st, opt, full_path: str, opt_type: str, current):
    # renders the right input widget for an option type
    # wraps everything in a full-width vertical stack so the Set button sits cleanly below the editor with breathing room
    if opt_type == "bool":
        new_val = not current if isinstance(current, bool) else True
        label = "Disable" if current else "Enable"
        with hd.box(width="100%", gap=0.75):
            with hd.hbox(gap=0.5, align="center"):
                if hd.button(
                    label, size="small",
                    background_color=s["accent"],
                    font_color="#ffffff",
                ).clicked:
                    st.add_intent(Intent(
                        intent_type=IntentType.SET_OPTION,
                        target=full_path,
                        value=new_val,
                    ))
                    state.persist()
                    opt.editing = ""
        return

    if opt_type in ("str", "path", "package", "hostname"):
        with hd.box(width="100%", gap=0.75):
            with hd.scope(f"input-str-{full_path}"):
                inp = hd.text_input(
                    placeholder="Enter value...",
                    value=str(current) if current is not None else "",
                    width="100%",
                )
                if hd.button(
                    "Set", size="small",
                    background_color=s["accent"],
                    font_color="#ffffff",
                ).clicked:
                    st.add_intent(Intent(
                        intent_type=IntentType.SET_OPTION,
                        target=full_path,
                        value=str(inp.value),
                    ))
                    state.persist()
                    opt.editing = ""
        return

    if opt_type in ("int", "float"):
        with hd.box(width="100%", gap=0.75):
            with hd.scope(f"input-num-{full_path}"):
                inp = hd.text_input(
                    placeholder="Enter number...",
                    value=str(current) if isinstance(current, (int, float)) else "",
                    width="100%",
                )
                if hd.button(
                    "Set", size="small",
                    background_color=s["accent"],
                    font_color="#ffffff",
                ).clicked:
                    try:
                        parsed = int(str(inp.value)) if opt_type == "int" else float(str(inp.value))
                        st.add_intent(Intent(
                            intent_type=IntentType.SET_OPTION,
                            target=full_path,
                            value=parsed,
                        ))
                        state.persist()
                        opt.editing = ""
                    except (ValueError, TypeError):
                        hd.text("Invalid number", font_size="small",
                                font_color=s["danger"])
        return

    if opt_type == "list" or opt_type.startswith("list"):
        with hd.box(width="100%", gap=0.75):
            with hd.scope(f"input-list-{full_path}"):
                default_text = "\n".join(current) if isinstance(current, list) else ""
                inp = hd.textarea(
                    placeholder="One item per line...",
                    value=default_text,
                    width="100%",
                    rows=15,
                )
                if hd.button(
                    "Set", size="small",
                    background_color=s["accent"],
                    font_color="#ffffff",
                ).clicked:
                    items = [line.strip() for line in str(inp.value).split("\n") if line.strip()]
                    st.add_intent(Intent(
                        intent_type=IntentType.SET_OPTION,
                        target=full_path,
                        value=items,
                    ))
                    state.persist()
                    opt.editing = ""
        return

    if opt_type in ("attrs", "attributeSet") or opt_type.startswith("attrs") or opt_type == "submodule":
        with hd.box(width="100%", gap=0.75):
            with hd.scope(f"input-attrs-{full_path}"):
                default_text = json.dumps(current, indent=2) if isinstance(current, dict) else ""
                inp = hd.textarea(
                    placeholder="JSON object...",
                    value=default_text,
                    width="100%",
                    rows=15,
                )
                if hd.button(
                    "Set", size="small",
                    background_color=s["accent"],
                    font_color="#ffffff",
                ).clicked:
                    try:
                        parsed = json.loads(str(inp.value).strip()) if str(inp.value).strip() else {}
                        st.add_intent(Intent(
                            intent_type=IntentType.SET_OPTION,
                            target=full_path,
                            value=parsed,
                        ))
                        state.persist()
                        opt.editing = ""
                    except json.JSONDecodeError:
                        hd.text("Invalid JSON", font_size="small",
                                font_color=s["danger"])
        return

    # fallback - JSON textarea for any unrecognized type (nullOr, enum, anything compound we don't have a dedicated widget for)
    # ensures the user can always edit something rather than seeing no editor at all
    with hd.box(width="100%", gap=0.75):
        with hd.scope(f"input-json-{full_path}"):
            default_text = json.dumps(current, indent=2) if current is not None else ""
            inp = hd.textarea(
                placeholder="JSON value...",
                value=default_text,
                width="100%",
                rows=15,
            )
            if hd.button(
                "Set", size="small",
                background_color=s["accent"],
                font_color="#ffffff",
            ).clicked:
                raw_val = str(inp.value).strip()
                if not raw_val:
                    st.add_intent(Intent(
                        intent_type=IntentType.SET_OPTION,
                        target=full_path,
                        value=None,
                    ))
                    state.persist()
                    opt.editing = ""
                else:
                    try:
                        parsed = json.loads(raw_val)
                        st.add_intent(Intent(
                            intent_type=IntentType.SET_OPTION,
                            target=full_path,
                            value=parsed,
                        ))
                        state.persist()
                        opt.editing = ""
                    except json.JSONDecodeError:
                        hd.text("Invalid JSON", font_size="small",
                                font_color=s["danger"])



def _render_not_nixos(s):
    # renders the 'not a nixos system' message
    with hd.box(gap=2):
        hd.text("Options", font_size="x-large", font_weight="bold",
                font_color=s["text"])
        hd.box(height=1)
        hd.text("Options browser requires a NixOS system.",
                font_color=s["text_muted"])


def _render_loading_tree(s):
    # renders the loading spinner while the options tree is fetched
    with hd.box(gap=2):
        hd.text("Options", font_size="x-large", font_weight="bold",
                font_color=s["text"])
        hd.spinner()
        hd.text("Loading options tree...", font_color=s["text_muted"])


def _render_breadcrumb(s, opt, filter_guard):
    # renders the breadcrumb navigation and page heading
    with hd.hbox(gap=1, align="center"):
        with hd.scope("breadcrumb"):
            with hd.hbox(gap=0.75, align="center"):
                root_btn = hd.button(
                    "⚙", size="small",
                    background_color=s["bg_item"],
                    font_color=s["text"],
                )
                if root_btn.clicked:
                    opt.path = []
                    opt.filter_text = ""
                    filter_guard.skip = 10
                for i, part in enumerate(opt.path):
                    with hd.scope(f"crumb-{i}"):
                        hd.text("/", font_color=s["text_muted"],
                                font_size="small")
                        if hd.button(
                            part, size="small",
                            background_color=s["bg_item"],
                            font_color=s["text"],
                        ).clicked:
                            opt.path = opt.path[:i + 1]
                            opt.filter_text = ""
                            filter_guard.skip = 10
        hd.text("NixOS Options", font_size="x-large", font_weight="bold",
                font_color=s["text"])


def _render_filter(s, opt, filter_guard):
    # renders the filter input area
    with hd.scope("filter-area"):
        with hd.hbox(gap=1.5, align="center"):
            ft = hd.text_input(
                placeholder="Filter options...",
                value=opt.filter_text,
                width="100%",
            )
            if hasattr(ft, "value"):
                if filter_guard.skip > 0:
                    filter_guard.skip -= 1
                else:
                    opt.filter_text = ft.value
            if hd.button(
                "✕", size="small",
                background_color=s["bg_item"],
                font_color=s["text_muted"],
            ).clicked:
                opt.filter_text = ""
                opt.path = []
                filter_guard.skip = 10


def _render_filter_view(s, st, reader, opt, tree, pending, values_task, fetch_task, meta_warm_task):
    # renders the flat filtered view of options
    global _flat_cache, _last_warmup_filter_key, _last_warmup_subtree_key
    if _flat_cache is None:
        _flat_cache = _flatten(tree)
    all_paths = _flat_cache
    filter_q = str(opt.filter_text).strip().lower()
    matches = [p for p in all_paths if filter_q in p.lower()][:50]
    if matches:
        hd.text(f"{len(matches)} matches", font_size="small",
                font_color=s["text_muted"])
    else:
        hd.text("No matching options.", font_color=s["text_muted"])

    # kick a single task to fill in values for the visible matches
    target = matches[:30]
    missing = [
        p for p in target
        if p not in reader._values_cache
        and not reader.is_heavy(p)
    ]
    if missing and not values_task.running:
        values_task.clear()
        values_task.run(_fetch_values, reader, missing)
    # pre-fetch metadata for the same targets so View clicks on filter results are instant
    meta_missing = [
        p for p in target
        if reader.get_cached_meta(p) is None
        and not reader.is_heavy(p)
    ]
    if (meta_missing
            and _last_warmup_filter_key != opt.filter_text
            and not meta_warm_task.running
            and not values_task.running):
        meta_warm_task.clear()
        meta_warm_task.run(_warm_meta, reader, meta_missing)
        _last_warmup_filter_key = opt.filter_text
        _last_warmup_subtree_key = None

    hd.box(height=0.5)
    if matches:
        with hd.box(gap=0.5):
            # for filter results we don't have the tree node handy, so we build a stub {type, description} from the cached values cache
            # type lookup uses the tree if available
            for mpath in target:
                parts = mpath.split(".")
                node = tree
                for part in parts:
                    if isinstance(node, dict) and part in node:
                        node = node[part]
                    else:
                        node = None
                        break
                if isinstance(node, dict):
                    val_meta = node
                else:
                    val_meta = {"type": "?", "description": ""}
                _render_option_row(
                    s, st, reader, opt, fetch_task,
                    values_task, pending.pending, mpath, mpath,
                    val_meta,
                )


def _render_tree_view(s, st, reader, opt, tree, pending, values_task, fetch_task, meta_warm_task):
    # renders the hierarchical tree view of options
    global _last_warmup_subtree_key, _last_warmup_filter_key
    subtree = _get_subtree(tree, opt.path)
    if subtree is None:
        hd.text("Invalid path", font_color=s["danger"])
        return

    items = sorted(subtree.items())
    # filter noise + module-system internals at the root level
    if not opt.path:
        noise = {
            "assertions", "containers", "docker-containers",
            "dysnomia", "ec2-hvm", "ec2-metadata", "routing",
            "security", "specialisation", "system", "virtualisation",
            "ids", "image", "isSpecialisation", "lib", "meta",
            "minifyStaticFiles", "jobs", "_module",
            "passthru", "snapraid", "swapDevices", "warnings",
        }
        items = [(n, v) for n, v in items if n not in noise]

    # collect visible leaf paths (for current view + warm next level)
    leaf_paths = [
        ".".join(opt.path + [n]) if opt.path else n
        for n, v in items if isinstance(v, dict) and "type" in v
    ]
    # pre-warm paths - leaves of every visible branch, one level deeper
    warm_paths = []
    for n, v in items:
        if isinstance(v, dict) and "type" not in v:
            branch_prefix = ".".join(opt.path + [n]) if opt.path else n
            for child_name, child_val in v.items():
                if isinstance(child_val, dict) and "type" in child_val:
                    warm_paths.append(f"{branch_prefix}.{child_name}")
    warm_paths = warm_paths[:50]

    # schedule one async fetch for everything missing in this view (visible leaves + warm targets), unless one is already running
    # heavy paths excluded - including them here means they're never cached, to_fetch never empties, task re-runs every render = infinite loop
    to_fetch = [
        p for p in leaf_paths + warm_paths
        if p not in reader._values_cache
        and not reader.is_heavy(p)
    ]
    if to_fetch and not values_task.running:
        values_task.clear()
        values_task.run(_fetch_values, reader, to_fetch)
    # pre-fetch metadata for the same targets so View clicks on any visible option are instant
    # heavy paths (package-typed) are filtered out - same reason as the values fetch
    meta_targets = [
        p for p in leaf_paths + warm_paths
        if reader.get_cached_meta(p) is None
        and not reader.is_heavy(p)
    ]
    # only fire meta warmup when the user has navigated into a subtree
    # on the root view there are 100+ paths and a cold nix eval cache (tree loaded from disk), so all 10 parallel fetches timeout at 10s
    # the on-demand fetch_task handles View clicks on root-level options
    # also skip if values_task is running - both spawn nix eval subprocesses and running them simultaneously doubles CPU load
    subtree_key = "/".join(opt.path)
    if (meta_targets
            and opt.path
            and _last_warmup_subtree_key != subtree_key
            and not meta_warm_task.running
            and not values_task.running):
        meta_warm_task.clear()
        meta_warm_task.run(_warm_meta, reader, meta_targets)
        _last_warmup_subtree_key = subtree_key
        _last_warmup_filter_key = None

    with hd.box(gap=0.5):
        for name, val in items:
            is_branch = isinstance(val, dict) and "type" not in val
            full_path = ".".join(opt.path + [name]) if opt.path else name

            if is_branch:
                with hd.scope(f"branch-{name}"):
                    with styles.card(s, padding=(0.75, 1, 0.75, 1)):
                        with hd.hbox(gap=1, align="center"):
                            hd.text("📁", font_size="small")
                            hd.text(name, font_weight="bold",
                                    font_color=s["text"])
                            hd.box(grow=1)
                            if hd.button(
                                "→", size="small",
                                background_color=s["bg_item"],
                                font_color=s["text"],
                            ).clicked:
                                opt.path = opt.path + [name]
            else:
                _render_option_row(
                    s, st, reader, opt, fetch_task, values_task,
                    pending.pending, full_path, name, val,
                )



def options():
    # renders the nixos options browser page
    s, st, reader = state.page_setup(with_reader=True)

    if not reader.is_nixos:
        _render_not_nixos(s)
        return

    # loads the options tree once (structure only - names, types, descriptions)
    tree = _load_tree(reader)
    if not tree:
        _render_loading_tree(s)
        return

    _render_options_page(s, st, reader, tree)


def _render_options_page(s, st, reader, tree):
    # state setup, then dispatch to filter view or tree view
    # reactive state for pending intents (updates instantly on click)
    pending = hd.state(pending=st.pending_intents)
    if pending.pending is not st.pending_intents:
        pending.pending = st.pending_intents

    # page navigation state. `expanded` is the full_path whose detail panel is open
    opt = hd.state(
        path=[],
        filter_text="",
        expanded="",
        editing="",
    )
    # hyperdiv controlled inputs overwrite programmatic clears with stale browser state; skip the first N renders after clearing
    filter_guard = hd.state(skip=0)

    # two hd.task() instances - one for background batch value fetches (re-used per navigation), one for on-demand
    # single-path fetches (re-used per expand)
    # plus a simplified meta warmup task
    values_task = hd.task()
    fetch_task = hd.task()
    meta_warm_task = hd.task()

    with hd.box(gap=1):
        _render_breadcrumb(s, opt, filter_guard)
        hd.box(height=0.5)
        _render_filter(s, opt, filter_guard)

        filter_q = str(opt.filter_text).strip().lower()

        if filter_q:
            _render_filter_view(
                s, st, reader, opt, tree, pending,
                values_task, fetch_task, meta_warm_task,
            )
        else:
            _render_tree_view(
                s, st, reader, opt, tree, pending,
                values_task, fetch_task, meta_warm_task,
            )
