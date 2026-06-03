# state management - data model, persistence, singleton
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import hyperdiv as hd

logger = logging.getLogger(__name__)


class IntentType(Enum):
    ADD_PACKAGE = "add_package"
    REMOVE_PACKAGE = "remove_package"
    SET_OPTION = "set_option"


@dataclass
class Intent:
    intent_type: IntentType
    target: str
    generated_nix: str | None = None
    applied: bool = False
    value: Any = None


@dataclass
class AppState:
    pending_intents: list[Intent] = field(default_factory=list)
    activation_mode: str = "switch"
    _config_reader_ref: Any = field(default=None, repr=False)

    def add_intent(self, intent: Intent):
        for existing in self.pending_intents:
            if existing.intent_type == intent.intent_type and existing.target == intent.target:
                existing.value = intent.value
                existing.generated_nix = intent.generated_nix
                return
        self.pending_intents.append(intent)

    def remove_intent(self, intent_type: IntentType, target: str):
        self.pending_intents = [
            i for i in self.pending_intents
            if not (i.intent_type == intent_type and i.target == target)
        ]

    def remove_intent_by_index(self, index: int):
        if 0 <= index < len(self.pending_intents):
            self.pending_intents.pop(index)

    def clear_intents(self):
        self.pending_intents.clear()


def get_config_reader(state: AppState):
    if state._config_reader_ref is None:
        from nixos_gui.config import NixConfigReader  # noqa: PLC0415
        state._config_reader_ref = NixConfigReader()
    return state._config_reader_ref


def _state_dir() -> Path:
    return Path.home() / ".config" / "nixos-gui"


def state_path() -> Path:
    env = os.environ.get("NIXOSGUI_STATE_PATH", "")
    if env:
        return Path(env)
    return _state_dir() / "state.json"


def intent_to_dict(intent: Intent) -> dict:
    return {
        "intent_type": intent.intent_type.value,
        "target": intent.target,
        "generated_nix": intent.generated_nix,
        "applied": intent.applied,
        "value": intent.value,
    }


def intent_from_dict(d: dict) -> Intent:
    return Intent(
        intent_type=IntentType(d["intent_type"]),
        target=d["target"],
        generated_nix=d.get("generated_nix"),
        applied=d.get("applied", False),
        value=d.get("value"),
    )


def load_state() -> AppState:
    sp = state_path()
    if not sp.exists():
        return AppState()
    try:
        data = json.loads(sp.read_text())
        intents = [intent_from_dict(i) for i in data.get("pending_intents", [])]
        return AppState(
            pending_intents=intents,
            activation_mode=data.get("activation_mode", "switch"),
        )
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Corrupt state file %s: %s: starting fresh", sp, e)
        return AppState()


def save_state(state: AppState):
    # atomic on same filesystem - tempfile in same dir as target so os.replace is atomic, can't end up with a half-written state.json on crash
    data = {
        "pending_intents": [intent_to_dict(i) for i in state.pending_intents],
        "activation_mode": state.activation_mode,
    }
    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(sp.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, sp)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


# singleton
_state: AppState | None = None


@hd.global_state
class _IntentVersionState(hd.BaseState):
    # persist() bumps the count to trigger page re-renders which read intent_count_version()
    # all calls share state using hd.global_state
    count = hd.Prop(hd.Int, 0)


def get_state() -> AppState:
    global _state
    if _state is None:
        _state = load_state()
    return _state


def refresh_state():
    global _state
    _state = load_state()


def persist():
    # save state to disk and bump the reactive version counter so all pages re-render without manual refresh
    save_state(get_state())
    _IntentVersionState().count += 1


def intent_count_version() -> int:
    # call this in a page render to establish a reactive dependency on pending_intents mutations
    return _IntentVersionState().count


def page_setup(*, with_reader: bool = False):
    # calls theme_version() and intent_count_version() for side effects (so the page subscribes), then returns get_style() and get_state()
    # pass with_reader=true to also return get_config_reader(st)
    from nixos_gui import styles  # noqa: PLC0415
    s = styles.get_style()
    _ = styles.theme_version()
    _ = intent_count_version()
    st = get_state()
    if with_reader:
        return s, st, get_config_reader(st)
    return s, st




