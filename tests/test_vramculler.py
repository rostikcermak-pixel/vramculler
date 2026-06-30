#!/usr/bin/env python3
"""Unit tests for vramculler.

Pure stdlib (unittest). They build synthetic Steam install trees in a temp dir
so the whole detection + mutation + restore pipeline can be exercised on any OS
without a real Steam install. Run:

    python -m unittest discover -s tests -v
    # or
    python tests/test_vramculler.py
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

# import the module under test (repo root is the parent of tests/)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import vramculler as vc  # noqa: E402


def _silent_console():
    c = vc.Console(use_rich=False, quiet=True)
    c.print = lambda *a, **k: None  # swallow all output during tests
    return c


def write(p: Path, text: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build_fixture(base: Path):
    """Create a two-library Steam tree with one game per supported engine."""
    steam = base / "Steam"
    lib2 = base / "SteamLibrary2"

    write(steam / "config" / "libraryfolders.vdf", f'''"libraryfolders"
{{
\t"0"
\t{{
\t\t"path"\t\t"{steam.as_posix()}"
\t\t"apps" {{ "1000" "1" "2000" "1" "3000" "1" "6000" "1" }}
\t}}
\t"1"
\t{{
\t\t"path"\t\t"{lib2.as_posix()}"
\t\t"apps" {{ "4000" "1" "5000" "1" }}
\t}}
}}
''')

    def acf(lib, appid, name, installdir):
        write(lib / "steamapps" / f"appmanifest_{appid}.acf", f'''"AppState"
{{
\t"appid"\t\t"{appid}"
\t"name"\t\t"{name}"
\t"installdir"\t\t"{installdir}"
}}
''')

    # UE5 under Proton (compatdata prefix present), with an existing Engine.ini
    acf(steam, "1000", "Stutter Knights UE5", "StutterKnights")
    g1 = steam / "steamapps" / "common" / "StutterKnights"
    (g1 / "Engine" / "Binaries" / "Win64").mkdir(parents=True)
    (g1 / "StutterKnights" / "Binaries" / "Win64").mkdir(parents=True)
    (g1 / "StutterKnights" / "Content" / "Paks").mkdir(parents=True)
    pfx_cfg = (steam / "steamapps" / "compatdata" / "1000" / "pfx" / "drive_c" / "users"
               / "steamuser" / "AppData" / "Local" / "StutterKnights" / "Saved"
               / "Config" / "Windows")
    write(pfx_cfg / "Engine.ini", "[Core.System]\nPaths=../../../Engine/Content\n")

    # Source 1
    acf(steam, "2000", "Counter Offensive", "Counter Offensive")
    g2 = steam / "steamapps" / "common" / "Counter Offensive"
    (g2 / "csgo" / "cfg").mkdir(parents=True)
    write(g2 / "csgo" / "gameinfo.txt", '"GameInfo" {}')
    write(g2 / "hl2_linux", "")

    # Unity
    acf(steam, "3000", "Voxel Survivor", "VoxelSurvivor")
    g3 = steam / "steamapps" / "common" / "VoxelSurvivor"
    (g3 / "VoxelSurvivor_Data").mkdir(parents=True)
    write(g3 / "VoxelSurvivor_Data" / "globalgamemanagers", "Unity")

    # proprietary / unknown
    acf(steam, "6000", "Mystery Box", "MysteryBox")
    write(steam / "steamapps" / "common" / "MysteryBox" / "game.bin", "x")

    # Source 2 in lib2
    acf(lib2, "4000", "Tactical Strike 2", "Tactical Strike 2")
    g4 = lib2 / "steamapps" / "common" / "Tactical Strike 2"
    (g4 / "game" / "tactics").mkdir(parents=True)
    write(g4 / "game" / "tactics" / "gameinfo.gi", '"GameInfo" {}')

    # UE4 native (no compatdata)
    acf(lib2, "5000", "Forest Walker UE4", "ForestWalker")
    g5 = lib2 / "steamapps" / "common" / "ForestWalker"
    (g5 / "Engine" / "Binaries" / "Linux").mkdir(parents=True)
    (g5 / "ForestWalker" / "Binaries" / "Linux").mkdir(parents=True)
    (g5 / "ForestWalker" / "Content" / "Paks").mkdir(parents=True)

    return steam, lib2


class IsolatedEnv(unittest.TestCase):
    """Base class: temp dir + isolated HOME / XDG so config/manifest writes are
    sandboxed, and host_os forced to linux unless a test overrides it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.console = _silent_console()
        self._env_backup = dict(os.environ)
        home = self.base / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
        os.environ.pop("NO_COLOR", None)
        self._orig_host_os = vc.host_os
        vc.host_os = lambda: "linux"

    def tearDown(self):
        vc.host_os = self._orig_host_os
        os.environ.clear()
        os.environ.update(self._env_backup)
        self.tmp.cleanup()


