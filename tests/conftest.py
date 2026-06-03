# test fixtures
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("NIXOSGUI_STATE_PATH", str(tmp_path / "state.json"))
