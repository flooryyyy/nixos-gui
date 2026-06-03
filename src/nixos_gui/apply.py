# apply engine - nix generation, diff, rebuild
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nixos_gui.codegen import generate_nix_module
from nixos_gui.state import Intent, IntentType

logger = logging.getLogger(__name__)


class PkexecMissing(RuntimeError):
    pass

BOOTSTRAP_MARKER = "# {nixos-gui bootstrap - do not edit below this line}"
DEFAULT_NIX_PATH = Path("/etc/nixos/nixos-gui.nix")
GENERATED_NIX_NAME = "generated.nix"
WRAPPER_NIX_NAME = "nixos-gui.nix"


# picks the right output file for the generated config
# step 1 - explicit nix_path argument wins
# step 2 - if the user's config_path has a wrapper (nixos-gui.nix) that imports generated.nix, write to generated.nix so we don't clobber the user's hand-written wrapper
# step 3 - otherwise write to <config_path>/nixos-gui.nix so a single-file config still works without a wrapper
# step 4 - fall back to DEFAULT_NIX_PATH (/etc/nixos/nixos-gui.nix) when no reader is available so a vanilla /etc/nixos setup still functions
def resolve_nix_path(nix_path: Path | None, config_reader: Any | None = None,) -> Path:
    if nix_path is not None:
        return nix_path
    if config_reader is not None and getattr(config_reader, "config_path", None):
        base = Path(config_reader.config_path)
        wrapper = base / WRAPPER_NIX_NAME
        generated = base / GENERATED_NIX_NAME
        if wrapper.exists():
            try:
                content = wrapper.read_text()
                if GENERATED_NIX_NAME in content:
                    return generated
            except OSError:
                pass
        return base / WRAPPER_NIX_NAME
    return DEFAULT_NIX_PATH

# one row in the apply page's diff view - option path + current value + pending value + change kind
@dataclass
class DiffEntry:
    option: str
    current: str
    pending: str
    change_type: str  # "add" | "remove" | "modify"

# result of a dry-run or apply - success flag + user-facing message + optional backup map for restore-on-failure
@dataclass
class ApplyResult:
    success: bool
    message: str
    nix_content: str | None = None
    # maps file paths to their original content before write_nix edited them
    # used by restore_backup() if the rebuild fails
    backup_files: dict[str, str] | None = None


# matches valid nix identifiers - letter/underscore start, then letters/digits/underscores/single-quotes/hyphens
# nix allows ' and - (e.g. foo', bar-baz) where most languages don't
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_'\-]*$")


def _validate_nix_ident(name: str) -> bool:
    return bool(_IDENT_RE.match(name))



def compute_diff(intents: list[Intent], config_reader) -> list[DiffEntry]:
    entries: list[DiffEntry] = []
    current_packages = config_reader.get_packages() if config_reader.is_nixos else []

    for intent in intents:
        if intent.intent_type == IntentType.ADD_PACKAGE:
            if intent.target not in current_packages:
                entries.append(DiffEntry(
                    option=f"packages.{intent.target}",
                    current="(not installed)",
                    pending=intent.target,
                    change_type="add",
                ))
        elif intent.intent_type == IntentType.REMOVE_PACKAGE:
            if intent.target in current_packages:
                entries.append(DiffEntry(
                    option=f"packages.{intent.target}",
                    current=intent.target,
                    pending="(removed)",
                    change_type="remove",
                ))
        elif intent.intent_type == IntentType.SET_OPTION:
            batch = config_reader.get_options_batch([intent.target], allow_heavy=True)
            current_val = batch.get(intent.target)
            entries.append(DiffEntry(
                option=intent.target,
                current=str(current_val) if current_val is not None else "(not set)",
                pending=str(intent.value),
                change_type="modify",
            ))
    return entries


