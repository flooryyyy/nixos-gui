# nixos-gui

> An experimental graphical interface for browsing and editing NixOS configuration.

nixos-gui is a local Hyperdiv web application for people who want a visual workflow around NixOS configuration. It reads the available options and current values through `nix eval`, lets you queue changes, shows the pending changes before applying them, then writes Nix code and can run `nixos-rebuild`.

i'm planning to refactor and eventually rewrite this project because i'm not happy with its current state. i'm still keeping it public for anyone who wants to look through it. thank you for staying patient :)

Expect rough edges, changing behavior, and incomplete support for some NixOS option types. Review generated changes before applying them.

## What it can do

| Area | Current behavior |
| --- | --- |
| Package search | Enumerates package metadata from nixpkgs, caches it locally, and ranks results with RapidFuzz. Results include package names, versions, and descriptions. |
| Package changes | Queue package additions and removals without editing the main configuration immediately. |
| Options browser | Browse the NixOS option tree, filter option paths, view types and descriptions, and inspect current values. |
| Option editing | Edit common boolean, string, path, package, hostname, integer, float, list, attribute-set, submodule, and JSON-compatible values. |
| Pending changes | Keep package and option changes in an intent queue. Repeating the same change updates the existing intent rather than adding a duplicate. |
| Diff display | Show inline old and new values for pending option changes. A separate full diff task exists but is not currently wired into the Apply page. |
| Build check | Write the generated configuration and run `nixos-rebuild dry-build`. Failed checks restore files changed by the operation. |
| Apply modes | Save configuration only, run `nixos-rebuild test`, or run `nixos-rebuild switch`. |
| NixOS integration | Flake-based configurations are the primary path. A legacy configuration fallback exists, but it is not reliable for normal `configuration.nix` setups. |
| Themes | Switch between NixOS Blue and Night Purple, with light and dark palettes supplied by Hyperdiv. |

## How the workflow works

```text
browse packages or options
        |
        v
create intents in the pending queue
        |
        v
review the generated changes
        |
        +--> Save      write configuration only
        +--> Test      nixos-rebuild test
        +--> Switch    nixos-rebuild switch
```

The GUI keeps pending intents in JSON state. NixOS configuration remains the source of truth for the system itself. The application does not replace the NixOS module system or maintain a separate database of system configuration.

### Configuration files

The exact output path depends on the configuration directory and whether a wrapper already exists.

| File | Purpose |
| --- | --- |
| `<config-dir>/nixos-gui.nix` | Integration module used by the NixOS configuration. A first write can create this file directly. |
| `<config-dir>/generated.nix` | Generated module used when an existing `nixos-gui.nix` wrapper imports it. |
| `~/.config/nixos-gui/state.json` | Pending intents and the selected activation mode. |

The Settings page can add the `nixos-gui.nix` import to a supported configuration file. You can also add it yourself to your `imports` list if you prefer to control the change manually.

## Requirements

| Requirement | Why it is needed |
| --- | --- |
| NixOS | The configuration reader expects the system Nix tools and a NixOS configuration directory. |
| Python 3.11 or newer | Runtime language. |
| `nix` | Evaluates the flake, options, current values, and package metadata. |
| `nixos-rebuild` | Used by the test and switch modes, and by build checks. |
| `pkexec` | Required for graphical authentication during test and switch operations. Save-only mode does not need it. |
| `alejandra` | Optional dependency reserved for the Nix formatter helper. The active apply path does not currently invoke it. |
| `git` | Optional for ordinary configurations. Used to stage generated files when the NixOS configuration directory is a Git repository, because flakes ignore untracked files. |

The current implementation calls the system Nix binaries at `/run/current-system/sw/bin/`. It is therefore intended for a NixOS host rather than a generic Linux development environment.

## Installation

Clone the repository, create an environment, and install the package:

```sh
git clone https://github.com/flooryyyy/nixos-gui.git
cd nixos-gui

uv venv
. .venv/bin/activate
uv pip install .
```

You can use `pip install .` instead if you already have a suitable Python environment.

## Running

With the virtual environment active:

```sh
python -m nixos_gui
```

