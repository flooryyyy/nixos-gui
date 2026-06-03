# all nixos-rebuild operations run in hd.task() threads so the UI stays responsive
# task functions return result strings
# the UI thread handles state updates (clearing intents on success, showing errors)
import logging

import hyperdiv as hd

from nixos_gui import apply, state, styles
from nixos_gui.state import IntentType

logger = logging.getLogger(__name__)

# track which intent rows are expanded on the apply page
_expanded_intents: set[str] = set()

# track which set_option targets have already been prefetched into the values cache
# module-level (NOT a task attribute) because hd.task() is recreated per render - task attributes are wiped, causing infinite restart loops if used as a guard
# survives across renders and server reloads within a single Python process
_prefetched_targets: set[str] = set()


# these wrap the synchronous apply module calls
# they receive plain Python values (list copy of intents, reader object) and return a result string
# state mutations (clear_intents, persist) happen in the UI thread via the task's `finished` event

def _do_diff(intents_list, reader):
    # compute diff in background
    entries = apply.compute_diff(intents_list, reader)
    return apply.diff_to_text(entries)


def _do_dry_run(intents_list, reader, switch):
    # write nix + dry-build in background
    # on failure, restores all files to their pre-write state so the user can change their mind without config files being left modified
    result = apply.write_nix(intents_list, reader)
    if not result.success:
        return f"Write failed: {result.message}"
    dr = apply.dry_run(config_reader=reader, switch=switch)
    if not dr.success and result.backup_files:
        apply.restore_backup(result.backup_files)
        return f"{dr.message}\n\n⚠ Config files restored to previous state."
    return dr.message


def _do_apply(intents_list, reader, switch, is_save):
    # write nix + rebuild (or save only) in background
    # on success the message starts with "✓" so the UI can detect it and clear intents
    # on rebuild failure, restores all files to their pre-write state so the user can change their mind
    result = apply.write_nix(intents_list, reader)
    if not result.success:
        return f"Write failed: {result.message}"
    if is_save:
        saved_path = apply.resolve_nix_path(None, reader)
        return f"✓ Config saved to {saved_path}"
    ar = apply.apply(config_reader=reader, switch=switch)
    if ar.success:
        changed = [i.target for i in intents_list if i.intent_type == IntentType.SET_OPTION]
        reader.invalidate(changed_paths=changed)
        return "✓ Applied successfully\n\n" + ar.message
    # rebuild failed - roll back config files so the user isn't stuck with modified files they didn't actually activate
    if result.backup_files:
        apply.restore_backup(result.backup_files)
        return f"{ar.message}\n\n⚠ Config files restored to previous state."
    return ar.message


