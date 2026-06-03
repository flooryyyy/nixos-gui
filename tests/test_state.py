# state model tests
import json
import sys

import pytest

sys.path.insert(0, "src")

from nixos_gui.state import (
    Intent,
    IntentType,
    AppState,
    intent_to_dict,
    intent_from_dict,
    load_state,
    save_state,
    state_path,
)


class TestIntent:
    def test_create(self):
        i = Intent(intent_type=IntentType.ADD_PACKAGE, target="firefox")
        assert i.intent_type == IntentType.ADD_PACKAGE
        assert i.target == "firefox"

    def test_serialize(self):
        i = Intent(intent_type=IntentType.SET_OPTION, target="services.openssh.enable",
                   value=True, applied=False)
        d = intent_to_dict(i)
        assert d["intent_type"] == "set_option"
        assert d["target"] == "services.openssh.enable"
        assert d["value"] is True

    def test_deserialize(self):
        d = {"intent_type": "add_package", "target": "firefox", "value": None,
             "applied": False, "generated_nix": None}
        i = intent_from_dict(d)
        assert i.intent_type == IntentType.ADD_PACKAGE
        assert i.target == "firefox"


class TestAppState:
    def test_add_intent(self):
        s = AppState()
        s.add_intent(Intent(IntentType.ADD_PACKAGE, "firefox"))
        assert len(s.pending_intents) == 1

    def test_add_duplicate_replaces(self):
        s = AppState()
        s.add_intent(Intent(IntentType.ADD_PACKAGE, "firefox", value=None))
        s.add_intent(Intent(IntentType.ADD_PACKAGE, "firefox", value="v2"))
        assert len(s.pending_intents) == 1
        assert s.pending_intents[0].value == "v2"

    def test_remove_intent(self):
        s = AppState()
        s.add_intent(Intent(IntentType.ADD_PACKAGE, "firefox"))
        s.remove_intent(IntentType.ADD_PACKAGE, "firefox")
        assert len(s.pending_intents) == 0

    def test_remove_by_index(self):
        s = AppState()
        s.add_intent(Intent(IntentType.ADD_PACKAGE, "a"))
        s.add_intent(Intent(IntentType.ADD_PACKAGE, "b"))
        s.remove_intent_by_index(0)
        assert len(s.pending_intents) == 1
        assert s.pending_intents[0].target == "b"

    def test_clear(self):
        s = AppState()
        s.add_intent(Intent(IntentType.ADD_PACKAGE, "a"))
        s.add_intent(Intent(IntentType.ADD_PACKAGE, "b"))
        s.clear_intents()
        assert len(s.pending_intents) == 0

    def test_default_activation_mode(self):
        s = AppState()
        assert s.activation_mode == "switch"


class TestPersistence:
    def test_save_load_roundtrip(self, tmp_state, tmp_path):
        s = AppState()
        s.add_intent(Intent(IntentType.ADD_PACKAGE, "firefox"))
        s.activation_mode = "test"
        save_state(s)

        sp = state_path()
        assert sp.exists()

        loaded = load_state()
        assert loaded.activation_mode == "test"
        assert len(loaded.pending_intents) == 1
        assert loaded.pending_intents[0].target == "firefox"

    def test_load_missing_file(self, tmp_state):
        sp = state_path()
        if sp.exists():
            sp.unlink()
        s = load_state()
        assert len(s.pending_intents) == 0
        assert s.activation_mode == "switch"

    def test_load_corrupt_file(self, tmp_state, tmp_path):
        sp = state_path()
        sp.write_text("not json")
        s = load_state()
        assert len(s.pending_intents) == 0
