# nix code generation - converts intents to valid nix expressions
import logging
import tempfile
from pathlib import Path
from typing import Any

from nixos_gui.state import Intent, IntentType

logger = logging.getLogger(__name__)

GENERATED_NIX_PATH = Path.home() / ".config" / "nixos-gui" / "generated.nix"


def _nix_value(value: Any) -> str:
    # canonical python-to-nix serializer used by codegen.py and apply.py - handles None, bool, int/float, str (with $ escaping), list, and dict
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # escape special chars in nix strings ($ is interpolation)
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        return f'"{escaped}"'
    if isinstance(value, list):
        items = " ".join(_nix_value(item) for item in value)
        return f"[ {items} ]" if items else "[ ]"
    if isinstance(value, dict):
        lines = []
        for k, v in sorted(value.items()):
            lines.append(f"  {k} = {_nix_value(v)};")
        inner = "\n".join(lines)
        return "{\n" + inner + "\n}" if inner else "{ }"
    # fallback - treat as string
    return f'"{value}"'


def _build_attrset(path: str, value: Any) -> dict:
    # build nested attrset from dotted path + value (e.g. "services.openssh.enable" + true -> {"services" - {"openssh" - {"enable" - True}}})
    parts = path.split(".")
    result = {}
    current = result
    for _, part in enumerate(parts[:-1]):
        current[part] = {}
        current = current[part]
    current[parts[-1]] = value
    return result


def _merge_attrsets(base: dict, override: dict) -> dict:
    # recursively merge two attrsets, override wins on conflicts
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _merge_attrsets(result[key], val)
        else:
            result[key] = val
    return result


def _render_attrset(attrs: dict, indent: int = 0) -> str:
    # render a nested dict as nix attrset syntax
    prefix = "  " * indent
    lines = []
    for key, val in sorted(attrs.items()):
        if isinstance(val, dict):
            inner = _render_attrset(val, indent + 1)
            lines.append(f"{prefix}{key} = {inner};")
        else:
            lines.append(f"{prefix}{key} = {_nix_value(val)};")
    if indent == 0:
        return "{\n" + "\n".join(lines) + "\n}"
    return "{\n" + "\n".join(lines) + f"\n{prefix}}}"


def generate_nix_module(
    intents: list[Intent],
    current_packages: list[str] | None = None,
) -> str:
    # SET_OPTION → nested attrsets, ADD_PACKAGE → environment.systemPackages, REMOVE_PACKAGE → builtins.filter
    # if current_packages is provided, existing packages are preserved minus explicit removals
    if not intents:
        return "{ config, pkgs, lib, ... }: { }"
    
    adds: list[str] = []
    removes: list[str] = []
    option_intents: list[Intent] = []
    
    for intent in intents:
        if intent.intent_type == IntentType.ADD_PACKAGE:
            adds.append(intent.target)
        elif intent.intent_type == IntentType.REMOVE_PACKAGE:
            removes.append(intent.target)
        elif intent.intent_type == IntentType.SET_OPTION:
            option_intents.append(intent)
    
    if not adds and not removes and not option_intents:
        return "{ config, pkgs, lib, ... }: { }"
    
    # build merged attrset from SET_OPTION intents only
    # package adds/removes are rendered as flat lines below because `with pkgs;` and `builtins.filter` can't be expressed in the plain dict→attrset renderer
    merged = {}
    for intent in option_intents:
        attrset = _build_attrset(intent.target, intent.value)
        merged = _merge_attrsets(merged, attrset)

    lines: list[str] = []

    # render option attrsets
    if merged:
        body = _render_attrset(merged)
        # strip outer braces - we'll re-wrap with the module header
        inner = body.strip("{}\n")
        if inner:
            lines.append(inner)

    # render package adds - preserve existing packages if provided
    if adds or (current_packages and removes):
        existing = current_packages or []
        merged = [p for p in existing if p not in removes] + [
            a for a in adds if a not in existing
        ]
        if merged:
            pkgs_str = " ".join(merged)
            lines.append(f"  environment.systemPackages = with pkgs; [ {pkgs_str} ];")
    elif adds:
        pkgs_str = " ".join(adds)
        lines.append(f"  environment.systemPackages = with pkgs; [ {pkgs_str} ];")
    
    # render package removes - only emit builtins.filter lines for packages that weren't in the preserved list (i.e packages being removed from other modules, not from generated.nix)
    for pkg in removes:
        if current_packages and pkg in current_packages:
            continue  # already excluded from the merged list above
        lines.append(
            f'  environment.systemPackages = '
            f'builtins.filter (p: p.pname or p.name or "" != "{pkg}") '
            f'config.environment.systemPackages;'
        )
    
    if not lines:
        return "{ pkgs, ... }: { }"
    
    return "{ config, pkgs, lib, ... }: {\n" + "\n".join(lines) + "\n}"


def write_generated_nix(content: str, path: Path = GENERATED_NIX_PATH) -> Path:
    # write generated nix to file atomically
    path.parent.mkdir(parents=True, exist_ok=True)

    # atomic write - write to temp, then rename
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        suffix=".nix",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    tmp_path.rename(path)
    logger.info("Wrote generated.nix to %s", path)
    return path


def generate_template_nix() -> str:
    # generate the nixos-gui.nix template that imports generated.nix
    return """{ ... }: {
  imports = [ ./generated.nix ];
}
"""


def ensure_template(path: Path = Path.home() / ".config" / "nixos-gui" / "nixos-gui.nix") -> Path:
    # create nixos-gui.nix template if it doesn't exist
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_template_nix())
    logger.info("Created nixos-gui.nix template at %s", path)
    return path