def diff_to_text(entries: list[DiffEntry]) -> str:
    if not entries:
        return "# No changes"
    lines = ["# nixos-gui pending changes", ""]
    for e in entries:
        prefix = {"add": "+", "remove": "-", "modify": "~"}.get(e.change_type, "?")
        lines.append(f"  {prefix} {e.option}: {e.current} → {e.pending}")
    return "\n".join(lines)



def _parse_existing_packages(nix_content: str) -> list[str]:
    # extracts package names from a generated.nix file's systemPackages line
    m = re.search(
        r"environment\.systemPackages\s*=\s*with\s+pkgs;\s*\[([^\]]*)\]",
        nix_content,
    )
    if not m:
        return []
    return m.group(1).split()


def _find_option_source_files(config_path: Path, option_path: str) -> list[Path]:
    # finds .nix files that define the given list option
    # searches all .nix files in the config tree (excluding nixos-gui's own files) for the last component of the option
    # path followed by '= ['
    # returns matching file paths sorted for deterministic ordering
    last_component = option_path.split(".")[-1]
    skip_files = {"nixos-gui.nix", "generated.nix"}
    results: list[Path] = []
    for nix_file in sorted(config_path.rglob("*.nix")):
        if nix_file.name in skip_files:
            continue
        try:
            content = nix_file.read_text()
            # match - optionName = [  (with optional whitespace)
            if re.search(rf'{re.escape(last_component)}\s*=\s*\[', content):
                results.append(nix_file)
        except OSError:
            continue
    return results


def _remove_items_from_nix_list(file_path: Path, option_name: str, items: list[str],) -> bool:
    # edits a .nix file to remove specific string items from a list option
    # handles both single-line and multi-line list syntax
    # returns true if the file was modified
    content = file_path.read_text()
    # match - optionName = [ ... ]  (list content is between [ and ])
    pattern = rf'({re.escape(option_name)}\s*=\s*\[)([^\]]*)(\])'
    modified = False

    def _remove_in_list(match: re.Match) -> str:
        nonlocal modified
        prefix, list_content, suffix = (
            match.group(1), match.group(2), match.group(3),
        )
        original = list_content
        for item in items:
            # removes "item" or 'item' with surrounding whitespace
            list_content = re.sub(
                rf'\s*["\']{re.escape(item)}["\']',
                '',
                list_content,
            )
        # clean up orphaned comments left behind after item removal (e.g. "Block Firewire # # Block Thunderbolt ...")
        list_content = re.sub(r'(#[^\n]*)#[^\n]*', r'\1', list_content)
        if list_content != original:
            modified = True
        return prefix + list_content + suffix

    new_content = re.sub(pattern, _remove_in_list, content)

    if modified:
        file_path.write_text(new_content)

    return modified


