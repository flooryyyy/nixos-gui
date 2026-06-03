# NixOS configuration reader - cached nix eval backend
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".cache" / "nixos-gui"
VALUES_CACHE_FILE = CACHE_DIR / "options-values-cache.json"
META_CACHE_FILE = CACHE_DIR / "options-meta-cache.json"

# marker cached for paths whose nix eval failed or timed out
# prevents infinite retry loops, UI shows "(unavailable)" instead of "(loading...)" forever
_FETCH_FAILED = "_FETCH_FAILED_"


def _atomic_write(path: Path, data: str):
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def _path_exists_in_tree(tree: dict, option_path: str) -> bool:
    # standard version, pages/options.py imports it
    # returns true if option_path exists in the cached options tree
    # the page layer passes its in-memory tree (from options-tree-v2.json) so we can cheaply filter stale paths before sending them to nix
    # falling back to true when the tree isn't available is safe - the nix-side tryEval machinery will still catch missing paths, just with a timeout penalty
    node = tree
    for part in option_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


class NixConfigReader:
    def __init__(self, config_path: str | None = None):
        self.config_path = Path(
            config_path
            or os.environ.get(
                "NIXOSGUI_CONFIG_PATH",
                str(Path("/etc/nixos")),
            )
        ).resolve()
        self.is_nixos = self._check_nixos()
        self._values_cache: dict[str, Any] = {}
        self._values_cache_dirty: bool = False
        # metadata cache keyed by generation key - {gen - {path - meta_dict}}
        self._meta_cache: dict[str, dict[str, dict]] = {}
        self._meta_cache_dirty: bool = False
        self.config_name: str | None = None
        self._load_values_cache()
        self._load_meta_cache()
        if self.is_nixos:
            self._detect_config()


    def _nixpkgs_key(self) -> str | None:
        # returns the nixpkgs revision which controls tree + meta caches
        # for flake systems, reads flake.lock and extracts the nixpkgs input's locked.rev (or narHash as fallback)
        # for channel systems, uses the nixos-version output
        lock = self.config_path / "flake.lock"
        if lock.exists():
            try:
                data = json.loads(lock.read_text())
                nixpkgs = data.get("nodes", {}).get("nixpkgs", {})
                locked = nixpkgs.get("locked", {})
                rev = locked.get("rev")
                if rev:
                    return f"flake-nixpkgs-{rev}"
                nar = locked.get("narHash")
                if nar:
                    return f"flake-nixpkgs-{nar}"
            except (json.JSONDecodeError, OSError):
                pass
        # channel fallback - nixos-version
        try:
            result = subprocess.run(
                ["/run/current-system/sw/bin/nixos-version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                ver = result.stdout.strip()
                if ver:
                    return f"channel-{ver}"
        except Exception:
            pass
        return None

    def _config_key(self) -> str | None:
        # returns a key that changes with every rebuild to control the values cache
        # reads /run/current-system symlink to get the store path
        # falls back to _nixpkgs_key() when /run/current-system doesn't exist (dev env, not booted)
        current_system = Path("/run/current-system")
        if current_system.exists():
            try:
                target = os.readlink(str(current_system))
                return f"config-{target}"
            except OSError:
                pass
        return self._nixpkgs_key()


    def _check_nixos(self) -> bool:
        if not Path("/run/current-system/sw/bin/nix").exists():
            return False
        if not self.config_path.exists():
            return False
        if not ((self.config_path / "flake.nix").exists() or
                (self.config_path / "configuration.nix").exists()):
            return False
        return True

    def _detect_config(self):
        import socket
        hostname = socket.gethostname()
        # try flake-based detection first (works for flake.nix configs)
        try:
            raw = self._nix_eval(
                f'builtins.attrNames (builtins.getFlake "{self.config_path}").nixosConfigurations',
            )
            result = json.loads(raw) if raw else None
            if isinstance(result, list) and result:
                if hostname in result:
                    self.config_name = hostname
                else:
                    self.config_name = result[0]
                logger.info("Detected NixOS config (flake): %s", self.config_name)
                return
        except Exception as e:
            logger.debug("Flake detection failed: %s", e)
        # fallback - import-based detection (for non-flake configs)
        try:
            raw = self._nix_eval(
                'builtins.attrNames (import "${config_dir}" {}).nixosConfigurations or {}'
                .replace("${config_dir}", str(self.config_path)),
            )
            result = json.loads(raw) if raw else None
            if isinstance(result, list) and result:
                if hostname in result:
                    self.config_name = hostname
                else:
                    self.config_name = result[0]
                logger.info("Detected NixOS config (import): %s", self.config_name)
        except Exception as e:
            logger.warning("Could not detect NixOS config name: %s: disabling NixOS features", e)
            self.is_nixos = False

    def _nix_eval(self, expr: str, timeout: int = 30) -> str:
        # runs `nix eval --json --expr`. returns raw JSON string or '' on failure
        cmd = [
            "/run/current-system/sw/bin/nix", "eval",
            "--impure",
            "--json",
            "--expr", expr,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, cwd=str(self.config_path),
            )
        except subprocess.TimeoutExpired:
            logger.error("nix eval timed out after %ds: %s", timeout, expr[:120])
            return ""
        except FileNotFoundError:
            logger.error("nix binary not found")
            return ""

        if result.returncode != 0:
            logger.debug("nix eval failed: %s", result.stderr[:300])
            return ""

        return result.stdout.strip()


    # paths that force nixpkgs eval or produce massive output - too expensive for tree browsing
    # suffix-matched against full option paths
    _HEAVY_PATH_SUFFIXES = (
        ".binsh", ".package", ".packages",
        # large nested attrsets / package sets that produce huge JSON
        ".devices",            # disko.devices: full disk layout tree
        ".checkScripts",       # disko.checkScripts: generated scripts
        ".kernelPackages",     # boot.kernelPackages: full nixpkgs set
    )

    def is_heavy(self, path: str) -> bool:
        # returns true if path ends with a heavy-path suffix
        return path.endswith(self._HEAVY_PATH_SUFFIXES)


    def get_options_batch(self, option_paths: list[str], allow_heavy: bool = False) -> dict[str, Any]:
        # fetch option values from cache, fall back to nix for misses
        # fast path - if all cached, skip disk and subprocesses
        # allow_heavy=False (default) skips _HEAVY_PATH_SUFFIXES so bg fetches stay fast
        # pass true for on-demand single-path fetches (e.g. View click) - result cached, cost paid once per flake rev
        if not self.is_nixos or not self.config_name or not option_paths:
            return {}
        if allow_heavy:
            light = option_paths
        else:
            light = [p for p in option_paths if not self.is_heavy(p)]
        if not light:
            return {}

        out: dict[str, Any] = {}
        missing: list[str] = []
        for p in light:
            cached = self._values_cache.get(p)
            if cached is not None or p in self._values_cache:
                out[p] = cached
            else:
                missing.append(p)

        # fast path - everything cached, no nix call needed
        if not missing:
            return out

        fetched = self._fetch_options_batch(missing)
        for k, v in fetched.items():
            self._values_cache[k] = v
            out[k] = v
        # cache paths that weren't returned (eval failed / timed out) as _FETCH_FAILED so they're not retried on every render
        # without this, the values_task re-runs indefinitely for stuck paths, and the UI shows "(loading...)" forever
        failed = [p for p in missing if p not in fetched]
        for p in failed:
            self._values_cache[p] = _FETCH_FAILED
        if fetched or failed:
            self._values_cache_dirty = True
            self._save_values_cache()

        return out

    def _fetch_options_batch(self, option_paths: list[str]) -> dict[str, Any]:
        # raw nix eval for missing option values, chunked and isolated
        flake = f'(builtins.getFlake "{self.config_path}").nixosConfigurations.{self.config_name}'
        out: dict[str, Any] = {}
        # keep chunks at 20 - each chunk spawns a separate nix eval subprocess that re-evaluates the flake from scratch, so smaller chunks = more subprocesses = slower
        # 45s timeout (up from 20s) gives headroom for chunks with large listOf outputs
        CHUNK_SIZE = 20
        for chunk_idx in range(0, len(option_paths), CHUNK_SIZE):
            chunk = option_paths[chunk_idx:chunk_idx + CHUNK_SIZE]
            entries = []
            for p in chunk:
                nix_access = ".".join(p.split("."))
                entries.append(
                    f'"{p}" = let r = builtins.tryEval (builtins.toJSON ({flake}.config.{nix_access}));'
                    f' in if r.success then r.value else null;'
                )
            expr = "{ " + " ".join(entries) + " }"
            raw = self._nix_eval(expr, timeout=45)
            if not raw:
                continue
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(result, dict):
                continue
            for k, v in result.items():
                if isinstance(v, str):
                    try:
                        out[k] = json.loads(v)
                    except (json.JSONDecodeError, TypeError):
                        out[k] = v
                else:
                    out[k] = v
        return out

    def get_cached_value(self, option_path: str) -> Any:
        # returns a single cached value, or None if not cached
        return self._values_cache.get(option_path)

    def _load_values_cache(self):
        try:
            if VALUES_CACHE_FILE.exists():
                data = json.loads(VALUES_CACHE_FILE.read_text())
                current_gen = self._config_key()
                if data.get("_generation") == current_gen:
                    self._values_cache = data.get("values", {})
                    logger.info("Loaded values cache (gen: %s, %d entries)",
                                current_gen[:40] if current_gen else "?",
                                len(self._values_cache))
                    return
                else:
                    logger.info("Values cache generation mismatch, rebuilding")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Corrupt values cache: %s", e)
        self._values_cache = {}

    def _save_values_cache(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "_generation": self._config_key(),
            "values": self._values_cache,
        }
        _atomic_write(VALUES_CACHE_FILE, json.dumps(data, indent=2, default=str))
        self._values_cache_dirty = False


    def get_option_meta(self, option_path: str, allow_heavy: bool = False) -> dict[str, Any]:
        # fetches safe metadata (default, example, description) for one option
        # when allow_heavy=True the heavy-path filter is bypassed - used for on-demand fetches when the user explicitly
        # clicks View on a heavy option like disko.devices
        if allow_heavy:
            # bypass the batch filter - fetch directly for this one path
            return self._fetch_meta_batch([option_path]).get(option_path, {})
        return self.get_options_meta_batch([option_path]).get(option_path, {})

    def get_cached_meta(self, option_path: str) -> dict | None:
        # returns cached metadata for an option, or None if not cached
        gen = self._nixpkgs_key() or ""
        return self._meta_cache.get(gen, {}).get(option_path)

    def get_options_meta_batch(self, option_paths: list[str]) -> dict[str, dict]:
        # fetches safe metadata (default, example, description) for many options
        # uses a parallel pool of per-path nix subprocesses
        # each path access forces a bit of module eval (nix caches it), so a thread pool gets good throughput without the nixpkgs-eval cliff a single big expression would hit
        # cache hits skip the nix call entirely
        # heavy paths (package-typed) are filtered out up front - fetching them would force a full nixpkgs eval
        if not self.is_nixos or not self.config_name or not option_paths:
            return {}
        gen = self._nixpkgs_key() or ""
        cache = self._meta_cache.setdefault(gen, {})
        # heavy paths are skipped - same reason as get_options_batch
        light = [p for p in option_paths if not self.is_heavy(p)]
        # dedupe within the request; preserve order
        seen: set[str] = set()
        todo: list[str] = []
        for p in light:
            if p in cache or p in seen:
                continue
            seen.add(p)
            todo.append(p)
        if not todo:
            return {p: cache[p] for p in option_paths if p in cache}
        fetched = self._fetch_meta_batch(todo)
        if fetched:
            cache.update(fetched)
            self._save_meta_cache()
        return {p: cache[p] for p in option_paths if p in cache}

    def _fetch_meta_batch(self, option_paths: list[str]) -> dict[str, dict]:
        # fetches meta for many options using batched nix eval calls
        # 30 options per nix eval subprocess
        # each entry is wrapped in builtins.tryEval so a stale/missing path returns null instead of aborting the whole batch
        # this is dramatically faster than spawning one subprocess per option (the old _fetch_meta_parallel approach) because each subprocess re-evaluates the flake from scratch
        flake = (
            f'(builtins.getFlake "{self.config_path}").'
            f'nixosConfigurations.{self.config_name}'
        )
        out: dict[str, dict] = {}
        CHUNK_SIZE = 30
        for chunk_idx in range(0, len(option_paths), CHUNK_SIZE):
            chunk = option_paths[chunk_idx:chunk_idx + CHUNK_SIZE]
            entries = []
            for p in chunk:
                nix_access = ".".join(p.split("."))
                entries.append(
                    f'"{p}" = let r = builtins.tryEval '
                    f'(getMeta ({flake}.options.{nix_access}));'
                    f' in if r.success then r.value else null;'
                )
            expr = (
                "let "
                "safe = opt: attr: "
                "let r = builtins.tryEval (builtins.toJSON (opt.${attr} or null)); "
                "in if r.success then r.value else null; "
                "getMeta = opt: { "
                'default = safe opt "default"; '
                'example = safe opt "example"; '
                'description = safe opt "description"; '
                "}; "
                "in { " + " ".join(entries) + " }"
            )
            raw = self._nix_eval(expr, timeout=45)
            if not raw:
                continue
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(result, dict):
                continue
            for path, meta in result.items():
                if not isinstance(meta, dict):
                    continue
                parsed: dict[str, Any] = {}
                for k, v in meta.items():
                    if isinstance(v, str) and v:
                        try:
                            parsed[k] = json.loads(v)
                        except (json.JSONDecodeError, TypeError):
                            parsed[k] = v
                    else:
                        parsed[k] = v
                out[path] = parsed
        return out

    pass  # _path_exists_in_tree is now a module-level function

    def _load_meta_cache(self):
        try:
            if META_CACHE_FILE.exists():
                self._meta_cache = json.loads(META_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Corrupt meta cache: %s", e)
            self._meta_cache = {}

    def _save_meta_cache(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(META_CACHE_FILE, json.dumps(self._meta_cache, indent=2, default=str))
        self._meta_cache_dirty = False


    def get_packages(self) -> list[str]:
        if not self.is_nixos or not self.config_name:
            return []
        try:
            raw = self._nix_eval(
                f"let pkgs = (builtins.getFlake \"{self.config_path}\").inputs"
                f".nixpkgs.legacyPackages.x86_64-linux; "
                f"cfg = (builtins.getFlake \"{self.config_path}\").nixosConfigurations"
                f".{self.config_name}.config; in "
                f"map (p: p.pname or p.name or \"?\") cfg.environment.systemPackages"
            )
            result = json.loads(raw) if raw else None
            if isinstance(result, list):
                return result
        except Exception as e:
            logger.warning("Could not read packages: %s", e)
        return []

    def get_enabled_programs(self) -> list[str]:
        # gets names of programs/services enabled via the module system
        # catches packages enabled via `programs.<name>.enable = true` or `services.<name>.enable = true` that don't appear in environment.systemPackages
        if not self.is_nixos or not self.config_name:
            return []
        try:
            raw = self._nix_eval(
                f'let cfg = (builtins.getFlake "{self.config_path}")'
                f'.nixosConfigurations.{self.config_name}.config; in'
                f' builtins.attrNames (builtins.filterAttrs'
                f' (n: v: v.enable or false) cfg.programs)'
                f' ++ builtins.attrNames (builtins.filterAttrs'
                f' (n: v: v.enable or false) cfg.services)'
            )
            result = json.loads(raw) if raw else None
            if isinstance(result, list):
                return result
        except Exception as e:
            logger.warning("Could not read enabled programs: %s", e)
        return []

    def get_hostname(self) -> str:
        if not self.is_nixos:
            import socket
            return socket.gethostname()
        return self.config_name or "unknown"

    def get_package_counts(self) -> dict[str, int]:
        if not self.is_nixos or not self.config_name:
            return {"system": 0}
        try:
            raw = self._nix_eval(
                f'let pkgs = (builtins.getFlake "{self.config_path}").inputs'
                f'.nixpkgs.legacyPackages.x86_64-linux; '
                f'count = builtins.length '
                f'(builtins.getFlake "{self.config_path}").nixosConfigurations'
                f'.{self.config_name}.config.environment.systemPackages; '
                f'in count'
            )
            result = json.loads(raw) if raw else None
            if isinstance(result, int):
                return {"system": result}
        except Exception:
            pass
        return {"system": 0}


    def invalidate(self, changed_paths: list[str] | None = None):
        # clears cached values
        # when changed_paths is provided, delete only those entries from the values cache (selective invalidation after GUI apply)
        # when None, clear the entire values cache
        if changed_paths is not None:
            for p in changed_paths:
                self._values_cache.pop(p, None)
            self._values_cache_dirty = True
            self._save_values_cache()
        else:
            self._values_cache.clear()
            self._values_cache_dirty = False
            self._meta_cache.clear()
            self._meta_cache_dirty = False
            if VALUES_CACHE_FILE.exists():
                VALUES_CACHE_FILE.unlink()
            if META_CACHE_FILE.exists():
                META_CACHE_FILE.unlink()


    def pre_fetch_config_values(self):
        # fetches values for paths set in the user's current config
        # after the tree loads, the config reader already knows which option paths are set in the user's config (configuration.nix / generated.nix)
        # pre-fetching those ~20-50 paths makes the "what does my current config look like" view instant
        if not self.is_nixos or not self.config_name:
            return
        config_paths = []
        for name in ("generated.nix", "nixos-gui.nix"):
            f = self.config_path / name
            if f.exists():
                try:
                    content = f.read_text()
                    import re
                    for m in re.finditer(r'^\s*([\w.]+)\s*=', content, re.MULTILINE):
                        path = m.group(1)
                        if path and not path.startswith("{"):
                            config_paths.append(path)
                except OSError:
                    pass
        if config_paths:
            try:
                self.get_options_batch(config_paths)
            except Exception as e:
                logger.debug("Pre-fetch config values failed: %s", e)