def apply_page():
    s, st, reader = state.page_setup(with_reader=True)
    # reactive pending intents for instant updates
    # only re-sync when the list reference actually changes - per-render writes can trigger hyperdiv's dirty check on their own (see options.py for the same fix)
    pending = hd.state(pending=st.pending_intents)
    if pending.pending is not st.pending_intents:
        pending.pending = st.pending_intents
    intents = pending.pending

    ap = hd.state(diff_text="", dry_result="", apply_result="")

    # three independent tasks - diff preview, dry-build, full apply
    # hd.task() state persists across renders via auto-assigned keys (creation order is stable)
    # each task runs in the threadpool, so subprocess calls don't block the UI
    diff_task = hd.task()
    dry_task = hd.task()
    apply_task = hd.task()

    any_running = diff_task.running or dry_task.running or apply_task.running

    with hd.box(gap=1.5):
        hd.text("Apply Changes", font_size="x-large", font_weight="bold",
                font_color=s["text"])

        if not intents:
            with styles.card(s, padding=(2, 2, 2, 2)):
                hd.text("No pending changes. Search for packages or browse options first.",
                        font_color=s["text_muted"])
                hd.box(height=1)
                hd.text("→ Go to Search (/search)", font_color=s["accent"])
                hd.box(height=0.5)
                hd.text("→ Browse Options (/options)", font_color=s["accent"])
            return

        with styles.card(s):
            hd.text(f"Pending Changes ({len(intents)})", font_weight="bold",
                    font_color=s["text"])
            hd.box(height=0.75)
            from nixos_gui.pages.home import _fmt_val

            # background prefetch for set_option values so the inline diff can show individual item changes
            # runs in threadpool; on completion hyperdiv re-renders and the cache is warm
            # without this, a cold cache shows "+ [N items]" instead
            # module-level dedup set (NOT task attributes - hd.task() is recreated per render so task attrs are wiped, causing an infinite restart loop if used as a guard)
            set_option_targets = [
                i.target for i in intents
                if i.intent_type == IntentType.SET_OPTION
            ]
            targets_to_fetch = [
                t for t in set_option_targets if t not in _prefetched_targets
            ]

            def _prefetch_values(_targets, _reader):
                if _targets:
                    _reader.get_options_batch(_targets, allow_heavy=True)
                    for t in _targets:
                        _prefetched_targets.add(t)

            prefetch_task = hd.task()
            if targets_to_fetch:
                prefetch_task.rerun(
                    _prefetch_values,
                    targets_to_fetch, reader,
                )

            for intent in intents:
                scope_key = f"apply-intent-{intent.intent_type.value}-{intent.target}"
                with hd.scope(scope_key):
                    itype = intent.intent_type.value
                    is_expanded = scope_key in _expanded_intents
                    if itype == "add_package":
                        color = s["success"]
                        sign = "+"
                        short_label = intent.target
                    elif itype == "remove_package":
                        color = s["danger"]
                        sign = "-"
                        short_label = intent.target
                    else:
                        sign = "±"
                        old_val = reader._values_cache.get(intent.target)
                        if isinstance(old_val, bool) and isinstance(intent.value, bool):
                            color = s["success"] if intent.value and not old_val else s["danger"]
                        elif old_val is None and intent.value is not None:
                            color = s["success"]
                        elif old_val is not None and intent.value is None:
                            color = s["danger"]
                        elif isinstance(old_val, list) and isinstance(intent.value, list):
                            color = s["success"] if len(intent.value) > len(old_val) else s["danger"]
                        else:
                            color = s["accent"]
                        short_label = intent.target

                    with hd.hbox(gap=0.5, align="center"):
                        if hd.icon_button(
                            "x",
                            background_color=s["bg_item"],
                            font_color=s["text"],
                            disabled=any_running,
                        ).clicked:
                            st.remove_intent(intent.intent_type, intent.target)
                            state.persist()

                        # all labels are plain text - identical look
                        hd.text(f"{sign} {short_label}", font_color=color)

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
                                disabled=any_running,
                            ).clicked:
                                if is_expanded:
                                    _expanded_intents.discard(scope_key)
                                else:
                                    _expanded_intents.add(scope_key)
                                state.persist()

                    if is_expanded and itype == "set_option":
                        with hd.box(padding=(0.5, 0, 0, 2.5), gap=0.15):
                            old_val = reader._values_cache.get(intent.target)
                            if isinstance(old_val, list) and isinstance(intent.value, list):
                                old_set = set(map(str, old_val))
                                new_set = set(map(str, intent.value))
                                removed = old_set - new_set
                                added = new_set - old_set
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

        hd.box(height=1)

        mode_labels = {
            "switch": "Apply: build, activate, persist to boot",
            "test": "Apply (test): build & activate, revert on reboot",
            "save": "Save: write config only",
        }
        action_label = mode_labels.get(st.activation_mode, "Apply")
        is_save = st.activation_mode == "save"

        with hd.hbox(gap=1):
            if hd.button(
                "Check Build",
                background_color=s["bg_item"],
                font_color=s["text"],
                size="medium",
                disabled=any_running,
            ).clicked:
                if not reader.is_nixos:
                    ap.dry_result = "Dry run requires a NixOS system."
                else:
                    ap.dry_result = ""
                    dry_task.rerun(
                        _do_dry_run, list(intents), reader,
                        st.activation_mode == "switch",
                    )

            if hd.button(
                action_label,
                background_color=s["accent"],
                font_color="#ffffff",
                size="medium",
                disabled=any_running,
            ).clicked:
                if is_save:
                    ap.apply_result = ""
                    apply_task.rerun(
                        _do_apply, list(intents), reader, False, True,
                    )
                elif reader.is_nixos:
                    ap.apply_result = ""
                    apply_task.rerun(
                        _do_apply, list(intents), reader,
                        st.activation_mode == "switch", False,
                    )
                else:
                    ap.apply_result = "Apply requires a NixOS system."

        if dry_task.running:
            hd.box(height=0.5)
            with hd.hbox(gap=0.5, align="center"):
                hd.spinner()
                hd.text("Running dry-build (this may take a minute)...",
                        font_size="small", font_color=s["text_muted"])

        if apply_task.running:
            hd.box(height=0.5)
            with hd.hbox(gap=0.5, align="center"):
                hd.spinner()
                label = "Saving config..." if is_save else "Building and activating (this may take a few minutes)..."
                hd.text(label, font_size="small", font_color=s["text_muted"])

        # capture dry_run result on completion
        if dry_task.finished:
            if dry_task.error:
                ap.dry_result = f"Error: {dry_task.error}"
            else:
                ap.dry_result = dry_task.result or "(no output)"

        # capture apply result on completion
        if apply_task.finished:
            if apply_task.error:
                ap.apply_result = f"Error: {apply_task.error}"
            else:
                result = apply_task.result or "(no output)"
                ap.apply_result = result
                if result.startswith("✓"):
                    st.clear_intents()
                    state.persist()

        if ap.dry_result:
            with styles.card(s):
                hd.text("Dry Run Output", font_weight="bold", font_color=s["text"])
                hd.box(height=0.5)
                hd.text(ap.dry_result[:2000],
                        font_size="small", font_color=s["text_muted"])

        if ap.apply_result:
            hd.box(height=0.5)
            is_success = ap.apply_result.startswith("✓")
            with styles.card(s,
                background_color=hd.lighten(s["success"], 0.7) if is_success
                else hd.lighten(s["danger"], 0.7),
                border=f"1px solid {s['success'] if is_success else s['danger']}"):
                hd.text(ap.apply_result[:2000],
                        font_size="small", font_color=s["text"])