The server starts on `http://localhost:8888` by default.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HD_PORT` | `8888` | Change the local Hyperdiv server port. |
| `NIXOSGUI_CONFIG_PATH` | `/etc/nixos` | Point the application at another NixOS configuration directory. |
| `NIXOSGUI_STATE_PATH` | `~/.config/nixos-gui/state.json` | Store pending state somewhere else. |

For example:

```sh
NIXOSGUI_CONFIG_PATH="$HOME/src/nix-config" HD_PORT=9000 python -m nixos_gui
```

The application also loads a `.env` file at the project root when started through `python -m nixos_gui`.

## Applying changes safely

The Apply page has three activation modes.

| Mode | Command or action | Effect |
| --- | --- | --- |
| Save | Write generated configuration | Does not run a rebuild. |
| Test | `pkexec nixos-rebuild test` | Builds and activates the configuration for the current boot. |
| Switch | `pkexec nixos-rebuild switch` | Builds and activates the configuration as the normal system generation. |

The `Check Build` button writes the configuration and runs `nixos-rebuild dry-build`. If that build fails, the application attempts to restore every file it changed. A successful check still leaves the generated configuration written, so inspect the file and your Git diff before switching.

`nixos-gui` can edit Nix files in the configuration directory when removing items from list options. Keep the configuration under version control if possible. The application does not provide an in-app history or system-generation rollback interface.

## Caching

The first evaluation of large NixOS data sets can take time. Cache files live in `~/.cache/nixos-gui/`.

| Cache | Behavior |
| --- | --- |
| Options tree | Stores option names, types, and descriptions. It is keyed by the nixpkgs revision and normally takes about 30 seconds to build on a cold cache. |
| Option values | Stores values keyed by the current system generation. |
| Option metadata | Stores defaults, examples, and descriptions keyed by nixpkgs revision. |
| Package list | Stores package names, versions, and descriptions. A cold fetch can take about 45 seconds; the package cache is reused for up to one week. |

Use `Settings -> Reset Cache` after changing the NixOS configuration or when cached data looks stale.

## Project structure

```text
src/nixos_gui/
├── __main__.py        Load environment and start Hyperdiv
├── app.py             Application shell, sidebar, top bar, and routing
├── state.py           Intent model and JSON persistence
├── styles.py          Theme presets and shared card styles
├── config.py          NixOS detection, nix eval, and option caches
├── search.py          nixpkgs package enumeration and fuzzy search
├── codegen.py         Intent queue to Nix module generation
├── apply.py           Diffs, file writes, bootstrap, and rebuilds
└── pages/
    ├── home.py        Dashboard and pending changes
    ├── search.py      Package search and package intents
    ├── options.py     Options tree, filtering, metadata, and editors
    ├── apply.py       Diff review, build checks, save, test, and switch
    └── settings.py    Import setup, themes, activation mode, and caches
```

## Development

Install the project in editable mode and install the test runner:

```sh
uv venv
. .venv/bin/activate
uv pip install -e .
uv pip install pytest
```

Run the tests with:

```sh
pytest
```

The tests cover the intent state model, JSON persistence, Nix value serialization, generated module structure, diff calculation, bootstrap markers, and file-writing behavior.

## Current limitations

| Limitation | Detail |
| --- | --- |
| Prototype status | The project is being reconsidered and a larger rewrite is planned. There are no stability guarantees yet. |
| Console entry point | The installed `nixos-gui` script currently calls the Hyperdiv app outside an active frame. Use `python -m nixos_gui` until that entry point is fixed. |
| NixOS scope | The UI may start elsewhere, but configuration browsing and applying require a working NixOS system and configuration directory. |
| Configuration layout | Flake-based configurations are the reliable path. The legacy `configuration.nix` fallback is incomplete and may not support normal setups. |
| Evaluation cost | Cold option and package evaluations are slow compared with warm-cache searches. |
| Option coverage | Not every NixOS type has a dedicated editor. Some values use the JSON fallback, and some large values are only fetched on demand. |
| Diff preview | Pending option changes have inline old/new displays, but the separate full diff task is not connected to the Apply page. |
| Formatter integration | `alejandra` support exists as a helper, but the active apply path does not currently invoke it. |
| Architecture portability | Flake package evaluation currently selects the `x86_64-linux` package set. Other architectures may need additional work. |
| System changes | Switch and test modes can change the running system. Generated Nix should be reviewed before approval. |
| Rollback UI | Failed writes and failed rebuilds attempt file restoration, but there is no GUI for browsing or rolling back NixOS generations. |

## Feedback

If you try it, the most useful report includes your NixOS version, Python version, configuration layout, the action you took, and the relevant terminal output. Issues and rough edges are expected while the project is in this experimental phase.