class TestVdfParser(unittest.TestCase):
    def test_nested_and_quotes(self):
        text = '"AppState"\n{\n\t"appid"\t\t"570"\n\t"name"\t"Dota 2"\n}\n'
        d = vc.parse_vdf(text)
        self.assertEqual(d["AppState"]["appid"], "570")
        self.assertEqual(d["AppState"]["name"], "Dota 2")

    def test_libraryfolders(self):
        text = '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t"/a/b"\n\t}\n}\n'
        d = vc.parse_vdf(text)
        self.assertEqual(d["libraryfolders"]["0"]["path"], "/a/b")

    def test_comment_lines_ignored(self):
        text = '// comment\n"k"\n{\n\t"x"\t"1"\n}\n'
        d = vc.parse_vdf(text)
        self.assertEqual(d["k"]["x"], "1")


class TestIniHelpers(unittest.TestCase):
    def test_create_section(self):
        out = vc.ini_set_values("", "SystemSettings", {"r.Streaming.PoolSize": "1200"})
        self.assertIn("[SystemSettings]", out)
        self.assertIn("r.Streaming.PoolSize=1200", out)

    def test_update_in_place_preserves_other(self):
        text = "[Core.System]\nPaths=x\n\n[SystemSettings]\nr.Streaming.PoolSize=500\n"
        out = vc.ini_set_values(text, "SystemSettings", {"r.Streaming.PoolSize": "1200"})
        self.assertIn("[Core.System]", out)
        self.assertIn("Paths=x", out)
        self.assertIn("r.Streaming.PoolSize=1200", out)
        self.assertNotIn("PoolSize=500", out)
        # exactly one SystemSettings section
        self.assertEqual(out.count("[SystemSettings]"), 1)

    def test_get_section_values(self):
        text = "[SystemSettings]\na=1\nb=2\n"
        vals = vc.ini_get_section_values(text, "SystemSettings")
        self.assertEqual(vals, {"a": "1", "b": "2"})


class TestDetection(IsolatedEnv):
    def setUp(self):
        super().setUp()
        self.steam, self.lib2 = build_fixture(self.base)

    def _games(self):
        libs = vc.enumerate_libraries(self.steam, self.console)
        games = vc.enumerate_games(libs, self.console)
        for gi in games:
            vc.detect_engine(gi)
        return {gi.appid: gi for gi in games}

    def test_two_libraries(self):
        libs = vc.enumerate_libraries(self.steam, self.console)
        self.assertEqual(len(libs), 2)

    def test_enumerate_all_games(self):
        games = self._games()
        self.assertEqual(set(games), {"1000", "2000", "3000", "4000", "5000", "6000"})

    def test_proton_vs_native(self):
        games = self._games()
        self.assertEqual(games["1000"].runtime, "proton")
        self.assertIsNotNone(games["1000"].prefix)
        self.assertEqual(games["5000"].runtime, "native")

    def test_engine_classification(self):
        games = self._games()
        self.assertEqual(games["1000"].engine, "unreal")
        self.assertEqual(games["1000"].engine_detail, "StutterKnights")
        self.assertEqual(games["2000"].engine, "source")
        self.assertEqual(games["3000"].engine, "unity")
        self.assertEqual(games["4000"].engine, "source2")
        self.assertEqual(games["5000"].engine, "unreal")
        self.assertEqual(games["6000"].engine, "unknown")

    def test_filter_games(self):
        games = list(self._games().values())
        self.assertEqual(len(vc.filter_games(games, "2000")), 1)
        self.assertEqual(vc.filter_games(games, "2000")[0].appid, "2000")
        self.assertEqual(len(vc.filter_games(games, "ue")), 2)  # both UE titles
        self.assertEqual(len(vc.filter_games(games, "nope")), 0)


class TestApplyUnrealProton(IsolatedEnv):
    def setUp(self):
        super().setUp()
        self.steam, self.lib2 = build_fixture(self.base)
        libs = vc.enumerate_libraries(self.steam, self.console)
        self.games = {gi.appid: gi for gi in vc.enumerate_games(libs, self.console)}
        for gi in self.games.values():
            vc.detect_engine(gi)
        self.gi = self.games["1000"]
        self.target = vc.resolve_unreal_engine_ini(self.gi)

    def test_resolve_points_into_prefix(self):
        self.assertIsNotNone(self.target)
        self.assertIn("compatdata", str(self.target))
        self.assertTrue(str(self.target).endswith("Engine.ini"))

    def test_apply_writes_and_verifies(self):
        prof = vc.PROFILES["balanced"]
        vc.apply_unreal(self.gi, prof, self.console, dry=False)
        self.assertEqual(self.gi.action, "applied")
        vals = vc.ini_get_section_values(self.target.read_text(), "SystemSettings")
        self.assertEqual(vals["r.Streaming.PoolSize"], "1200")
        self.assertEqual(vals["r.Streaming.LimitPoolSizeToVRAM"], "1")
        # existing file -> backup made
        self.assertTrue(vc.existing_backups(self.target))
        # original section preserved
        self.assertIn("[Core.System]", self.target.read_text())

    def test_idempotent_rerun(self):
        prof = vc.PROFILES["balanced"]
        vc.apply_unreal(self.gi, prof, self.console, dry=False)
        n_bak = len(vc.existing_backups(self.target))
        # fresh GameInfo for second run
        gi2 = self.games["1000"]
        gi2.action = "skip"
        vc.apply_unreal(gi2, prof, self.console, dry=False)
        self.assertEqual(gi2.action, "already")
        self.assertEqual(self.target.read_text().count("[SystemSettings]"), 1)
        self.assertEqual(len(vc.existing_backups(self.target)), n_bak)  # no extra backup

    def test_dry_run_no_write(self):
        before = self.target.read_text()
        prof = vc.PROFILES["aggressive"]
        vc.apply_unreal(self.gi, prof, self.console, dry=True)
        self.assertEqual(self.gi.action, "dry")
        self.assertEqual(self.target.read_text(), before)  # untouched
        self.assertFalse(vc.existing_backups(self.target))


