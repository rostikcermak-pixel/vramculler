#!/usr/bin/env python3
"""
vramculler - reduce VRAM pressure for installed Steam games via known-safe,
per-engine config tweaks.

It edits *human-readable* game config files for engines that expose documented,
safe VRAM knobs (Unreal Engine texture-streaming pool size, Source texture
quality). It does NOT touch binaries, archives, shaders, or proprietary
formats. It is NOT a texture compressor and is unrelated to NVIDIA NTC.

Honest effect: it reduces VRAM *pressure*. FPS gains happen ONLY when a game was
exceeding VRAM and spilling to system RAM (stutter / thrashing). Games that
already fit in VRAM will see no FPS change.

Pure stdlib, Python 3.10+. Optional: `rich` for prettier output (degrades
gracefully to ANSI when absent).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

__version__ = "0.1.0"

# --------------------------------------------------------------------------- #
# Optional rich
# --------------------------------------------------------------------------- #
try:
    from rich.console import Console as _RichConsole  # type: ignore
    from rich.table import Table as _RichTable  # type: ignore
    from rich import box as _rich_box  # type: ignore

    _HAVE_RICH = True
except Exception:  # pragma: no cover - rich is optional
    _HAVE_RICH = False


# --------------------------------------------------------------------------- #
# Profiles (single editable dict). Tuned for a 4 GB card.
#   ue_pool_size_mb         -> r.Streaming.PoolSize (texture streaming budget)
#   ue_limit_to_vram        -> r.Streaming.LimitPoolSizeToVRAM (engine clamps to
#                              actual VRAM; pool size becomes an upper bound)
#   source_mat_picmip       -> mat_picmip (0 high .. 2 low). In-game-equivalent
#                              texture quality. No VAC-relevant cvars are used.
# --------------------------------------------------------------------------- #
PROFILES: dict[str, dict[str, Any]] = {
    "conservative": {
        "ue_pool_size_mb": 1500,
        "ue_limit_to_vram": True,
        "source_mat_picmip": 1,
    },
    "balanced": {
        "ue_pool_size_mb": 1200,
        "ue_limit_to_vram": True,
        "source_mat_picmip": 1,
    },
    "aggressive": {
        "ue_pool_size_mb": 800,
        "ue_limit_to_vram": True,
        "source_mat_picmip": 2,
    },
}
DEFAULT_PROFILE = "balanced"


# --------------------------------------------------------------------------- #
# Synthwave palette / console
# --------------------------------------------------------------------------- #
class C:
    """ANSI synthwave palette. Disabled automatically when not a TTY."""

    enabled = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    PINK = "\033[38;5;205m"
    MAGENTA = "\033[38;5;201m"
    PURPLE = "\033[38;5;141m"
    CYAN = "\033[38;5;51m"
    BLUE = "\033[38;5;39m"
    GREEN = "\033[38;5;48m"
    YELLOW = "\033[38;5;227m"
    RED = "\033[38;5;203m"
    GREY = "\033[38;5;245m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    @classmethod
    def wrap(cls, text: str, *codes: str) -> str:
        if not cls.enabled:
            return text
        return "".join(codes) + text + cls.RESET


class Console:
    """Thin output wrapper. Uses rich if available, else ANSI."""

    def __init__(self, use_rich: bool = True, quiet: bool = False):
        self.use_rich = bool(use_rich and _HAVE_RICH)
        self._rich = _RichConsole() if self.use_rich else None
        self.quiet = quiet

    def print(self, text: str = "") -> None:
        print(text)

    def banner(self) -> None:
        art = [
            r"  __   ___ __ _ _ __ ___   ___ _   _| | | ___ _ __ ",
            r"  \ \ / / '__/ _` | '_ ` _ \ / __| | | | | |/ _ \ '__|",
            r"   \ V /| | | (_| | | | | | | (__| |_| | | |  __/ |   ",
            r"    \_/ |_|  \__,_|_| |_| |_|\___|\__,_|_|_|\___|_|   ",
        ]
        for i, line in enumerate(art):
            shade = [C.MAGENTA, C.PINK, C.PURPLE, C.CYAN][i % 4]
            self.print(C.wrap(line, C.BOLD, shade))
        self.print(
            C.wrap("  VRAM pressure reducer for Steam games", C.DIM, C.CYAN)
        )
        self.print()

    def rule(self, title: str = "") -> None:
        bar = "═" * 64
        if title:
            self.print(C.wrap(f"── {title} ", C.BOLD, C.PURPLE) + C.wrap("─" * max(0, 60 - len(title)), C.PURPLE))
        else:
            self.print(C.wrap(bar, C.PURPLE))

    def info(self, msg: str) -> None:
        self.print(C.wrap("• ", C.CYAN) + msg)

    def good(self, msg: str) -> None:
        self.print(C.wrap("✔ ", C.GREEN) + msg)

    def warn(self, msg: str) -> None:
        self.print(C.wrap("! ", C.YELLOW) + C.wrap(msg, C.YELLOW))

    def err(self, msg: str) -> None:
        self.print(C.wrap("✘ ", C.RED) + C.wrap(msg, C.RED))

    def debug(self, msg: str) -> None:
        if self.quiet:
            return
        self.print(C.wrap("    ┄ " + msg, C.DIM, C.GREY))


# --------------------------------------------------------------------------- #
# Valve KeyValues (VDF) parser - good enough for libraryfolders.vdf / *.acf
# --------------------------------------------------------------------------- #
_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict[str, Any]:
    """Parse a (text) Valve KeyValues document into nested dicts.

    Duplicate keys keep the last value (sufficient for our use). Comment lines
    starting with // are stripped.
    """
    # strip // comments (not inside quotes - acf/vdf comments are line-level)
    cleaned_lines = []
    for line in text.splitlines():
        # only strip when // is not within a quoted string; cheap heuristic
        if line.lstrip().startswith("//"):
            continue
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)

    tokens = []
    for m in _VDF_TOKEN.finditer(text):
        if m.group(2):
            tokens.append(m.group(2))
        else:
            tokens.append(m.group(1).replace(r"\\", "\\").replace(r"\"", '"'))

    pos = 0

    def parse_obj() -> dict[str, Any]:
        nonlocal pos
        obj: dict[str, Any] = {}
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                pos += 1
                return obj
            key = tok
            pos += 1
            if pos >= len(tokens):
                break
            nxt = tokens[pos]
            if nxt == "{":
                pos += 1
                obj[key] = parse_obj()
            else:
                obj[key] = nxt
                pos += 1
        return obj

    # top level may begin with a root key then a brace
    if not tokens:
        return {}
    if tokens[0] == "{":
        pos = 1
        return parse_obj()
    # root key + object
    root_key = tokens[0]
    pos = 1
    if pos < len(tokens) and tokens[pos] == "{":
        pos += 1
        return {root_key: parse_obj()}
    return {root_key: tokens[pos] if pos < len(tokens) else ""}


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class GameInfo:
    appid: str
    name: str
    install_dir: Path
    library: Path
    runtime: str = "native"  # "native" | "proton"
    prefix: Optional[Path] = None  # compatdata/<appid>/pfx for proton
    engine: str = "unknown"  # unreal | source | source2 | unity | unknown
    engine_detail: str = ""  # e.g. UE project name, source mod dir
    config_target: Optional[Path] = None  # file we would touch
    action: str = "skip"  # applied | already | dry | skip | fail
    effect: str = ""
    backup: Optional[Path] = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# OS / runtime helpers
# --------------------------------------------------------------------------- #
def host_os() -> str:
    s = platform.system()
    if s == "Windows":
        return "windows"
    if s == "Linux":
        return "linux"
    if s == "Darwin":
        return "darwin"
    return s.lower()


def detect_linux_distro() -> str:
    try:
        data = Path("/etc/os-release").read_text(encoding="utf-8")
        m = re.search(r"^PRETTY_NAME=\"?(.*?)\"?$", data, re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "Linux"


def detect_user_shell() -> str:
    sh = os.environ.get("SHELL", "")
    return Path(sh).name if sh else ""


# --------------------------------------------------------------------------- #
# Steam path probing
# --------------------------------------------------------------------------- #
def steam_path_candidates() -> list[Path]:
    osname = host_os()
    cands: list[Path] = []
    home = Path.home()
    if osname == "windows":
        for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
            base = os.environ.get(env)
            if base:
                cands.append(Path(base) / "Steam")
        cands.append(Path("C:/Program Files (x86)/Steam"))
    elif osname == "linux":
        # Respect a customized XDG_DATA_HOME (some Arch/CachyOS setups move it).
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            cands.append(Path(xdg_data) / "Steam")
        cands += [
            home / ".local/share/Steam",            # native pkg (Arch/CachyOS, Fedora, Debian/Ubuntu)
            home / ".steam/steam",                  # symlink -> the above on most distros
            home / ".steam/root",
            home / ".steam/debian-installation",    # older Debian/Ubuntu .deb layout
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",  # Flatpak
            home / ".var/app/com.valvesoftware.Steam/data/Steam",          # Flatpak (older)
            home / "snap/steam/common/.local/share/Steam",                 # Ubuntu Snap
        ]
    elif osname == "darwin":
        cands.append(home / "Library/Application Support/Steam")
    # de-dupe, keep order
    seen: set[str] = set()
    out: list[Path] = []
    for c in cands:
        key = str(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def is_steam_root(p: Path) -> bool:
    return (p / "steamapps").is_dir() or (p / "config" / "libraryfolders.vdf").is_file()


def probe_steam_path(console: Console) -> Optional[Path]:
    found = []
    for c in steam_path_candidates():
        if c.exists():
            tag = "steam root" if is_steam_root(c) else "exists (no steamapps yet)"
            console.debug(f"probe: {c}  [{tag}]")
            if is_steam_root(c):
                found.append(c)
        else:
            console.debug(f"probe: {c}  [absent]")
    return found[0] if found else None


# --------------------------------------------------------------------------- #
# Library + game enumeration
# --------------------------------------------------------------------------- #
def find_libraryfolders_vdf(steam_root: Path) -> Optional[Path]:
    for rel in ("config/libraryfolders.vdf", "steamapps/libraryfolders.vdf"):
        p = steam_root / rel
        if p.is_file():
            return p
    return None


def enumerate_libraries(steam_root: Path, console: Console) -> list[Path]:
    libs: list[Path] = []
    # the steam root itself is always a library
    if (steam_root / "steamapps").is_dir():
        libs.append(steam_root)
    vdf = find_libraryfolders_vdf(steam_root)
    if vdf:
        console.debug(f"libraryfolders.vdf: {vdf}")
        try:
            data = parse_vdf(vdf.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            console.warn(f"could not parse {vdf}: {e}")
            data = {}
        root = data.get("libraryfolders", data)
        if isinstance(root, dict):
            for key, val in root.items():
                if isinstance(val, dict) and "path" in val:
                    libs.append(Path(val["path"]))
                elif isinstance(val, str) and key.isdigit():
                    libs.append(Path(val))
    else:
        console.warn("no libraryfolders.vdf found; using steam root only")
    # de-dupe preserving order, keep only those with steamapps
    seen: set[str] = set()
    out: list[Path] = []
    for lib in libs:
        try:
            rp = lib.resolve()
        except Exception:
            rp = lib
        key = str(rp)
        if key in seen:
            continue
        seen.add(key)
        if (lib / "steamapps").is_dir():
            out.append(lib)
        else:
            console.debug(f"library listed but no steamapps dir: {lib}")
    return out


def enumerate_games(libraries: Iterable[Path], console: Console) -> list[GameInfo]:
    games: list[GameInfo] = []
    for lib in libraries:
        steamapps = lib / "steamapps"
        for acf in sorted(steamapps.glob("appmanifest_*.acf")):
            try:
                data = parse_vdf(acf.read_text(encoding="utf-8", errors="replace"))
            except Exception as e:
                console.warn(f"could not parse {acf.name}: {e}")
                continue
            state = data.get("AppState", {})
            if not isinstance(state, dict):
                continue
            appid = state.get("appid", acf.stem.split("_", 1)[-1])
            name = state.get("name", f"app {appid}")
            installdir = state.get("installdir", "")
            if not installdir:
                continue
            install_path = steamapps / "common" / installdir
            if not install_path.is_dir():
                console.debug(f"appid {appid} ({name}): install dir missing -> {install_path}")
                continue
            gi = GameInfo(
                appid=str(appid),
                name=name,
                install_dir=install_path,
                library=lib,
            )
            detect_runtime(gi, steamapps)
            games.append(gi)
    return games


def detect_runtime(gi: GameInfo, steamapps: Path) -> None:
    """Native vs Proton. Proton games have a compatdata/<appid>/pfx prefix."""
    if host_os() == "windows":
        gi.runtime = "native"
        return
    compat = steamapps / "compatdata" / gi.appid / "pfx"
    if compat.is_dir():
        gi.runtime = "proton"
        gi.prefix = compat
    else:
        gi.runtime = "native"


# --------------------------------------------------------------------------- #
# Engine detection
# --------------------------------------------------------------------------- #
def _iter_top_dirs(p: Path) -> list[Path]:
    try:
        return [c for c in p.iterdir() if c.is_dir()]
    except Exception:
        return []


def detect_engine(gi: GameInfo) -> None:
    root = gi.install_dir
    # ---- Unreal Engine: a project dir sibling to Engine/, containing
    #      Binaries + Content. Packaged UE games have no .uproject. ----
    has_engine_dir = (root / "Engine").is_dir()
    ue_project = None
    for d in _iter_top_dirs(root):
        if d.name == "Engine":
            continue
        if (d / "Content").is_dir() and (d / "Binaries").is_dir():
            ue_project = d.name
            break
    if ue_project is None:
        # some games ship Content/Paks without a Binaries dir at that level
        for d in _iter_top_dirs(root):
            if d.name == "Engine":
                continue
            if (d / "Content" / "Paks").is_dir():
                ue_project = d.name
                break
    if ue_project or has_engine_dir:
        gi.engine = "unreal"
        gi.engine_detail = ue_project or "(unknown project name)"
        return

    # ---- Source 2 (CS2, Dota2 reborn): game/<mod>/gameinfo.gi ----
    for gi_file in root.glob("game/*/gameinfo.gi"):
        gi.engine = "source2"
        gi.engine_detail = gi_file.parent.name
        return
    if (root / "game" / "bin" / "win64").is_dir() or (root / "game" / "bin" / "linuxsteamrt64").is_dir():
        gi.engine = "source2"
        gi.engine_detail = "(source2)"
        return

    # ---- Source 1: gameinfo.txt in a mod dir ----
    for gi_file in list(root.glob("*/gameinfo.txt"))[:1]:
        gi.engine = "source"
        gi.engine_detail = gi_file.parent.name
        return
    if (root / "hl2.exe").exists() or (root / "hl2_linux").exists():
        gi.engine = "source"
        gi.engine_detail = "(source)"
        return

    # ---- Unity: <Game>_Data with managed assemblies / UnityPlayer ----
    if (root / "UnityPlayer.dll").exists():
        gi.engine = "unity"
        return
    for d in _iter_top_dirs(root):
        if d.name.endswith("_Data") and (d / "globalgamemanagers").exists():
            gi.engine = "unity"
            gi.engine_detail = d.name
            return

    gi.engine = "unknown"


# --------------------------------------------------------------------------- #
# Config target resolution (the cross-platform hard part)
# --------------------------------------------------------------------------- #
def _local_appdata_dir_for(gi: GameInfo) -> Optional[Path]:
    """Return the LocalAppData base where UE per-user config lives.

    Windows native: %LOCALAPPDATA%.
    Linux + Proton: <prefix>/drive_c/users/steamuser/AppData/Local.
    """
    if host_os() == "windows":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base)
        return Path.home() / "AppData" / "Local"
    if gi.runtime == "proton" and gi.prefix:
        return gi.prefix / "drive_c" / "users" / "steamuser" / "AppData" / "Local"
    return None


def resolve_unreal_engine_ini(gi: GameInfo) -> Optional[Path]:
    """Resolve the Engine.ini that the packaged game reads user overrides from.

    UE reads user config from:
      <LocalAppData>/<Project>/Saved/Config/<Platform>/Engine.ini
    Platform dir is 'Windows' (UE5) or 'WindowsNoEditor' (UE4) under Windows
    runtime, 'Linux'/'LinuxNoEditor' for native Linux builds.

    We prefer an already-existing Engine.ini; otherwise we choose a sane
    default path to create.
    """
    project = gi.engine_detail
    if not project or project.startswith("("):
        return None

    candidates: list[Path] = []

    if host_os() == "windows" or gi.runtime == "proton":
        base = _local_appdata_dir_for(gi)
        if base:
            for platform_dir in ("Windows", "WindowsNoEditor"):
                candidates.append(base / project / "Saved" / "Config" / platform_dir / "Engine.ini")
    else:
        # native Linux UE build
        home = Path.home()
        for cfgbase in (home / ".config" / "Epic", home):
            for platform_dir in ("Linux", "LinuxNoEditor"):
                candidates.append(cfgbase / project / "Saved" / "Config" / platform_dir / "Engine.ini")
        # also the in-install saved config some packaged builds use
        candidates.append(gi.install_dir / project / "Saved" / "Config" / "Linux" / "Engine.ini")

    # prefer existing
    for c in candidates:
        if c.is_file():
            return c
    # else return the first (preferred default) to be created
    return candidates[0] if candidates else None


def resolve_source_autoexec(gi: GameInfo) -> Optional[Path]:
    """Source 1 autoexec.cfg lives in <install>/<moddir>/cfg/autoexec.cfg.

    These are inside the game install (not the prefix), so the path is the same
    regardless of OS / Proton.
    """
    root = gi.install_dir
    moddir = gi.engine_detail
    if moddir and not moddir.startswith("("):
        cfgdir = root / moddir / "cfg"
        return cfgdir / "autoexec.cfg"
    # fall back: find any existing */cfg dir
    for cfg in root.glob("*/cfg"):
        if cfg.is_dir():
            return cfg / "autoexec.cfg"
    return None


# --------------------------------------------------------------------------- #
# INI editing helpers (line-based, preserves unrelated content)
# --------------------------------------------------------------------------- #
def ini_get_section_values(text: str, section: str) -> dict[str, str]:
    out: dict[str, str] = {}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
            continue
        if cur == section and "=" in s and not s.startswith(";"):
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
    return out


def ini_set_values(text: str, section: str, kv: dict[str, str]) -> str:
    """Set key=value pairs within [section], creating the section/keys as
    needed. Existing keys are updated in place; unrelated lines untouched."""
    lines = text.splitlines()
    # locate section bounds
    sec_start = None
    sec_end = len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        if s == f"[{section}]":
            sec_start = i
            continue
        if sec_start is not None and s.startswith("[") and s.endswith("]"):
            sec_end = i
            break
    remaining = dict(kv)
    if sec_start is None:
        # append a new section at end
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(f"[{section}]")
        for k, v in remaining.items():
            lines.append(f"{k}={v}")
        return "\n".join(lines) + "\n"

    # update existing keys within the section
    for i in range(sec_start + 1, sec_end):
        s = lines[i].strip()
        if "=" in s and not s.startswith(";"):
            k = s.split("=", 1)[0].strip()
            if k in remaining:
                lines[i] = f"{k}={remaining.pop(k)}"
    # insert any leftover keys at end of section
    if remaining:
        insert_at = sec_end
        new_lines = [f"{k}={v}" for k, v in remaining.items()]
        lines[insert_at:insert_at] = new_lines
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Backups / restore
# --------------------------------------------------------------------------- #
BACKUP_RE = re.compile(r"\.bak\.\d{8}-\d{6}$")


def existing_backups(target: Path) -> list[Path]:
    return sorted(target.parent.glob(target.name + ".bak.*"))


def make_backup(target: Path, console: Console, dry: bool) -> Optional[Path]:
    """Copy target to <target>.bak.<timestamp>. Never overwrite the first
    backup: if any backup already exists, keep it and skip making another."""
    prior = existing_backups(target)
    if prior:
        console.debug(f"backup already exists (preserving original): {prior[0].name}")
        return prior[0]
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = target.with_name(target.name + f".bak.{ts}")
    if dry:
        console.debug(f"[dry-run] would back up -> {bak.name}")
        return bak
    bak.write_bytes(target.read_bytes())
    console.debug(f"backup created -> {bak.name}")
    return bak


def restore_all(steam_root: Path, libraries: list[Path], games: list[GameInfo], console: Console, dry: bool) -> int:
    """Revert every change vramculler made.

    Uses the change manifest first: files we *created* are deleted, files we
    *modified* are restored from their earliest (pristine) backup. Then falls
    back to scanning install dirs / prefixes for any stray backups so restore
    still works even if the manifest was lost.
    Returns the count of files reverted.
    """
    restored = 0
    handled: set[str] = set()

    # ---- manifest-driven revert (authoritative) ----
    manifest = load_manifest()
    for key, entry in manifest.items():
        target = Path(key)
        if entry.get("created"):
            if target.exists():
                if dry:
                    console.info(f"[dry-run] would delete created file {target}")
                else:
                    try:
                        target.unlink()
                        console.good(f"deleted created file {target}")
                    except Exception as e:
                        console.err(f"failed to delete {target}: {e}")
                        continue
                restored += 1
            handled.add(str(target))
        else:
            baks = existing_backups(target)
            if baks:
                if dry:
                    console.info(f"[dry-run] would restore {target} from {baks[0].name}")
                else:
                    try:
                        target.write_bytes(baks[0].read_bytes())
                        console.good(f"restored {target} from {baks[0].name}")
                    except Exception as e:
                        console.err(f"failed to restore {target}: {e}")
                        continue
                restored += 1
            handled.add(str(target))

    # ---- fallback: discover any remaining backups by scanning ----
    targets: set[Path] = set()
    for gi in games:
        if gi.engine == "unreal":
            t = resolve_unreal_engine_ini(gi)
            if t:
                targets.add(t)
        elif gi.engine == "source":
            t = resolve_source_autoexec(gi)
            if t:
                targets.add(t)
    # Also discover by scanning for *.bak.* next to known config trees.
    for gi in games:
        search_roots = [gi.install_dir]
        if gi.prefix:
            search_roots.append(gi.prefix / "drive_c")
        for sr in search_roots:
            try:
                for bak in sr.rglob("Engine.ini.bak.*"):
                    targets.add(bak.with_name("Engine.ini"))
                for bak in sr.rglob("autoexec.cfg.bak.*"):
                    targets.add(bak.with_name("autoexec.cfg"))
            except Exception:
                pass

    for target in sorted(targets):
        if str(target) in handled:
            continue
        baks = existing_backups(target)
        if not baks:
            continue
        original = baks[0]  # earliest = pristine
        if dry:
            console.info(f"[dry-run] would restore {target} from {original.name}")
            restored += 1
            continue
        try:
            target.write_bytes(original.read_bytes())
            console.good(f"restored {target} from {original.name}")
            restored += 1
        except Exception as e:
            console.err(f"failed to restore {target}: {e}")
    return restored


# --------------------------------------------------------------------------- #
# Apply per-engine tweaks
# --------------------------------------------------------------------------- #
def _writable(path: Path) -> bool:
    """Is path (or its first existing parent) writable by us?"""
    p = path
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return os.access(p, os.W_OK)


def apply_unreal(gi: GameInfo, profile: dict[str, Any], console: Console, dry: bool) -> None:
    target = resolve_unreal_engine_ini(gi)
    if target is None:
        gi.action = "skip"
        gi.notes.append("could not resolve a UE project / Engine.ini path")
        gi.effect = "none"
        return
    gi.config_target = target
    pool = int(profile["ue_pool_size_mb"])
    limit = "1" if profile["ue_limit_to_vram"] else "0"
    desired = {
        "r.Streaming.PoolSize": str(pool),
        "r.Streaming.LimitPoolSizeToVRAM": limit,
    }
    section = "SystemSettings"

    text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    current = ini_get_section_values(text, section)
    before = {k: current.get(k, "<unset>") for k in desired}

    console.debug(f"engine.ini target: {target}")
    for k in desired:
        console.debug(f"    {k}: {before[k]} -> {desired[k]}")

    if all(current.get(k) == v for k, v in desired.items()):
        gi.action = "already"
        gi.effect = "VRAM pressure already reduced (idempotent)"
        console.good(f"{gi.name}: already applied, nothing to do")
        return

    if not _writable(target):
        gi.action = "skip"
        gi.notes.append("target not writable; skipped (no escalation)")
        gi.effect = "none"
        console.warn(f"{gi.name}: {target} not writable - skipped")
        return

    new_text = ini_set_values(text, section, desired)

    if dry:
        gi.action = "dry"
        gi.effect = "VRAM pressure ↓ (FPS only if it was spilling)"
        gi.backup = make_backup(target, console, dry=True) if target.is_file() else None
        console.info(f"{gi.name}: [dry-run] would write {len(desired)} keys to {target}")
        return

    existed = target.is_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    gi.backup = make_backup(target, console, dry=False) if existed else None
    target.write_text(new_text, encoding="utf-8")
    record_change(target, created=not existed, backup=gi.backup)

    # verify
    verify = ini_get_section_values(target.read_text(encoding="utf-8", errors="replace"), section)
    if all(verify.get(k) == v for k, v in desired.items()):
        gi.action = "applied"
        gi.effect = "VRAM pressure ↓ (FPS only if it was spilling)"
        console.good(f"{gi.name}: applied + verified")
    else:
        gi.action = "fail"
        gi.effect = "verify FAILED"
        console.err(f"{gi.name}: wrote file but verification failed")


def apply_source(gi: GameInfo, profile: dict[str, Any], console: Console, dry: bool) -> None:
    target = resolve_source_autoexec(gi)
    if target is None:
        gi.action = "skip"
        gi.notes.append("no cfg dir found")
        gi.effect = "none"
        return
    gi.config_target = target
    picmip = int(profile["source_mat_picmip"])
    desired_line = f"mat_picmip {picmip}"
    cvar_re = re.compile(r"^\s*mat_picmip\b.*$", re.MULTILINE)

    text = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    m = re.search(r"^\s*mat_picmip\s+(\S+)", text, re.MULTILINE)
    before = m.group(1) if m else "<unset>"
    console.debug(f"autoexec.cfg target: {target}")
    console.debug(f"    mat_picmip: {before} -> {picmip}")

    if m and m.group(1) == str(picmip):
        gi.action = "already"
        gi.effect = "texture quality already set (idempotent)"
        console.good(f"{gi.name}: already applied")
        return

    if not _writable(target):
        gi.action = "skip"
        gi.notes.append("target not writable; skipped")
        gi.effect = "none"
        console.warn(f"{gi.name}: {target} not writable - skipped")
        return

    if cvar_re.search(text):
        new_text = cvar_re.sub(desired_line, text, count=1)
    else:
        prefix = text if text.endswith("\n") or text == "" else text + "\n"
        new_text = prefix + f"// added by vramculler: lower texture VRAM use\n{desired_line}\n"

    if dry:
        gi.action = "dry"
        gi.effect = "VRAM pressure ↓ (FPS only if it was spilling)"
        gi.backup = make_backup(target, console, dry=True) if target.is_file() else None
        console.info(f"{gi.name}: [dry-run] would set {desired_line} in {target}")
        return

    existed = target.is_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    gi.backup = make_backup(target, console, dry=False) if existed else None
    target.write_text(new_text, encoding="utf-8")
    record_change(target, created=not existed, backup=gi.backup)

    verify_text = target.read_text(encoding="utf-8", errors="replace")
    vm = re.search(r"^\s*mat_picmip\s+(\S+)", verify_text, re.MULTILINE)
    if vm and vm.group(1) == str(picmip):
        gi.action = "applied"
        gi.effect = "VRAM pressure ↓ (FPS only if it was spilling)"
        console.good(f"{gi.name}: applied + verified")
    else:
        gi.action = "fail"
        gi.effect = "verify FAILED"
        console.err(f"{gi.name}: verification failed")


def handle_source2(gi: GameInfo, profile: dict[str, Any], console: Console) -> None:
    """Source 2 (e.g. CS2) does not expose mat_picmip; texture quality is an
    in-game video setting. We never write VAC-relevant cvars. Print the safe,
    standard recommendation instead and make no file changes."""
    gi.action = "skip"
    gi.effect = "manual: set Texture Quality = Low in game video options"
    gi.notes.append("Source 2: lower 'Texture Streaming'/'Texture Quality' in-game (no safe cfg knob)")
    console.warn(f"{gi.name}: Source 2 - set texture quality in-game video settings (no safe cfg tweak)")
    print_source2_launch_hint(gi, console)


def handle_unity(gi: GameInfo, console: Console) -> None:
    gi.action = "skip"
    gi.effect = "no safe config tweak available"
    gi.notes.append("Unity: no documented, universally-safe VRAM config key")
    console.warn(f"{gi.name}: Unity detected - no safe config tweak available (skipped)")


def handle_unknown(gi: GameInfo, console: Console) -> None:
    gi.action = "skip"
    gi.effect = "unsupported engine"
    console.debug(f"{gi.name}: engine unsupported / not detected - skipped")


# --------------------------------------------------------------------------- #
# Shell snippet helper (OS-correct syntax)
# --------------------------------------------------------------------------- #
def print_source2_launch_hint(gi: GameInfo, console: Console) -> None:
    osname = host_os()
    console.debug("Recommended (optional) Steam launch options - set via game Properties:")
    console.debug("    -high -nojoy")
    if osname == "linux":
        shell = detect_user_shell() or "fish"
        if "fish" in shell:
            console.debug("If launching outside Steam, fish syntax for env vars:")
            console.debug("    set -gx DXVK_HUD compiler")
        else:
            console.debug("If launching outside Steam (bash/zsh):")
            console.debug("    export DXVK_HUD=compiler")
    elif osname == "windows":
        console.debug("If launching outside Steam (PowerShell):")
        console.debug('    $env:DXVK_HUD = "compiler"')


# --------------------------------------------------------------------------- #
# Tool config (saved steam path)
# --------------------------------------------------------------------------- #
def tool_config_path() -> Path:
    if host_os() == "windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "vramculler" / "config.json"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "vramculler" / "config.json"


def load_saved_steam_path() -> Optional[Path]:
    p = tool_config_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            sp = data.get("steam_path")
            if sp:
                return Path(sp)
        except Exception:
            return None
    return None


def save_steam_path(path: Path, console: Console) -> None:
    p = tool_config_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["steam_path"] = str(path)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        console.debug(f"saved steam path to {p}")
    except Exception as e:
        console.warn(f"could not save config: {e}")


# --------------------------------------------------------------------------- #
# Change manifest - lets --restore delete files we created from scratch and
# restore files we modified (backups alone can't represent "didn't exist").
# --------------------------------------------------------------------------- #
def manifest_path() -> Path:
    return tool_config_path().with_name("manifest.json")


def load_manifest() -> dict[str, Any]:
    p = manifest_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def record_change(target: Path, created: bool, backup: Optional[Path]) -> None:
    """Record that we touched `target`. First record wins (so re-applies don't
    flip a 'modified' file into a 'created' one)."""
    p = manifest_path()
    data = load_manifest()
    key = str(target)
    if key not in data:
        data[key] = {
            "created": bool(created),
            "backup": (backup.name if backup else None),
        }
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
ACTION_COLOR = {
    "applied": C.GREEN,
    "already": C.CYAN,
    "dry": C.YELLOW,
    "skip": C.GREY,
    "fail": C.RED,
}


def render_summary(games: list[GameInfo], console: Console) -> None:
    console.print()
    console.rule("SUMMARY")
    if console.use_rich:
        table = _RichTable(box=_rich_box.HEAVY_EDGE, header_style="bold magenta", border_style="magenta")
        for col in ("game", "os/runtime", "engine", "action", "est. effect", "backup"):
            table.add_column(col, overflow="fold")
        for gi in games:
            table.add_row(
                gi.name,
                f"{host_os()}/{gi.runtime}",
                gi.engine + (f":{gi.engine_detail}" if gi.engine_detail and not gi.engine_detail.startswith("(") else ""),
                gi.action,
                gi.effect or "-",
                (gi.backup.name if gi.backup else "-"),
            )
        console._rich.print(table)  # type: ignore[union-attr]
    else:
        rows = [("GAME", "OS/RUNTIME", "ENGINE", "ACTION", "EST. EFFECT", "BACKUP")]
        for gi in games:
            rows.append((
                gi.name[:28],
                f"{host_os()}/{gi.runtime}",
                (gi.engine + (f":{gi.engine_detail}" if gi.engine_detail and not gi.engine_detail.startswith("(") else ""))[:22],
                gi.action,
                (gi.effect or "-")[:40],
                (gi.backup.name if gi.backup else "-"),
            ))
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        for ri, row in enumerate(rows):
            cells = []
            for ci, cell in enumerate(row):
                txt = cell.ljust(widths[ci])
                if ri == 0:
                    txt = C.wrap(txt, C.BOLD, C.MAGENTA)
                elif ci == 3:
                    txt = C.wrap(txt, ACTION_COLOR.get(row[3], C.GREY))
                cells.append(txt)
            console.print("  ".join(cells))
    console.print()
    console.print(C.wrap(
        "Note: VRAM pressure ↓ does NOT mean guaranteed +FPS. FPS improves only "
        "when a game was exceeding VRAM and spilling to system RAM. Titles that "
        "already fit will look identical.", C.DIM, C.CYAN))


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #
def resolve_steam_path(args, console: Console) -> Optional[Path]:
    if args.steam_path:
        sp = Path(args.steam_path).expanduser()
        if not is_steam_root(sp):
            console.warn(f"{sp} doesn't look like a Steam root (no steamapps/) - using anyway")
        save_steam_path(sp, console)
        return sp
    saved = load_saved_steam_path()
    if saved and saved.exists():
        console.info(f"using saved Steam path: {saved}")
        return saved
    console.info("probing known Steam locations...")
    probed = probe_steam_path(console)
    if probed:
        console.good(f"found Steam at {probed}")
        return probed
    return None


def filter_games(games: list[GameInfo], selector: str) -> list[GameInfo]:
    """Filter games by exact appid or case-insensitive substring of the name."""
    sel = selector.strip().lower()
    out = []
    for gi in games:
        if gi.appid == selector.strip() or sel in gi.name.lower():
            out.append(gi)
    return out


def populate_report(gi: GameInfo, profile: dict[str, Any]) -> None:
    """Fill in intended target/action/effect for audit views without writing."""
    if gi.engine == "unreal":
        gi.config_target = resolve_unreal_engine_ini(gi)
        gi.action = "would-tweak" if gi.config_target else "skip"
        gi.effect = ("set r.Streaming.PoolSize / LimitPoolSizeToVRAM"
                     if gi.config_target else "no UE config path")
    elif gi.engine == "source":
        gi.config_target = resolve_source_autoexec(gi)
        gi.action = "would-tweak"
        gi.effect = f"set mat_picmip {profile['source_mat_picmip']}"
    elif gi.engine == "source2":
        gi.action = "manual"
        gi.effect = "set texture quality in-game (no safe cfg knob)"
    elif gi.engine == "unity":
        gi.action = "skip"
        gi.effect = "no safe config tweak available"
    else:
        gi.action = "skip"
        gi.effect = "unsupported engine"


def apply_one(gi: GameInfo, profile: dict[str, Any], console: Console, dry: bool) -> None:
    """Dispatch a single game to its engine handler."""
    console.print(C.wrap(f"▸ {gi.name}", C.BOLD, C.PINK))
    if gi.engine == "unreal":
        apply_unreal(gi, profile, console, dry=dry)
    elif gi.engine == "source":
        apply_source(gi, profile, console, dry=dry)
    elif gi.engine == "source2":
        handle_source2(gi, profile, console)
    elif gi.engine == "unity":
        handle_unity(gi, console)
    else:
        handle_unknown(gi, console)


# --------------------------------------------------------------------------- #
# Interactive menu
# --------------------------------------------------------------------------- #
ENGINE_COLOR = {
    "unreal": C.MAGENTA,
    "source": C.CYAN,
    "source2": C.BLUE,
    "unity": C.YELLOW,
    "unknown": C.GREY,
}


def _menu_list_games(games: list[GameInfo], console: Console) -> None:
    console.rule("DETECTED GAMES")
    actionable = {"unreal", "source"}
    for i, gi in enumerate(games, 1):
        col = ENGINE_COLOR.get(gi.engine, C.GREY)
        eng = gi.engine + (f":{gi.engine_detail}"
                           if gi.engine_detail and not gi.engine_detail.startswith("(") else "")
        mark = C.wrap("●", C.GREEN) if gi.engine in actionable else C.wrap("○", C.GREY)
        num = C.wrap(f"{i:>2}", C.BOLD, C.PINK)
        rt = C.wrap(f"{host_os()}/{gi.runtime}", C.DIM, C.GREY)
        console.print(f"  {num} {mark} {C.wrap(gi.name, C.BOLD)}  {rt}  {C.wrap(eng, col)}")
    console.print()
    console.print(C.wrap("  ● = has a safe VRAM knob   ○ = skipped (no safe tweak)", C.DIM, C.GREY))
    console.print()


def _parse_selection(raw: str, n: int) -> Optional[list[int]]:
    """Parse '1,3,5', '2-4', 'all'/'a' into a 0-based index list. None = bad."""
    raw = raw.strip().lower()
    if raw in ("a", "all", "*"):
        return list(range(n))
    idx: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            try:
                lo, hi = part.split("-", 1)
                for v in range(int(lo), int(hi) + 1):
                    if 1 <= v <= n:
                        idx.add(v - 1)
            except ValueError:
                return None
        else:
            try:
                v = int(part)
            except ValueError:
                return None
            if 1 <= v <= n:
                idx.add(v - 1)
    return sorted(idx)


def _prompt(console: Console, msg: str) -> str:
    try:
        return input(C.wrap("  ❯ ", C.BOLD, C.PINK) + msg + " ").strip()
    except EOFError:
        return "q"


def interactive_menu(games: list[GameInfo], steam_root: Path, libraries: list[Path],
                     console: Console, profile_name: str) -> int:
    """Drive detection results through a numbered, stdlib-only menu."""
    if not games:
        console.warn("no games detected - nothing to show in the menu.")
        return 0

    while True:
        console.print()
        _menu_list_games(games, console)
        console.print(C.wrap("  Actions:", C.BOLD, C.CYAN))
        console.print("    [number(s)]  select games to tweak (e.g. 1,3 or 2-5 or 'all')")
        console.print(f"    [p] profile (current: {C.wrap(profile_name, C.BOLD, C.PINK)})    "
                      "[v] restore/undo all    [q] quit")
        console.print()
        choice = _prompt(console, "select:")

        if choice in ("q", "quit", ""):
            console.info("bye.")
            return 0

        if choice == "p":
            names = list(PROFILES.keys())
            console.print("  profiles: " + ", ".join(
                f"{C.wrap(str(i + 1), C.BOLD)}={n}" for i, n in enumerate(names)))
            sel = _prompt(console, "choose profile number (or name):")
            chosen = None
            if sel.isdigit() and 1 <= int(sel) <= len(names):
                chosen = names[int(sel) - 1]
            elif sel in PROFILES:
                chosen = sel
            if chosen:
                profile_name = chosen
                console.good(f"profile set to {profile_name}")
            else:
                console.warn("unrecognized profile; unchanged.")
            continue

        if choice in ("v", "restore", "undo"):
            confirm = _prompt(console, "restore ALL changes from backups? [y/N]:")
            if confirm.lower() in ("y", "yes"):
                console.rule("RESTORE")
                n = restore_all(steam_root, libraries, games, console, dry=False)
                console.good(f"restore complete: {n} file(s) restored")
            else:
                console.info("restore cancelled.")
            continue

        picks = _parse_selection(choice, len(games))
        if picks is None:
            console.warn("could not parse that selection - try e.g. 1,3 or 2-5 or 'all'.")
            continue
        if not picks:
            console.warn("nothing selected.")
            continue

        selected = [games[i] for i in picks]
        console.print()
        console.info("selected: " + ", ".join(C.wrap(g.name, C.BOLD) for g in selected))
        mode = _prompt(console, "[d]ry-run preview, [a]pply for real, or [c]ancel?")
        mode = mode.lower()
        if mode in ("c", "cancel", "q", ""):
            console.info("cancelled.")
            continue
        dry = mode in ("d", "dry", "dry-run")
        if not dry and mode not in ("a", "apply"):
            console.warn("unrecognized; cancelling this round.")
            continue
        if not dry:
            confirm = _prompt(console, f"apply '{profile_name}' tweaks to {len(selected)} game(s) for REAL? [y/N]:")
            if confirm.lower() not in ("y", "yes"):
                console.info("not applied.")
                continue

        profile = PROFILES[profile_name]
        console.print()
        console.rule("APPLY" + (" (dry-run)" if dry else ""))
        for gi in selected:
            apply_one(gi, profile, console, dry=dry)
        render_summary(selected, console)
        if dry:
            console.info("dry-run: no files were modified.")
        # loop back to the menu for another round


def run(args) -> int:
    console = Console(use_rich=not args.no_rich, quiet=getattr(args, "quiet", False))
    if not args.quiet_banner:
        console.banner()
    console.print(C.wrap(
        "⚠  WORK IN PROGRESS / EXPERIMENTAL - use at your own risk. It edits game "
        "config files; backups are made but things can still break. The author is "
        "NOT liable for anything that breaks (no warranty - MIT). Try --report-only "
        "and --dry-run first.", C.BOLD, C.YELLOW))
    console.print()

    osname = host_os()
    distro = detect_linux_distro() if osname == "linux" else platform.platform()
    console.info(f"host: {C.wrap(osname, C.BOLD)}  ({distro})")
    console.info(f"profile: {C.wrap(args.profile, C.BOLD, C.PINK)}  (4GB-card tuned)")
    console.print()

    steam_root = resolve_steam_path(args, console)
    if not steam_root:
        console.err("no Steam library found. Pass --steam-path /path/to/Steam")
        console.print()
        console.info("Linux candidates probed:")
        for c in steam_path_candidates():
            console.debug(str(c))
        return 2

    console.rule("DETECTION")
    libraries = enumerate_libraries(steam_root, console)
    if not libraries:
        console.err("no Steam libraries with a steamapps/ dir were found")
        return 2
    for lib in libraries:
        console.info(f"library: {lib}")
    games = enumerate_games(libraries, console)
    console.info(f"installed games found: {C.wrap(str(len(games)), C.BOLD)}")
    if getattr(args, "game", None):
        matched = filter_games(games, args.game)
        console.info(f"filter '{args.game}': {len(matched)} of {len(games)} game(s) match")
        if not matched:
            console.warn(f"no installed game matches '{args.game}' (appid or name substring)")
        games = matched
    console.print()

    for gi in games:
        detect_engine(gi)
        console.print(C.wrap(f"▸ {gi.name}", C.BOLD, C.CYAN) + C.wrap(f"  [appid {gi.appid}]", C.DIM, C.GREY))
        console.debug(f"os/runtime : {osname}/{gi.runtime}" + (f"  prefix={gi.prefix}" if gi.prefix else ""))
        console.debug(f"install    : {gi.install_dir}")
        console.debug(f"engine     : {gi.engine}" + (f" ({gi.engine_detail})" if gi.engine_detail else ""))

    # interactive menu short-circuits the non-interactive flow
    if getattr(args, "menu", False):
        if not sys.stdin.isatty():
            console.err("--menu needs an interactive terminal (stdin is not a TTY).")
            return 2
        return interactive_menu(games, steam_root, libraries, console, args.profile)

    # restore mode short-circuits mutation
    if args.restore:
        console.print()
        console.rule("RESTORE")
        n = restore_all(steam_root, libraries, games, console, dry=args.dry_run)
        console.good(f"restore complete: {n} file(s) {'would be ' if args.dry_run else ''}restored")
        return 0

    profile = PROFILES[args.profile]

    if not args.report_only:
        console.print()
        console.rule("APPLY" + (" (dry-run)" if args.dry_run else ""))

    for gi in games:
        if args.report_only:
            populate_report(gi, profile)
            continue
        apply_one(gi, profile, console, dry=args.dry_run)

    render_summary(games, console)

    if args.report_only:
        console.info("report-only: no files were modified.")
    elif args.dry_run:
        console.info("dry-run: no files were modified.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vramculler",
        description="Reduce VRAM pressure for installed Steam games via safe, "
                    "per-engine config tweaks (UE texture-streaming pool, Source "
                    "texture quality). Not a texture compressor; unrelated to NVIDIA NTC.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"vramculler {__version__}")
    p.add_argument("--steam-path", help="Path to a Steam root (contains steamapps/). Saved for next time.")
    p.add_argument("--profile", choices=list(PROFILES.keys()), default=DEFAULT_PROFILE,
                   help=f"Tweak aggressiveness (default: {DEFAULT_PROFILE}).")
    p.add_argument("--game", metavar="NAME|APPID",
                   help="Only act on games whose appid matches exactly or whose name contains this substring.")
    p.add_argument("--menu", action="store_true",
                   help="Interactive menu: detect all games, then pick which to tweak. "
                        "(Auto-launches when run with no other action flags in a terminal.)")
    p.add_argument("--report-only", action="store_true",
                   help="Audit only: list games/OS/runtime/engine and what would be tweaked. No changes.")
    p.add_argument("--dry-run", action="store_true",
                   help="Show intended changes (incl. backups) but modify nothing.")
    p.add_argument("--restore", action="store_true",
                   help="Revert all changes from vramculler backups.")
    p.add_argument("--no-rich", action="store_true", help="Force ANSI output even if rich is installed.")
    p.add_argument("--quiet", action="store_true", help="Suppress per-game debug lines (keep summary).")
    p.add_argument("--quiet-banner", action="store_true", help="Suppress the ASCII banner.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # With no explicit action and an interactive terminal, default to the menu.
    if (not args.menu and not args.report_only and not args.dry_run
            and not args.restore and not args.game and sys.stdin.isatty()):
        args.menu = True
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\ninterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
