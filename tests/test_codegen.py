# tests for nix code generation
import pytest
from pathlib import Path
import tempfile

from nixos_gui.codegen import (
    _nix_value,
    _build_attrset,
    _merge_attrsets,
    _render_attrset,
    generate_nix_module,
    write_generated_nix,
    generate_template_nix,
    ensure_template,
)
from nixos_gui.state import Intent, IntentType


class TestNixValue:
    def test_bool_true(self):
        assert _nix_value(True) == "true"
    
    def test_bool_false(self):
        assert _nix_value(False) == "false"
    
    def test_int(self):
        assert _nix_value(42) == "42"
    
    def test_float(self):
        assert _nix_value(3.14) == "3.14"
    
    def test_string(self):
        assert _nix_value("hello") == '"hello"'
    
    def test_string_with_quotes(self):
        assert _nix_value('say "hi"') == '"say \\"hi\\""'
    
    def test_string_with_backslash(self):
        assert _nix_value("path\\to") == '"path\\\\to"'
    
    def test_none(self):
        assert _nix_value(None) == "null"
    
    def test_empty_list(self):
        assert _nix_value([]) == "[ ]"
    
    def test_list(self):
        assert _nix_value([1, 2, 3]) == "[ 1 2 3 ]"
    
    def test_list_strings(self):
        assert _nix_value(["a", "b"]) == '[ "a" "b" ]'
    
    def test_empty_dict(self):
        assert _nix_value({}) == "{ }"
    
    def test_dict(self):
        result = _nix_value({"x": 1, "y": 2})
        assert "x = 1;" in result
        assert "y = 2;" in result


class TestBuildAttrset:
    def test_simple(self):
        result = _build_attrset("services.openssh.enable", True)
        assert result == {"services": {"openssh": {"enable": True}}}
    
    def test_single_part(self):
        result = _build_attrset("hostname", "laptop")
        assert result == {"hostname": "laptop"}
    
    def test_deep_nesting(self):
        result = _build_attrset("a.b.c.d", 42)
        assert result == {"a": {"b": {"c": {"d": 42}}}}


class TestMergeAttrsets:
    def test_disjoint(self):
        base = {"a": 1}
        override = {"b": 2}
        result = _merge_attrsets(base, override)
        assert result == {"a": 1, "b": 2}
    
    def test_override_scalar(self):
        base = {"a": 1}
        override = {"a": 2}
        result = _merge_attrsets(base, override)
        assert result == {"a": 2}
    
    def test_merge_nested(self):
        base = {"services": {"openssh": {"enable": False}}}
        override = {"services": {"openssh": {"port": 2222}}}
        result = _merge_attrsets(base, override)
        assert result == {"services": {"openssh": {"enable": False, "port": 2222}}}


class TestRenderAttrset:
    def test_simple(self):
        attrs = {"enable": True}
        result = _render_attrset(attrs)
        assert "enable = true;" in result
    
    def test_nested(self):
        attrs = {"services": {"openssh": {"enable": True}}}
        result = _render_attrset(attrs)
        assert "services = {" in result
        assert "openssh = {" in result
        assert "enable = true;" in result


class TestGenerateNixModule:
    def test_empty_intents(self):
        result = generate_nix_module([])
        assert result == "{ config, pkgs, lib, ... }: { }"
    
    def test_single_option(self):
        intents = [
            Intent(
                intent_type=IntentType.SET_OPTION,
                target="services.openssh.enable",
                value=True,
            )
        ]
        result = generate_nix_module(intents)
        assert "{ config, pkgs, lib, ... }:" in result
        assert "services = {" in result
        assert "openssh = {" in result
        assert "enable = true;" in result
    
    def test_multiple_options(self):
        intents = [
            Intent(
                intent_type=IntentType.SET_OPTION,
                target="services.openssh.enable",
                value=True,
            ),
            Intent(
                intent_type=IntentType.SET_OPTION,
                target="services.openssh.port",
                value=2222,
            ),
        ]
        result = generate_nix_module(intents)
        assert "enable = true;" in result
        assert "port = 2222;" in result
    
    def test_mixed_intents(self):
        intents = [
            Intent(
                intent_type=IntentType.ADD_PACKAGE,
                target="vim",
            ),
            Intent(
                intent_type=IntentType.SET_OPTION,
                target="networking.hostName",
                value="laptop",
            ),
        ]
        result = generate_nix_module(intents)
        assert "networking = {" in result
        assert "hostName = " in result
        assert "environment.systemPackages" in result
        assert "vim" in result


class TestWriteGeneratedNix:
    def test_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.nix"
            content = "{ pkgs, ... }: { }"
            result = write_generated_nix(content, path)
            assert result == path
            assert path.exists()
            assert path.read_text() == content
    
    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "subdir" / "test.nix"
            content = "{ }"
            write_generated_nix(content, path)
            assert path.exists()


class TestTemplate:
    def test_generate_template(self):
        result = generate_template_nix()
        assert "imports = [ ./generated.nix ];" in result
    
    def test_ensure_template(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nixos-gui.nix"
            result = ensure_template(path)
            assert result == path
            assert path.exists()
            assert "imports" in path.read_text()
    
    def test_ensure_template_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "nixos-gui.nix"
            path.write_text("existing content")
            result = ensure_template(path)
            assert path.read_text() == "existing content"