def write_nix(
    intents: list[Intent],
    config_reader,
    nix_path: Path | None = None,
) -> ApplyResult:
    # backups of every file we modify, for rollback on rebuild failure
    backup: dict[str, str] = {}

    # when removing items from a list option, we must edit the source file where the option is defined
    # nixos merges list options by concatenation, so writing a shorter list to nixos-gui.nix wouldn't actually remove anything
    codegen_intents: list[Intent] = []

    for intent in intents:
        skip_codegen = False

        if intent.intent_type == IntentType.SET_OPTION and config_reader:
            # ensure the current value is in the cache
            config_reader.get_options_batch(
                [intent.target], allow_heavy=True,
            )
            old_val = config_reader._values_cache.get(intent.target)
            if isinstance(old_val, list) and isinstance(intent.value, list):
                old_set = set(map(str, old_val))
                new_set = set(map(str, intent.value))
                removed = old_set - new_set
                added = new_set - old_set

                if removed and getattr(config_reader, "config_path", None):
                    config_path = Path(config_reader.config_path)
                    option_name = intent.target.split(".")[-1]
                    source_files = _find_option_source_files(
                        config_path, intent.target,
                    )
                    edited_any = False
                    for sf in source_files:
                        # save original before editing (for rollback)
                        sf_key = str(sf)
                        if sf_key not in backup:
                            try:
                                backup[sf_key] = sf.read_text()
                            except OSError:
                                pass
                        if _remove_items_from_nix_list(sf, option_name, list(removed)):
                            _git_add_if_repo(sf)
                            edited_any = True
                            logger.info("Edited %s to remove %s from %s",
                                        sf, removed, intent.target)

                    if edited_any:
                        if not added:
                            # pure removal - source file has the correct remaining items. don't write to nixos-gui.nix
                            skip_codegen = True
                        else:
                            # mixed - edit source for removals, only write added items to nixos-gui.nix
                            intent = Intent(
                                intent_type=intent.intent_type,
                                target=intent.target,
                                value=[v for v in intent.value
                                       if str(v) in added],
                            )

        if not skip_codegen:
            codegen_intents.append(intent)

    nix_path = resolve_nix_path(nix_path, config_reader)
    # parse existing generated.nix to get GUI-managed packages (not all system packages - just what nixos-gui previously wrote)
    existing = ""
    if nix_path.exists():
        try:
            existing = nix_path.read_text()
        except OSError:
            pass
    # save original nixos-gui.nix for rollback (even if empty = file didn't exist)
    backup[str(nix_path)] = existing
    current_packages = _parse_existing_packages(existing)
    content = generate_nix_module(codegen_intents, current_packages=current_packages)
    # preserve content above bootstrap marker if present
    # the codegen output includes a module header but the existing file already has one above the marker, so strip it to avoid a duplicate
    codegen_body = content
    header_prefix = "{ config, pkgs, lib, ... }: "
    if content.startswith(header_prefix):
        codegen_body = content[len(header_prefix):]
    if BOOTSTRAP_MARKER in existing:
        above = existing.split(BOOTSTRAP_MARKER, 1)[0]
        new_content = above + BOOTSTRAP_MARKER + "\n" + codegen_body
    else:
        new_content = header_prefix + "\n\n" + BOOTSTRAP_MARKER + "\n" + codegen_body
    nix_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(nix_path.parent), suffix=".nix")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_content)
        os.replace(tmp, nix_path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        return ApplyResult(success=False, message="Failed to write nixos-gui.nix")

    # nix flakes only see git-tracked files
    # if the config dir is a git repo, stage the written file so nixos-rebuild can see it
    _git_add_if_repo(nix_path)

    return ApplyResult(
        success=True, message="Config written",
        nix_content=new_content, backup_files=backup,
    )


def restore_backup(backup: dict[str, str]) -> bool:
    # restores files to their pre-write_nix state
    # called when a rebuild fails (build error, password timeout, user changed their mind)
    # restores every file that write_nix touched to its original content
    # if a file didn't exist before (empty string backup), it is deleted
    # returns true if all restores succeeded
    all_ok = True
    for file_path, original in backup.items():
        p = Path(file_path)
        try:
            if original == "" and not p.exists():
                # file never existed, nothing to restore
                continue
            if original == "":
                # file was created by write_nix, remove it
                p.unlink(missing_ok=True)
            else:
                p.write_text(original)
            _git_add_if_repo(p)
            logger.info("Restored %s from backup", p)
        except OSError as e:
            logger.warning("Failed to restore %s: %s", p, e)
            all_ok = False
    return all_ok


def _git_add_if_repo(nix_path: Path) -> None:
    # stages nix_path in git if the config dir is a git repo
    # nix flakes ignore untracked files, so a freshly-written nixos-gui.nix or generated.nix is invisible to nixos-rebuild until git-tracked
    try:
        result = subprocess.run(
            ["git", "-C", str(nix_path.parent), "add", nix_path.name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.debug("git add failed (not a repo?): %s", result.stderr[:200])
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass



def _build_nixos_cmd(
    action: str,
    config_reader: Any | None = None,
    nix_path: Path | None = None,
    dry_run: bool = False,
) -> list[str]:
    # builds the nixos-rebuild command list with the right path / flake flags
    # the reader's config_path may be a flake (flake.nix present) or a legacy single-file config (configuration.nix)
    # for flake configs we need to pass --flake <path>#<config-name> so nixos-rebuild doesn't try to read configuration.nix from cwd
    # for legacy configs the cwd already works because nix_path.parent is the config dir with a configuration.nix in it
    # note - dry-run is a subcommand, NOT a --dry-run flag (the original code conflated them)
    if dry_run:
        # map real actions to their dry counterparts
        # ``switch`` and ``test`` don't have direct dry equivalents; ``dry-build`` is the closest ("would this build succeed without actually building")
        action = {"switch": "dry-build", "test": "dry-build", "boot": "dry-build"}.get(
            action, "dry-build"
        )
    base = ["/run/current-system/sw/bin/nixos-rebuild", action]

    # test/switch/boot need root
    # dry-build doesn't
    # pkexec shows a graphical password dialog so the user can enter the sudo password
    # if pkexec isn't installed, the caller gets a PkexecMissing error rather than silently falling back (sudo needs a tty a GUI app doesn't have, so any "fallback" was always broken in practice)
    if not dry_run and action in ("test", "switch", "boot"):
        if not os.path.isfile("/run/wrappers/bin/pkexec"):
            raise PkexecMissing("no graphical auth tool (pkexec) found; install polkit")
        base = ["/run/wrappers/bin/pkexec"] + base

    if config_reader is not None and getattr(config_reader, "config_path", None):
        # use absolute path - nixos-rebuild needs it to find the flake
        cfg_dir = Path(config_reader.config_path).resolve()
        flake_nix = cfg_dir / "flake.nix"
        config_nix = cfg_dir / "configuration.nix"
        if flake_nix.exists() and not config_nix.exists():
            # pure flake - need --flake to make nixos-rebuild use it
            name = getattr(config_reader, "config_name", None) or "default"
            # use absolute path to be safe; the user can override via cwd if they want
            base += ["--flake", f"{cfg_dir}#{name}"]
    return base


def dry_run(
    config_reader: Any | None = None,
    nix_path: Path | None = None,
    switch: bool = False,
) -> ApplyResult:
    nix_path = resolve_nix_path(nix_path, config_reader)
    if not nix_path.exists():
        return ApplyResult(success=False, message="nixos-gui.nix not found: write first")
    # use dry-build so the user sees what would be built/activated without actually building
    # (dry-run used to be passed as `--dry-run` which nixos-rebuild rejects; `dry-build` is the correct subcommand)
    try:
        cmd = _build_nixos_cmd(
            "switch" if switch else "test",
            config_reader, nix_path, dry_run=True,
        )
    except PkexecMissing as e:
        return ApplyResult(success=False, message=str(e))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=str(nix_path.parent),
        )
        combined = (result.stdout + result.stderr).strip()
        return ApplyResult(
            success=result.returncode == 0,
            message=combined or "(no output)",
        )
    except FileNotFoundError:
        return ApplyResult(success=False, message="nixos-rebuild not found")


def apply(
    config_reader: Any | None = None,
    nix_path: Path | None = None,
    switch: bool = False,
) -> ApplyResult:
    nix_path = resolve_nix_path(nix_path, config_reader)
    if not nix_path.exists():
        return ApplyResult(success=False, message="nixos-gui.nix not found: write first")
    action = "switch" if switch else "test"
    try:
        cmd = _build_nixos_cmd(action, config_reader, nix_path, dry_run=False)
    except PkexecMissing as e:
        return ApplyResult(success=False, message=str(e))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=str(nix_path.parent),
        )
        combined = (result.stdout + result.stderr).strip()
        return ApplyResult(
            success=result.returncode == 0,
            message=combined or "(no output)",
        )
    except FileNotFoundError:
        return ApplyResult(success=False, message="nixos-rebuild not found")



def check_bootstrap(
    config_reader: Any | None = None,
    nix_path: Path | None = None,
) -> str | None:
    nix_path = resolve_nix_path(nix_path, config_reader)
    base = (
        Path(config_reader.config_path)
        if config_reader is not None and getattr(config_reader, "config_path", None)
        else Path("/etc/nixos")
    )
    # check for both the wrapper (nixos-gui.nix) and the generated file
    # resolve_nix_path may return generated.nix when the wrapper exists, but the host config imports the wrapper, not generated.nix directly
    names_to_check = {nix_path.name, WRAPPER_NIX_NAME}
    hosts_dir = base / "hosts"
    search_paths = [
        hosts_dir.glob("*.nix"),
        [base / "configuration.nix"],
    ]
    for group in search_paths:
        for config_file in group:
            if not config_file.exists():
                continue
            try:
                content = config_file.read_text()
                if any(name in content for name in names_to_check):
                    return None
            except OSError:
                pass
    return f"{WRAPPER_NIX_NAME} is not imported in any NixOS config. Add it to Settings."


def bootstrap_import(
    config_reader: Any | None = None,
    nix_path: Path | None = None,
) -> ApplyResult:
    # auto-add ./nixos-gui.nix to the user's NixOS config imports
    nix_path = resolve_nix_path(nix_path, config_reader)
    base = (
        Path(config_reader.config_path)
        if config_reader is not None and getattr(config_reader, "config_path", None)
        else Path("/etc/nixos")
    )
    nix_name = nix_path.name
    hosts_dir = base / "hosts"

    # find the right config file to edit
    hn = _get_hostname()
    candidate = None
    for f in [
        hosts_dir / f"{hn}.nix",           # hosts/laptop.nix
        hosts_dir / hn / "default.nix",    # hosts/laptop/default.nix
        base / "configuration.nix",
    ]:
        if f.exists():
            candidate = f
            break
    if candidate is None:
        return ApplyResult(success=False, message="No NixOS config file found to edit.")

    content = candidate.read_text()

    # already imported?
    if nix_name in content:
        return ApplyResult(success=True, message=f"{nix_name} already imported in {candidate}")

    # compute relative path from the config file to nixos-gui.nix
    try:
        rel = os.path.relpath(nix_path, candidate.parent)
        import_line = rel if rel.startswith("./") or rel.startswith("../") else f"./{rel}"
    except ValueError:
        import_line = f"./{nix_name}"
    if "imports" in content:
        # add to existing imports list
        m = re.search(r'(imports\s*=\s*\[)', content)
        if m:
            pos = m.end()
            # detect existing indent from the first import line
            rest = content[pos:]
            indent_match = re.search(r'\n(\s+)\S', rest)
            indent = indent_match.group(1) if indent_match else "  "
            after_bracket = content[pos:pos+1]
            if after_bracket == "\n":
                sep = f"\n{indent}"
            else:
                sep = " "
            new = content[:pos] + f"{sep}{import_line}" + content[pos:]
        else:
            return ApplyResult(success=False, message="Found imports but couldn't parse: add manually.")
    else:
        # no imports list - add one before the closing }
        last_brace = content.rfind("}")
        if last_brace == -1:
            return ApplyResult(success=False, message="Could not find where to add imports in config.")
        indent = "  "
        import_block = f"{indent}imports = [{import_line}];\n"
        new = content[:last_brace] + import_block + content[last_brace:]

    # write back atomically
    fd, tmp = tempfile.mkstemp(dir=str(candidate.parent), suffix=".nix")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new)
        os.replace(tmp, candidate)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        return ApplyResult(success=False, message=f"Failed to write {candidate}")
    return ApplyResult(success=True, message=f"Added {import_line} to {candidate}")


def _get_hostname() -> str:
    import socket
    return socket.gethostname()



def format_nix(code: str) -> str:
    try:
        result = subprocess.run(
            ["alejandra", "--quiet"],
            input=code, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return code
