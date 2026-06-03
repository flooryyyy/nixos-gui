# nixos-gui

a web-based GUI for editing NixOS configurations. browse nixpkgs options, search packages, queue changes, and apply them with nixos-rebuild.

## features

- **package search** - fuzzy search across all nixpkgs packages, add/remove to your config
- **options browser** - tree navigation of all NixOS options with type info, descriptions, defaults, and inline editing
- **pending changes** - queue up adds/removes/option edits before applying, with diff preview
- **apply** - dry-run, switch, test, or save-only modes via nixos-rebuild
- **flake + legacy support** - auto-detects flake vs configuration.nix setups
- **caching** - options tree, values, and metadata cached to disk so browsing is fast after first load

## requirements

- NixOS (needs nix, nixos-rebuild, nixos-version)
- Python 3.11+
- [pkexec](https://gitlab.freedesktop.org/polkit/polkit) (for apply operations - GUI apps don't have a tty for sudo)
- [alejandra](https://github.com/kamadorueda/alejandra) (nix formatter, optional - falls back to unformatted if missing)
- git (for staging files in flake repos, optional)

## install

```sh
pip install .
```

or with uv:

```sh
uv pip install .
```

## run

```sh
nixos-gui
```

opens at `http://localhost:8888`. set `HD_PORT` env var to change the port.

set `NIXOSGUI_CONFIG_PATH` to point at your config dir if it's not `/etc/nixos`.

## how it works

nixos-gui writes two files in your config dir:

- `nixos-gui.nix` - wrapper that imports generated.nix (you add this to your imports)
- `generated.nix` - the actual config generated from your queued changes

on first run, the settings page shows an auto-import button that adds `./nixos-gui.nix` to your configuration.nix or flake imports.

options are fetched via `nix eval --json` and cached in `~/.cache/nixos-gui/`. the first options tree load takes ~30s (full nix eval), after that it's instant from cache.

## dev

```sh
pip install -e .
nixos-gui
```

tests:

```sh
pytest
```
