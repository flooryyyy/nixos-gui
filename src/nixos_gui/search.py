# package search - fuzzy matching across nixpkgs packages
import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass

from rapidfuzz import fuzz

from nixos_gui.config import CACHE_DIR

logger = logging.getLogger(__name__)
CACHE_FILE = CACHE_DIR / "packages.json"

_package_list: list[dict] | None = None
_last_fetch_time: float = 0


@dataclass
class PackageResult:
    name: str
    pname: str
    version: str
    description: str
    score: float = 0.0


def _build_nix_expr(config_path: str | None = None) -> str:
    # builds a nix expression that extracts name/pname/version/description for every package in nixpkgs
    # config_path selects flake vs channel mode for the package set reference
    if config_path:
        pkgs_ref = f'(builtins.getFlake "{config_path}").inputs.nixpkgs.legacyPackages.x86_64-linux'
    else:
        pkgs_ref = "import <nixpkgs> {}"
    return f"""
    let
      pkgs = {pkgs_ref};
      allPkgs = builtins.attrNames pkgs;
      getInfo = n:
        let r = builtins.tryEval (
          if pkgs.${{n}} ? meta && pkgs.${{n}}.meta ? description
          then {{
            name = n;
            pname = pkgs.${{n}}.pname or n;
            version = pkgs.${{n}}.version or "";
            description = pkgs.${{n}}.meta.description or "";
          }}
          else null
        ); in if r.success then r.value else null;
      allInfo = builtins.map getInfo allPkgs;
    in
      builtins.filter (x: x != null) allInfo
    """


def _fetch_package_list(config_path: str | None = None) -> list[dict]:
    # runs the nix expression to enumerate all nixpkgs packages, caches result to disk
    # takes ~45s on first run, subsequent loads read from cache
    global _last_fetch_time
    logger.info("Fetching package list from nixpkgs (this may take ~45s)...")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with subprocess.Popen(
        ["/run/current-system/sw/bin/nix", "eval", "--impure", "--json",
         "--file", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        stdout, stderr = proc.communicate(input=_build_nix_expr(config_path), timeout=120)
    if proc.returncode != 0:
        logger.error("Failed to fetch package list: %s", stderr[:500])
        return []
    try:
        packages = json.loads(stdout)
    except json.JSONDecodeError:
        logger.error("Failed to parse package list JSON")
        return []
    CACHE_FILE.write_text(json.dumps(packages))
    _last_fetch_time = time.time()
    logger.info("Fetched %d packages", len(packages))
    return packages


def _load_cached() -> list[dict] | None:
    # returns cached package list if it exists and is <1 week old, else None
    if not CACHE_FILE.exists():
        return None
    mtime = CACHE_FILE.stat().st_mtime
    if time.time() - mtime > 604800:  # 1 week
        return None
    try:
        return json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _ensure_package_list(config_path: str | None = None):
    # loads the package list from cache or fetches it if missing/stale
    global _package_list, _last_fetch_time
    if _package_list is not None:
        return
    cached = _load_cached()
    if cached:
        _package_list = cached
        _last_fetch_time = CACHE_FILE.stat().st_mtime
        logger.debug("Loaded %d packages from cache", len(_package_list))
    else:
        _package_list = _fetch_package_list(config_path)


# kick off a background load at import time so the first /search?q=… in a fresh process is fast
# without this, the very first search after `nixos-gui` startup parses the 3MB packages.json on the request thread and the user sees a 1-3s spinner even though the cache file is on disk
def _background_preload():
    try:
        _ensure_package_list()
    except Exception as e:
        logger.debug("Background package preload failed: %s", e)


_preload_thread = threading.Thread(target=_background_preload, daemon=True)
_preload_thread.start()


def search_packages(query: str, limit: int = 20, config_path: str | None = None) -> list[PackageResult]:
    # fuzzy search across all nixpkgs packages, returns ranked results
    _ensure_package_list(config_path)
    if not _package_list or not query.strip():
        return []
    q = query.strip().lower()

    # pre-filter - cheap substring match first (~6x faster)
    candidates = [
        pkg for pkg in _package_list
        if q in (pkg.get("name") or "").lower()
        or q in (pkg.get("pname") or "").lower()
        or q in (pkg.get("description") or "").lower()
    ]
    # fall back to full list if pre-filter is too aggressive
    if len(candidates) < 5:
        candidates = _package_list

    # score = name match (70%) + description match (30%), exact prefix match boosts to 100
    results: list[PackageResult] = []
    for pkg in candidates:
        name = (pkg.get("name") or "").lower()
        pname = (pkg.get("pname") or "").lower()
        desc = (pkg.get("description") or "").lower()
        name_score = max(fuzz.token_set_ratio(q, name), fuzz.token_set_ratio(q, pname))
        desc_score = fuzz.token_set_ratio(q, desc) if desc else 0
        score = name_score * 0.7 + desc_score * 0.3
        if name.startswith(q) or pname.startswith(q):
            score = max(score, 100.0)
        if score < 30:
            continue
        results.append(PackageResult(
            name=pkg.get("name", "?"),
            pname=pkg.get("pname", pkg.get("name", "?")),
            version=pkg.get("version", ""),
            description=pkg.get("description", ""),
            score=score,
        ))
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]


def invalidate_cache():
    global _package_list, _last_fetch_time
    _package_list = None
    _last_fetch_time = 0
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
    logger.info("Package cache cleared")