class TestApplySourceAndRestore(IsolatedEnv):
    def setUp(self):
        super().setUp()
        self.steam, self.lib2 = build_fixture(self.base)
        libs = vc.enumerate_libraries(self.steam, self.console)
        self.games = {gi.appid: gi for gi in vc.enumerate_games(libs, self.console)}
        for gi in self.games.values():
            vc.detect_engine(gi)

    def test_source_creates_autoexec(self):
        gi = self.games["2000"]
        target = vc.resolve_source_autoexec(gi)
        self.assertFalse(target.exists())
        vc.apply_source(gi, vc.PROFILES["balanced"], self.console, dry=False)
        self.assertEqual(gi.action, "applied")
        self.assertIn("mat_picmip 1", target.read_text())

    def test_source_idempotent(self):
        gi = self.games["2000"]
        vc.apply_source(gi, vc.PROFILES["balanced"], self.console, dry=False)
        gi2 = self.games["2000"]
        vc.apply_source(gi2, vc.PROFILES["balanced"], self.console, dry=False)
        self.assertEqual(gi2.action, "already")
        target = vc.resolve_source_autoexec(gi)
        # only one mat_picmip line
        self.assertEqual(target.read_text().count("mat_picmip"), 1)

    def test_restore_reverts_modified_and_deletes_created(self):
        ue = self.games["1000"]
        src = self.games["2000"]
        ue_target = vc.resolve_unreal_engine_ini(ue)
        src_target = vc.resolve_source_autoexec(src)
        ue_original = ue_target.read_text()
        vc.apply_unreal(ue, vc.PROFILES["balanced"], self.console, dry=False)
        vc.apply_source(src, vc.PROFILES["balanced"], self.console, dry=False)
        self.assertTrue(src_target.exists())
        self.assertNotEqual(ue_target.read_text(), ue_original)

        n = vc.restore_all(self.steam, [], list(self.games.values()), self.console, dry=False)
        self.assertGreaterEqual(n, 2)
        # modified UE file restored to pristine
        self.assertEqual(ue_target.read_text(), ue_original)
        # created source file deleted
        self.assertFalse(src_target.exists())

    def test_unity_and_unknown_skipped(self):
        unity = self.games["3000"]
        vc.handle_unity(unity, self.console)
        self.assertEqual(unity.action, "skip")
        unknown = self.games["6000"]
        vc.handle_unknown(unknown, self.console)
        self.assertEqual(unknown.action, "skip")


class TestWindowsPathResolution(IsolatedEnv):
    def test_unreal_resolves_localappdata_on_windows(self):
        steam, lib2 = build_fixture(self.base)
        libs = vc.enumerate_libraries(steam, self.console)
        games = {gi.appid: gi for gi in vc.enumerate_games(libs, self.console)}
        for gi in games.values():
            vc.detect_engine(gi)
        gi = games["5000"]  # native UE4 -> on windows uses LOCALAPPDATA
        vc.host_os = lambda: "windows"
        gi.runtime = "native"
        gi.prefix = None
        localapp = self.base / "LocalApp"
        os.environ["LOCALAPPDATA"] = str(localapp)
        target = vc.resolve_unreal_engine_ini(gi)
        self.assertIsNotNone(target)
        self.assertIn(str(localapp), str(target))
        self.assertIn("ForestWalker", str(target))


class TestMainSmoke(IsolatedEnv):
    def test_report_only_runs_clean(self):
        steam, _ = build_fixture(self.base)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = vc.main(["--steam-path", str(steam), "--report-only", "--no-rich",
                          "--quiet-banner", "--quiet"])
        self.assertEqual(rc, 0)
        # report-only must not create any backups anywhere
        self.assertEqual(list(steam.rglob("*.bak.*")), [])

    def test_no_steam_found_returns_2(self):
        empty = self.base / "nothing"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = vc.main(["--steam-path", str(empty / "Steam"), "--report-only",
                          "--no-rich", "--quiet-banner", "--quiet"])
        # path doesn't exist -> enumerate finds no libraries -> rc 2
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
