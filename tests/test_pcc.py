#!/usr/bin/env python3
"""Proton Command Center test suite. Stdlib only, no Steam required:
builds a mock Steam install in a temp dir. Run:  python3 tests/test_pcc.py"""

import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pcc  # noqa: E402


def make_mock_steam(base: Path) -> Path:
    root = base / "Steam"
    (root / "steamapps/common/TestGame/Engine").mkdir(parents=True)
    (root / "userdata/12345678/config").mkdir(parents=True)
    (root / "steamapps/shadercache/12345/fozpipelinesv6").mkdir(parents=True)

    (root / "steamapps/appmanifest_12345.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"12345"\n\t"name"\t\t"Test Game"\n'
        '\t"installdir"\t\t"TestGame"\n\t"SizeOnDisk"\t\t"52428800"\n}\n')
    (root / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{root}"\n\t}}\n}}\n')
    (root / "userdata/12345678/config/localconfig.vdf").write_text(
        '"UserLocalConfigStore"\n{\n\t"friends"\n\t{\n'
        '\t\t"VoiceReceiveVolume"\t\t"0.75"\n\t}\n'
        '\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n'
        '\t\t\t\t"apps"\n\t\t\t\t{\n\t\t\t\t\t"12345"\n\t\t\t\t\t{\n'
        '\t\t\t\t\t\t"LaunchOptions"\t\t"PROTON_USE_NTSYNC=1 %command%"\n'
        '\t\t\t\t\t\t"playtime"\t\t"120"\n'
        '\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n')

    # fake DLSS DLL with VS_FIXEDFILEINFO signature, version 310.3.0.0
    blob = (b"MZ" + b"\x00" * 200 + struct.pack("<I", 0xFEEF04BD)
            + struct.pack("<I", 0x00010000)
            + struct.pack("<II", (310 << 16) | 3, 0) + b"\x00" * 100)
    (root / "steamapps/common/TestGame/Engine/nvngx_dlss.dll").write_bytes(blob)
    (root / "steamapps/shadercache/12345/fozpipelinesv6/steam_pipeline_cache.foz").write_bytes(b"foz")
    return root


class PCCTests(unittest.TestCase):
    _ORIGINALS = ("steam_running", "driver_version", "_nvidia_gpus", "_drm_gpus",
                  "cpu_name", "find_font", "shutdown_steam", "subprocess",
                  "MANGOHUD_DIR")

    def setUp(self):
        self._saved = {n: getattr(pcc, n) for n in self._ORIGINALS
                       if hasattr(pcc, n)}
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = make_mock_steam(base)
        # isolate app data
        pcc.DATA_DIR = base / "appdata"
        pcc.DLL_LIBRARY = pcc.DATA_DIR / "dlls"
        pcc.BACKUP_DIR = pcc.DATA_DIR / "backups"
        pcc.STATE_FILE = pcc.DATA_DIR / "state.json"
        pcc.CONFIG_FILE = pcc.DATA_DIR / "config.json"
        pcc.ART_DIR = pcc.DATA_DIR / "art"
        pcc.RESHADE_DIR = pcc.DATA_DIR / "reshade"
        pcc.RESHADE_SHADERS_DIR = pcc.RESHADE_DIR / "shaders"
        for d in (pcc.DLL_LIBRARY, pcc.BACKUP_DIR, pcc.ART_DIR, pcc.RESHADE_DIR):
            d.mkdir(parents=True, exist_ok=True)
        pcc.steam_running = lambda: False
        pcc.ART_MISSES.clear()
        pcc.driver_version = lambda: "580.65.06"

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(pcc, n, v)
        self.tmp.cleanup()

    # ---- library / VDF ----
    def test_list_games(self):
        games = pcc.list_games(self.root)
        self.assertEqual(games[0]["appid"], "12345")
        self.assertEqual(games[0]["name"], "Test Game")
        self.assertTrue(games[0]["installed"])

    def test_launch_options_roundtrip(self):
        self.assertEqual(pcc.get_launch_options(self.root, "12345")["value"],
                         "PROTON_USE_NTSYNC=1 %command%")
        new = "PROTON_ENABLE_WAYLAND=1 game-performance %command% -dx12"
        pcc.set_launch_options(self.root, "12345", new)
        self.assertEqual(pcc.get_launch_options(self.root, "12345")["value"], new)

    def test_vdf_preserves_unrelated_keys(self):
        pcc.set_launch_options(self.root, "12345", "mangohud %command%")
        data = pcc.vdf_parse(
            (self.root / "userdata/12345678/config/localconfig.vdf").read_text())
        store = data["UserLocalConfigStore"]
        self.assertEqual(store["friends"]["VoiceReceiveVolume"], "0.75")
        self.assertEqual(
            store["Software"]["Valve"]["Steam"]["apps"]["12345"]["playtime"], "120")

    def test_vdf_escaped_quotes(self):
        tricky = 'WINEDLLOVERRIDES="dwmapi=n,b" %command%'
        pcc.set_launch_options(self.root, "12345", tricky)
        self.assertEqual(pcc.get_launch_options(self.root, "12345")["value"], tricky)

    def test_refuses_while_steam_running_without_flag(self):
        pcc.steam_running = lambda: True
        with self.assertRaises(RuntimeError):
            pcc.set_launch_options(self.root, "12345", "x %command%")

    def test_shortcut_launch_options_roundtrip(self):
        """Non-Steam shortcuts have no localconfig.vdf apps.<appid> node -
        Steam reads their launch args straight off the shortcut entry, so
        this must land in shortcuts.vdf, not localconfig.vdf."""
        r = pcc.add_shortcut(self.root, "My Game", "/tmp/MyGame/MyGame.exe")
        appid = r["appid"]
        self.assertEqual(pcc.get_launch_options(self.root, appid)["value"], "")
        pcc.set_launch_options(self.root, appid, "-dx12 -novid")
        self.assertEqual(pcc.get_launch_options(self.root, appid)["value"],
                         "-dx12 -novid")
        data = pcc.binvdf_parse(pcc.shortcuts_path(self.root).read_bytes())
        entry = next(e for e in data["shortcuts"].values()
                    if str(e["appid"] & 0xFFFFFFFF) == appid)
        self.assertEqual(entry["LaunchOptions"], "-dx12 -novid")
        cfg = pcc.vdf_parse(
            (self.root / "userdata/12345678/config/localconfig.vdf").read_text())
        apps = cfg["UserLocalConfigStore"]["Software"]["Valve"]["Steam"].get("apps", {})
        self.assertNotIn(appid, apps)

    # ---- binary VDF (shortcuts.vdf) / non-Steam games ----
    def test_binvdf_roundtrip_synthetic(self):
        """Hand-built fixture (NOT generated by the code under test) matching
        a real shortcuts.vdf's byte layout: type byte, null-terminated key,
        then a type-specific value. Every object - including the implicit
        top-level one - closes with one 0x08, so a single-entry document with
        one empty nested "tags" object ends in a run of four 0x08 bytes.
        This exact shape was verified against a real Steam-written
        shortcuts.vdf during development; this fixture is independent
        synthetic data so the test doesn't depend on anyone's real file."""
        import struct as _struct
        fixture = (
            b"\x00shortcuts\x00"
            b"\x000\x00"
            b"\x02appid\x00" + _struct.pack("<i", -12345) +
            b"\x01AppName\x00TestGame\x00"
            b'\x01Exe\x00"/tmp/TestGame/TestGame.exe"\x00'
            b"\x01StartDir\x00/tmp/TestGame/\x00"
            b"\x00tags\x00\x08"   # empty "tags" object, closed
            b"\x08"               # close entry "0"
            b"\x08"               # close "shortcuts"
            b"\x08"               # close the implicit root
        )
        parsed = pcc.binvdf_parse(fixture)
        entry = parsed["shortcuts"]["0"]
        self.assertEqual(entry["appid"], -12345)
        self.assertEqual(entry["AppName"], "TestGame")
        self.assertEqual(entry["Exe"], '"/tmp/TestGame/TestGame.exe"')
        self.assertEqual(entry["tags"], {})
        self.assertEqual(pcc.binvdf_dump(parsed), fixture)

    def test_compute_shortcut_id_matches_real_steam_shortcut(self):
        """Regression anchor: these exact inputs/output were captured from a
        real shortcut Steam itself created (verified independently against
        that shortcut's grid-art folder name, which is filed under the same
        unsigned value)."""
        top32, signed = pcc.compute_shortcut_id('"/usr/bin/flatpak"', "Jellyfin")
        self.assertEqual(signed, -1333899919)
        self.assertEqual(top32, 2961067377)

    def test_add_shortcut_creates_and_merges_into_games(self):
        r = pcc.add_shortcut(self.root, "My Game", "/tmp/MyGame/MyGame.exe")
        self.assertTrue(r["saved"])
        shortcuts = pcc.list_shortcuts(self.root)
        self.assertEqual(len(shortcuts), 1)
        self.assertEqual(shortcuts[0]["appid"], r["appid"])
        self.assertEqual(shortcuts[0]["name"], "My Game")
        self.assertTrue(shortcuts[0]["custom"])
        # merges into the same list real installed games appear in
        merged = {g["appid"]: g for g in pcc.all_games(self.root)}
        self.assertIn(r["appid"], merged)
        self.assertIn("12345", merged)   # the mock's real Steam game, untouched

    def test_add_shortcut_preserves_other_entries_on_remove(self):
        a = pcc.add_shortcut(self.root, "Game A", "/tmp/A/A.exe")
        b = pcc.add_shortcut(self.root, "Game B", "/tmp/B/B.exe")
        pcc.remove_shortcut(self.root, a["appid"])
        remaining = pcc.list_shortcuts(self.root)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["appid"], b["appid"])
        self.assertEqual(remaining[0]["name"], "Game B")

    def test_add_shortcut_refuses_duplicate(self):
        pcc.add_shortcut(self.root, "Same Game", "/tmp/Same/Same.exe")
        with self.assertRaises(RuntimeError):
            pcc.add_shortcut(self.root, "Same Game", "/tmp/Same/Same.exe")

    def test_add_shortcut_refuses_while_steam_running_without_flag(self):
        pcc.steam_running = lambda: True
        with self.assertRaises(RuntimeError):
            pcc.add_shortcut(self.root, "My Game", "/tmp/MyGame/MyGame.exe")

    def test_remove_shortcut_not_found(self):
        with self.assertRaises(RuntimeError):
            pcc.remove_shortcut(self.root, "999999999")

    def test_launch_game_shifts_id_for_shortcuts_only(self):
        """Non-Steam shortcuts need the 64-bit shifted id for rungameid;
        real Steam appids (always well under 2**31) must NOT be shifted."""
        calls = []
        real_which, real_spawn = pcc.shutil.which, pcc._spawn_detached
        pcc.shutil.which = lambda n: "/usr/bin/steam"
        pcc._spawn_detached = lambda cmd: calls.append(cmd) or True
        try:
            pcc.launch_game("12345")
            self.assertTrue(calls[0][-1].endswith("rungameid/12345"))
            r = pcc.add_shortcut(self.root, "My Game", "/tmp/MyGame/MyGame.exe")
            top32 = int(r["appid"])
            pcc.launch_game(r["appid"])
            expected = (top32 << 32) | 0x02000000
            self.assertTrue(calls[1][-1].endswith(f"rungameid/{expected}"))
        finally:
            pcc.shutil.which, pcc._spawn_detached = real_which, real_spawn

    def test_auto_close_steam(self):
        calls = []
        state = {"running": True}
        pcc.steam_running = lambda: state["running"]
        pcc.shutdown_steam = lambda timeout=60: (calls.append(1), state.update(running=False))
        r = pcc.set_launch_options(self.root, "12345", "x %command%", close_steam=True)
        self.assertTrue(r["saved"])
        self.assertEqual(len(calls), 1)

    # ---- DLSS ----
    def test_pe_version(self):
        dll = self.root / "steamapps/common/TestGame/Engine/nvngx_dlss.dll"
        self.assertEqual(pcc.pe_version(dll), "310.3.0.0")

    def test_scan_swap_restore(self):
        game_dir = self.root / "steamapps/common/TestGame"
        dlls = pcc.scan_game_dlss(game_dir)
        self.assertEqual(dlls[0]["kind"], "sr")
        info = pcc.import_dll(dlls[0]["path"])
        lib = pcc.DLL_LIBRARY / "sr" / info["version"] / "nvngx_dlss.dll"
        s = pcc.swap_dll(dlls[0]["path"], str(lib))
        self.assertTrue(s["swapped"])
        r = pcc.restore_dll(dlls[0]["path"])
        self.assertTrue(r["restored"])

    def test_scan_ultraplus_detects_loader_and_asi(self):
        exe_dir = self.root / "steamapps/common/TestGame/Binaries/Win64"
        mods_dir = exe_dir / "ue4ss" / "Mods"
        mods_dir.mkdir(parents=True)
        (exe_dir / "dwmapi.dll").write_bytes(b"x")
        (exe_dir / "NaniteRayTracingFix.asi").write_bytes(b"x")
        (mods_dir / "mods.txt").write_text(
            "﻿BPML_GenericFunctions : 1\nUltraPlusExtensions : 1\n")
        st = pcc.scan_ultraplus(self.root / "steamapps/common/TestGame")
        self.assertTrue(st["installed"])
        self.assertTrue(st["loader_present"])
        self.assertTrue(st["mod_enabled"])
        self.assertEqual(st["asi_files"], ["NaniteRayTracingFix.asi"])
        self.assertEqual(st["exe_dir"], str(exe_dir))

    def test_scan_ultraplus_missing_loader(self):
        exe_dir = self.root / "steamapps/common/TestGame/Binaries/Win64"
        (exe_dir / "ue4ss" / "Mods").mkdir(parents=True)
        st = pcc.scan_ultraplus(self.root / "steamapps/common/TestGame")
        self.assertTrue(st["installed"])
        self.assertFalse(st["loader_present"])
        self.assertEqual(st["asi_files"], [])

    def test_scan_ultraplus_not_installed(self):
        st = pcc.scan_ultraplus(self.root / "steamapps/common/TestGame")
        self.assertFalse(st["installed"])

    # ---- Ultra+ mod install (ported from Ultra+ Manager's C# fixes) ----

    def _fake_ue_game(self):
        d = self.root / "steamapps/common/TestGame"
        exe_dir = d / "Binaries/Win64"
        exe_dir.mkdir(parents=True, exist_ok=True)
        exe = exe_dir / "TestGame-Win64-Shipping.exe"
        exe.write_bytes(b"MZ")
        return d, exe

    def _fake_catalog(self):
        return {"games": {"TestGame": {
            "full_name": "Test Game", "search_terms": ["testgame"], "url": "",
            "exe_path": "Binaries/Win64/TestGame-Win64-Shipping.exe",
            "ue_game_path": "", "install_root_only": False,
            "mod_filename_prefixes": [],
        }}}

    def test_encode_url_path_escapes_spaces(self):
        # Regression: the Ultra+ mods manifest ships download URLs with
        # literal spaces in the filename (e.g. RoboCop: Rogue City -
        # Unfinished Business), which real http.client rejects outright.
        url = ("https://cdn.example/mods/RobocopUnfinishedBusiness/"
               "RobocopUnfinishedBusiness Ultra Plus v0.1.1.zip")
        encoded = pcc._encode_url_path(url)
        self.assertNotIn(" ", encoded)
        self.assertEqual(encoded, ("https://cdn.example/mods/RobocopUnfinishedBusiness/"
                                   "RobocopUnfinishedBusiness%20Ultra%20Plus%20v0.1.1.zip"))

    def test_encode_url_path_leaves_ordinary_url_unchanged(self):
        url = "https://cdn.example/mods/Foo/Foo_v1.0.0.zip"
        self.assertEqual(pcc._encode_url_path(url), url)

    def test_get_safe_segments_rejects_parent_traversal(self):
        with self.assertRaises(ValueError):
            pcc._get_safe_segments("../../etc/passwd")

    def test_get_safe_segments_rejects_absolute_path(self):
        with self.assertRaises(ValueError):
            pcc._get_safe_segments("/etc/passwd")
        with self.assertRaises(ValueError):
            pcc._get_safe_segments("C:/Windows/evil.dll")

    def test_get_safe_segments_allows_ordinary_path(self):
        self.assertEqual(
            pcc._get_safe_segments("ue4ss/Mods/UltraPlusExtensions/main.lua"),
            ["ue4ss", "Mods", "UltraPlusExtensions", "main.lua"])

    def test_resolve_destination_path_install_root_only(self):
        d, exe = self._fake_ue_game()
        r = pcc.resolve_destination_path("some/file.txt", str(d), None, None, True)
        self.assertEqual(r, (d / "some/file.txt").resolve())

    def test_resolve_destination_path_binaries_win_goes_beside_exe(self):
        d, exe = self._fake_ue_game()
        r = pcc.resolve_destination_path(
            "Binaries/Win64/ue4ss/UE4SS.dll", str(d), str(d), str(exe), False)
        self.assertEqual(r, (exe.parent / "ue4ss/UE4SS.dll").resolve())

    def test_resolve_destination_path_content_paks_goes_under_project(self):
        d, exe = self._fake_ue_game()
        r = pcc.resolve_destination_path(
            "Content/Paks/~mods/999_Mod_P.pak", str(d), str(d), str(exe), False)
        self.assertEqual(r, (d / "Content/Paks/~mods/999_Mod_P.pak").resolve())

    def test_resolve_destination_path_root_injection_dll_beside_exe(self):
        d, exe = self._fake_ue_game()
        r = pcc.resolve_destination_path("dwmapi.dll", str(d), str(d), str(exe), False)
        self.assertEqual(r, (exe.parent / "dwmapi.dll").resolve())

    def test_resolve_destination_path_unrecognized_falls_back_to_root(self):
        d, exe = self._fake_ue_game()
        r = pcc.resolve_destination_path("readme.txt", str(d), str(d), str(exe), False)
        self.assertEqual(r, (d / "readme.txt").resolve())

    def test_resolve_destination_path_engine_binaries_not_treated_as_game_binaries(self):
        d, exe = self._fake_ue_game()
        r = pcc.resolve_destination_path(
            "Engine/Binaries/Win64/SomeEngineDll.dll", str(d), str(d), str(exe), False)
        self.assertEqual(r, (d / "Engine/Binaries/Win64/SomeEngineDll.dll").resolve())

    def test_resolve_executable_and_project_path_from_catalog(self):
        d, exe = self._fake_ue_game()
        catalog = self._fake_catalog()
        resolved_exe = pcc.resolve_executable_path("TestGame", str(d), catalog)
        self.assertEqual(Path(resolved_exe), exe)
        project = pcc.resolve_unreal_project_path("TestGame", str(d), catalog, resolved_exe)
        self.assertEqual(Path(project), d)

    def test_should_install_with_user_managed_ue4ss_allows_ultraplus_files(self):
        self.assertTrue(pcc.should_install_with_user_managed_ue4ss(
            "Binaries/Win64/ue4ss/Mods/UltraPlusExtensions/scripts/main.lua"))
        self.assertTrue(pcc.should_install_with_user_managed_ue4ss(
            "Content/Paks/~mods/999_Mod_P.pak"))

    def test_should_install_with_user_managed_ue4ss_rejects_ue4ss_runtime(self):
        self.assertFalse(pcc.should_install_with_user_managed_ue4ss(
            "Binaries/Win64/ue4ss/UE4SS.dll"))
        self.assertFalse(pcc.should_install_with_user_managed_ue4ss("dwmapi.dll"))

    def test_is_signature_path(self):
        self.assertTrue(pcc.is_signature_path("/a/b/UE4SS_Signatures/foo.lua"))
        self.assertFalse(pcc.is_signature_path("/a/b/c.lua"))

    def test_synchronize_signature_directories_deletes_stale_files(self):
        sig_dir = Path(self.tmp.name) / "ue4ss/UE4SS_Signatures"
        sig_dir.mkdir(parents=True)
        stale = sig_dir / "OldMod.lua"
        kept = sig_dir / "NewMod.lua"
        stale.write_text("stale")
        kept.write_text("kept")
        deleted = pcc.synchronize_signature_directories([kept])
        self.assertEqual(deleted, 1)
        self.assertFalse(stale.exists())
        self.assertTrue(kept.exists())

    def test_merge_config_contents_case_insensitive_key_matching(self):
        result = pcc.merge_config_contents("raytracing=enabled", "RayTracing=disabled")
        self.assertEqual(result, "RayTracing=enabled")

    def test_merge_config_contents_preserves_shipped_comment_not_backup(self):
        backup = "Quality=Low; old comment from a previous release"
        new = "Quality=High; current release comment"
        self.assertEqual(pcc.merge_config_contents(backup, new),
                         "Quality=Low; current release comment")

    def test_merge_config_contents_section_header_passthrough(self):
        result = pcc.merge_config_contents("Key=uservalue", "[Section]\nKey=newvalue")
        self.assertEqual(result.splitlines(), ["[Section]", "Key=uservalue"])

    def test_list_presets_and_apply_preset(self):
        config_dir = Path(self.tmp.name) / "config"
        config_dir.mkdir()
        (config_dir / "UltraPlusConfig.ini").write_text(
            "; header\nQuality=Medium\nRayTracing=off\n")
        (config_dir / "preset_performance.ini").write_text("Quality=Low\nRayTracing=off\n")
        self.assertEqual(pcc.list_presets(str(config_dir)), ["performance"])
        pcc.apply_preset(str(config_dir), "performance")
        applied = (config_dir / "UltraPlusConfig.ini").read_text()
        self.assertIn("Quality=Low", applied)
        self.assertIn("; header", applied)

    # ---- Settings editor (parser_friendly_settings.ini + override JSONs) ----

    def test_parse_parser_friendly_settings_real_shape(self):
        content = (
            'BetterReflectionSDFs.Comment="game/off/on; improves quality"\n'
            "BetterReflectionSDFs.Type=enum\n"
            "BetterReflectionSDFs.Default=game\n"
            "BetterReflectionSDFs.Category=Advanced\n"
            "BetterReflectionSDFs.UserSettings=game,off,on\n"
            "\n"
            'Bloom.Comment="Bloom intensity (0.0 = off)"\n'
            "Bloom.Type=numeric\n"
            "Bloom.ValueType=float\n"
            "Bloom.Min=0\n"
            "Bloom.Max=3\n"
            "Bloom.Step=0.1\n"
        )
        parsed = pcc._parse_parser_friendly_settings(content)
        self.assertEqual(parsed["BetterReflectionSDFs"]["comment"], "game/off/on; improves quality")
        self.assertEqual(parsed["BetterReflectionSDFs"]["type"], "enum")
        self.assertEqual(parsed["BetterReflectionSDFs"]["usersettings"], "game,off,on")
        self.assertEqual(parsed["Bloom"]["type"], "numeric")
        self.assertEqual(parsed["Bloom"]["min"], "0")
        self.assertEqual(parsed["Bloom"]["step"], "0.1")

    def test_synthesize_numeric_options_int_and_float(self):
        self.assertEqual(pcc._synthesize_numeric_options(0, 3, 1, "int"), ["0", "1", "2", "3"])
        self.assertEqual(pcc._synthesize_numeric_options(0, 1, 0.5, "float"), ["0", "0.5", "1"])

    def test_synthesize_numeric_options_rejects_bad_ranges(self):
        self.assertEqual(pcc._synthesize_numeric_options(5, 0, 1, "int"), [])  # hi < lo
        self.assertEqual(pcc._synthesize_numeric_options(0, 10, 0, "int"), [])  # step<=0
        self.assertEqual(pcc._synthesize_numeric_options("game", 3, 1, "int"), [])  # non-numeric

    def test_synthesize_numeric_options_caps_step_count(self):
        # A malformed/huge range must not synthesize an unbounded list.
        self.assertEqual(len(pcc._synthesize_numeric_options(0, 1000000, 0.0001, "float")), 1000)

    def _fake_settings_config_dir(self):
        config_dir = Path(self.tmp.name) / "settings_config"
        config_dir.mkdir()
        (config_dir / "UltraPlusConfig.ini").write_text(
            "; header\n"
            "BetterReflectionSDFs=game\n"
            "Bloom=1.0\n"
            "LegacyNoSchema=off\n"
        )
        (config_dir / "UltraPlusConfig.default").write_text(
            "; header\n"
            "BetterReflectionSDFs=game\n"
            "Bloom=1.0\n"
            "LegacyNoSchema=off\n"
        )
        (config_dir / "parser_friendly_settings.ini").write_text(
            'BetterReflectionSDFs.Comment="!Mod-provided description wins"\n'
            "BetterReflectionSDFs.Type=enum\n"
            "BetterReflectionSDFs.Default=game\n"
            "BetterReflectionSDFs.Category=Advanced\n"
            "BetterReflectionSDFs.UserSettings=game,off,on\n"
            "\n"
            'Bloom.Comment="Bloom intensity"\n'
            "Bloom.Type=numeric\n"
            "Bloom.ValueType=float\n"
            "Bloom.Min=0\n"
            "Bloom.Max=2\n"
            "Bloom.Step=0.5\n"
        )
        return config_dir

    def test_list_mod_settings_enum_and_numeric_and_missing_schema(self):
        config_dir = self._fake_settings_config_dir()
        overrides = {
            "categories": {"Reflections": ["BetterReflectionSDFs"]},
            "descriptions": {"BetterReflectionSDFs": "Override description - should NOT win"},
            "names": {"Bloom": "Bloom Intensity"},
        }
        settings = {s["key"]: s for s in pcc.list_mod_settings(config_dir, overrides)}
        self.assertEqual(set(settings), {"BetterReflectionSDFs", "Bloom", "LegacyNoSchema"})

        sdf = settings["BetterReflectionSDFs"]
        self.assertEqual(sdf["type"], "enum")
        self.assertEqual(sdf["options"], ["game", "off", "on"])
        self.assertEqual(sdf["category"], "Reflections")
        self.assertTrue(sdf["advanced"])
        # Comment starts with '!' -> mod's own description wins over the override.
        self.assertEqual(sdf["description"], "Mod-provided description wins")

        bloom = settings["Bloom"]
        self.assertEqual(bloom["type"], "numeric")
        self.assertEqual(bloom["options"], ["0", "0.5", "1", "1.5", "2"])
        self.assertEqual(bloom["name"], "Bloom Intensity")
        self.assertEqual(bloom["category"], "Other")  # not in the categories override

        legacy = settings["LegacyNoSchema"]
        self.assertEqual(legacy["type"], "enum")  # no schema entry at all -> enum fallback
        self.assertEqual(legacy["options"], [])
        self.assertEqual(legacy["value"], "off")

    def test_list_mod_settings_description_override_wins_without_bang(self):
        config_dir = self._fake_settings_config_dir()
        # Remove the '!' so the override should win instead of the mod's own comment.
        content = (config_dir / "parser_friendly_settings.ini").read_text()
        content = content.replace('"!Mod-provided description wins"', '"Plain mod comment"')
        (config_dir / "parser_friendly_settings.ini").write_text(content)
        overrides = {"categories": {}, "descriptions": {"BetterReflectionSDFs": "Curated override"},
                    "names": {}}
        settings = {s["key"]: s for s in pcc.list_mod_settings(config_dir, overrides)}
        self.assertEqual(settings["BetterReflectionSDFs"]["description"], "Curated override")

    def test_set_mod_setting_preserves_comments_and_appends_new_key(self):
        config_dir = self._fake_settings_config_dir()
        pcc.set_mod_setting(str(config_dir), "Bloom", "1.5")
        content = (config_dir / "UltraPlusConfig.ini").read_text()
        self.assertIn("Bloom=1.5", content)
        self.assertIn("; header", content)
        self.assertTrue((config_dir / "config_modified").is_file())

        pcc.set_mod_setting(str(config_dir), "BrandNewKey", "on")
        content = (config_dir / "UltraPlusConfig.ini").read_text()
        self.assertIn("BrandNewKey=on", content)

    def test_set_mod_setting_requires_existing_ini(self):
        config_dir = Path(self.tmp.name) / "no_ini"
        config_dir.mkdir()
        with self.assertRaises(RuntimeError):
            pcc.set_mod_setting(str(config_dir), "Bloom", "1.5")

    def test_restore_mod_defaults(self):
        config_dir = self._fake_settings_config_dir()
        pcc.set_mod_setting(str(config_dir), "Bloom", "2.0")
        self.assertIn("Bloom=2.0", (config_dir / "UltraPlusConfig.ini").read_text())
        pcc.restore_mod_defaults(str(config_dir))
        self.assertIn("Bloom=1.0", (config_dir / "UltraPlusConfig.ini").read_text())

    # ---- Addons ----

    def _fake_addon_catalog(self):
        return {"games": {"TestGame": {
            "full_name": "Test Game", "search_terms": ["testgame"], "url": "",
            "exe_path": "Binaries/Win64/TestGame-Win64-Shipping.exe",
            "ue_game_path": "", "install_root_only": False, "mod_filename_prefixes": [],
            "addons": [{"Name": "Disable VRS", "FileName": "DisableVRS", "Description": "Turns off VRS"}],
        }}}

    class _FakeAddonResp:
        def __init__(self, payload):
            self.payload, self._pos = payload, 0
            self.headers = {"Content-Length": str(len(payload))}
        def read(self, n=None):
            if n is None:
                c, self._pos = self.payload[self._pos:], len(self.payload); return c
            c = self.payload[self._pos:self._pos + n]; self._pos += n; return c
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def test_list_addons_matches_manifest_filename_prefix(self):
        d, exe = self._fake_ue_game()
        catalog = self._fake_addon_catalog()
        real_fetch = pcc._fetch_json
        pcc._fetch_json = lambda url: {"files": [
            {"filename": "TestGame DisableVRS Ultra Plus v1.0.0.zip",
             "url": "http://x/a.zip", "updated": "2026-01-01T00:00:00"},
            {"filename": "OtherGame DisableVRS Ultra Plus v1.0.0.zip",
             "url": "http://x/b.zip", "updated": "2026-01-01T00:00:00"},
        ]} if "addons_manifest" in url else real_fetch(url)
        try:
            addons = pcc.list_addons("TestGame", catalog, "999")
        finally:
            pcc._fetch_json = real_fetch
        self.assertEqual(len(addons), 1)
        self.assertEqual(addons[0]["file_name"], "DisableVRS")
        self.assertEqual(len(addons[0]["versions"]), 1)
        self.assertEqual(addons[0]["versions"][0]["version"], "1.0.0")
        self.assertIsNone(addons[0]["installed"])

    def test_install_addon_routes_pak_and_asi_then_remove(self):
        d, exe = self._fake_ue_game()
        catalog = self._fake_addon_catalog()

        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("DisableVRS_P.pak", "pak data")
            zf.writestr("DisableVRS.asi", "asi data")
        pcc.urllib.request.urlopen = lambda req, timeout=0: self._FakeAddonResp(buf.getvalue())

        r = pcc.install_addon("999", str(d), "TestGame", catalog, "DisableVRS",
                              "http://x/DisableVRS.zip", "TestGame DisableVRS Ultra Plus v1.0.0.zip")
        self.assertTrue(r["installed"])
        self.assertEqual(r["files"], 2)
        pak_path = d / "Content/Paks/~mods/DisableVRS_P.pak"
        asi_path = exe.parent / "DisableVRS.asi"
        self.assertTrue(pak_path.is_file())
        self.assertTrue(asi_path.is_file())

        rec = pcc.load_state()["addon_installs"]["999"]["DisableVRS"]
        self.assertEqual(rec["version"], "1.0.0")
        self.assertEqual(set(rec["installed_files"]), {str(pak_path), str(asi_path)})

        removed = pcc.remove_addon("999", "DisableVRS")
        self.assertTrue(removed["removed"])
        self.assertFalse(pak_path.exists())
        self.assertFalse(asi_path.exists())
        with self.assertRaises(RuntimeError):
            pcc.remove_addon("999", "DisableVRS")

    def test_install_addon_rejects_path_traversal(self):
        d, exe = self._fake_ue_game()
        catalog = self._fake_addon_catalog()
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../evil.pak", "pwned")
        pcc.urllib.request.urlopen = lambda req, timeout=0: self._FakeAddonResp(buf.getvalue())
        with self.assertRaises(ValueError):
            pcc.install_addon("999", str(d), "TestGame", catalog, "DisableVRS",
                              "http://x/DisableVRS.zip", "TestGame DisableVRS Ultra Plus v1.0.0.zip")

    def test_install_mod_full_flow_and_update_and_remove(self):
        d, exe = self._fake_ue_game()
        catalog = self._fake_catalog()

        def build_zip(quality_value):
            import io, zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("dwmapi.dll", "loader")
                zf.writestr("Binaries/Win64/ue4ss/UE4SS.dll", "ue4ss")
                zf.writestr(
                    "Binaries/Win64/ue4ss/Mods/UltraPlusExtensions/scripts/config/UltraPlusConfig.ini",
                    f"; Ultra+ config\nQuality={quality_value}\nRayTracing=off\n")
                zf.writestr(
                    "Binaries/Win64/ue4ss/Mods/UltraPlusExtensions/scripts/config/preset_performance.ini",
                    "Quality=Low\n")
                zf.writestr("Content/Paks/~mods/999_TestGame_P.pak", "pak data")
            return buf.getvalue()

        class FakeResp:
            def __init__(self, payload):
                self.payload, self._pos = payload, 0
                self.headers = {"Content-Length": str(len(payload))}
            def read(self, n=None):
                if n is None:
                    c, self._pos = self.payload[self._pos:], len(self.payload)
                    return c
                c = self.payload[self._pos:self._pos + n]
                self._pos += n
                return c
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def checked_urlopen(payload):
            # A raw space in the request would make real http.client raise
            # "URL can't contain control characters" - assert it never
            # reaches urlopen un-encoded (regression: RoboCop's manifest URL).
            def _open(req, timeout=0):
                self.assertNotIn(" ", req.full_url)
                return FakeResp(payload)
            return _open

        zip_v1 = build_zip("Medium")
        pcc.urllib.request.urlopen = checked_urlopen(zip_v1)

        r = pcc.install_mod("12345", str(d), "TestGame", catalog,
                            "http://example/mod v1.0.0.zip", "TestGame Ultra Plus v1.0.0.zip",
                            skip_ue4ss=False)
        self.assertTrue(r["installed"])
        self.assertEqual(r["version"], "1.0.0")

        config_path = exe.parent / "ue4ss/Mods/UltraPlusExtensions/scripts/config/UltraPlusConfig.ini"
        self.assertTrue((exe.parent / "dwmapi.dll").is_file())
        self.assertTrue((exe.parent / "ue4ss/UE4SS.dll").is_file())
        self.assertTrue((d / "Content/Paks/~mods/999_TestGame_P.pak").is_file())
        self.assertIn("Quality=Medium", config_path.read_text())
        self.assertFalse(Path(str(config_path) + ".incoming").exists())  # staged file must not linger

        rec = pcc.load_state()["mod_installs"]["12345"]
        self.assertEqual(rec["version"], "1.0.0")
        self.assertIn(str(config_path), rec["installed_files"])

        # Simulate the user editing a setting, then reinstalling a new
        # version - the edit must survive the merge.
        config_path.write_text(config_path.read_text().replace("Quality=Medium", "Quality=Ultra"))
        zip_v2 = build_zip("Medium")  # shipped default unchanged in v2
        pcc.urllib.request.urlopen = checked_urlopen(zip_v2)
        r2 = pcc.install_mod("12345", str(d), "TestGame", catalog,
                             "http://example/mod v1.0.1.zip", "TestGame Ultra Plus v1.0.1.zip",
                             skip_ue4ss=False)
        self.assertEqual(r2["version"], "1.0.1")
        self.assertIn("Quality=Ultra", config_path.read_text())
        self.assertFalse(Path(str(config_path) + ".incoming").exists())
        default_path = config_path.with_suffix(".default")
        self.assertIn("Quality=Medium", default_path.read_text())

        removed = pcc.remove_mod("12345")
        self.assertTrue(removed["removed"])
        self.assertFalse((exe.parent / "dwmapi.dll").exists())
        with self.assertRaises(RuntimeError):
            pcc.remove_mod("12345")

    def test_install_mod_reinstall_with_fewer_files_cleans_up_orphans(self):
        # Regression: RoboCop's v0.1.1 archive doesn't ship keybinds.ini/
        # changelog.txt/preset_*.ini that v1.0.0 does. Installing v1.0.0
        # then "updating" to v0.1.1 must delete those now-orphaned files,
        # not just silently stop tracking them (which left Remove unable
        # to clean them up).
        d, exe = self._fake_ue_game()
        catalog = self._fake_catalog()

        class FakeResp:
            def __init__(self, payload):
                self.payload, self._pos = payload, 0
                self.headers = {"Content-Length": str(len(payload))}
            def read(self, n=None):
                if n is None:
                    c, self._pos = self.payload[self._pos:], len(self.payload)
                    return c
                c = self.payload[self._pos:self._pos + n]
                self._pos += n
                return c
            def __enter__(self): return self
            def __exit__(self, *a): pass

        import io, zipfile

        def build_big_zip():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("dwmapi.dll", "loader")
                zf.writestr(
                    "Binaries/Win64/ue4ss/Mods/UltraPlusExtensions/scripts/config/UltraPlusConfig.ini",
                    "Quality=Medium\n")
                zf.writestr(
                    "Binaries/Win64/ue4ss/Mods/UltraPlusExtensions/scripts/config/keybinds.ini",
                    "Sprint=Shift\n")
                zf.writestr(
                    "Binaries/Win64/ue4ss/Mods/UltraPlusExtensions/scripts/config/changelog.txt",
                    "notes")
            return buf.getvalue()

        def build_small_zip():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("dwmapi.dll", "loader")
                zf.writestr(
                    "Binaries/Win64/ue4ss/Mods/UltraPlusExtensions/scripts/config/UltraPlusConfig.ini",
                    "Quality=Medium\n")
            return buf.getvalue()

        pcc.urllib.request.urlopen = lambda req, timeout=0: FakeResp(build_big_zip())
        pcc.install_mod("12345", str(d), "TestGame", catalog,
                        "http://example/mod v1.0.0.zip", "TestGame Ultra Plus v1.0.0.zip",
                        skip_ue4ss=False)
        keybinds = exe.parent / "ue4ss/Mods/UltraPlusExtensions/scripts/config/keybinds.ini"
        changelog = exe.parent / "ue4ss/Mods/UltraPlusExtensions/scripts/config/changelog.txt"
        self.assertTrue(keybinds.is_file())
        self.assertTrue(changelog.is_file())

        pcc.urllib.request.urlopen = lambda req, timeout=0: FakeResp(build_small_zip())
        r = pcc.install_mod("12345", str(d), "TestGame", catalog,
                            "http://example/mod v0.1.1.zip", "TestGame Ultra Plus v0.1.1.zip",
                            skip_ue4ss=False)
        self.assertEqual(r["version"], "0.1.1")
        self.assertFalse(keybinds.exists(), "orphaned file from the old version must be deleted")
        self.assertFalse(changelog.exists(), "orphaned file from the old version must be deleted")

        rec = pcc.load_state()["mod_installs"]["12345"]
        self.assertNotIn(str(keybinds), rec["installed_files"])
        self.assertNotIn(str(changelog), rec["installed_files"])

    def test_install_mod_rejects_path_traversal_archive(self):
        d, exe = self._fake_ue_game()
        catalog = self._fake_catalog()

        class FakeResp:
            def __init__(self, payload):
                self.payload, self._pos = payload, 0
                self.headers = {"Content-Length": str(len(payload))}
            def read(self, n=None):
                if n is None:
                    c, self._pos = self.payload[self._pos:], len(self.payload)
                    return c
                c = self.payload[self._pos:self._pos + n]
                self._pos += n
                return c
            def __enter__(self): return self
            def __exit__(self, *a): pass

        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../../etc/evil.txt", "pwned")
        pcc.urllib.request.urlopen = lambda req, timeout=0: FakeResp(buf.getvalue())

        with self.assertRaises(ValueError):
            pcc.install_mod("12345", str(d), "TestGame", catalog,
                            "http://example/mod.zip", "TestGame Ultra Plus v1.0.0.zip",
                            skip_ue4ss=False)
        self.assertFalse((Path(self.tmp.name) / "etc/evil.txt").exists())

    def test_swap_refuses_type_mismatch(self):
        game_dll = self.root / "steamapps/common/TestGame/Engine/nvngx_dlss.dll"
        wrong = pcc.DLL_LIBRARY / "nvngx_dlssg.dll"
        wrong.write_bytes(b"x")
        with self.assertRaises(RuntimeError):
            pcc.swap_dll(str(game_dll), str(wrong))

    # ---- ReShade ----
    def test_detect_graphics_api_priority_and_dxgi_inference(self):
        """DX12 > Vulkan > DX11 > DX10 > OpenGL > DX9 > DX8, and a dxgi.dll
        import with nothing higher-priority present is inferred as DX12 (many
        modern engines create their device through DXGI alone)."""
        self.assertEqual(pcc.detect_graphics_api({"d3d11.dll", "d3d9.dll"}), "d3d11")
        self.assertEqual(pcc.detect_graphics_api({"opengl32.dll"}), "opengl")
        self.assertEqual(pcc.detect_graphics_api({"dxgi.dll"}), "d3d12")
        self.assertEqual(pcc.detect_graphics_api({"dxgi.dll", "d3d11.dll"}), "d3d11")
        self.assertIsNone(pcc.detect_graphics_api(set()))

    def test_find_game_exe_skips_installers_picks_largest(self):
        d = self.root / "steamapps/common/TestGame"
        (d / "UnInstall.exe").write_bytes(b"x" * 500)
        (d / "EasyAntiCheat_Setup.exe").write_bytes(b"x" * 5000)
        (d / "Binaries/Win64").mkdir(parents=True, exist_ok=True)
        real = d / "Binaries/Win64/Game-Win64-Shipping.exe"
        real.write_bytes(b"x" * 2000)
        found = pcc._find_game_exe(d)
        self.assertEqual(found, real)

    def _fake_exe(self):
        d = self.root / "steamapps/common/TestGame"
        exe = d / "Game.exe"
        exe.write_bytes(b"MZ" + b"\x00" * 62)   # not a real PE; api_override bypasses detection
        return d, exe

    def test_install_reshade_refuses_foreign_dll(self):
        d, exe = self._fake_exe()
        (d / "dxgi.dll").write_bytes(b"not ours")
        with self.assertRaises(RuntimeError) as ctx:
            pcc.install_reshade("12345", str(d), api_override="d3d11")
        self.assertIn("wasn't installed by Command Center", str(ctx.exception))

    def test_install_reshade_full_flow_and_update_and_remove(self):
        d, exe = self._fake_exe()

        engine_dir = pcc.RESHADE_DIR / "9.9.9"
        engine_dir.mkdir(parents=True)
        (engine_dir / "ReShade64.dll").write_bytes(b"fake reshade64")
        (engine_dir / "ReShade32.dll").write_bytes(b"fake reshade32")
        real_latest, real_ensure_engine, real_ensure_shaders = (
            pcc.reshade_latest, pcc.ensure_reshade_engine, pcc.ensure_default_shaders)
        pcc.reshade_latest = lambda: {"version": "9.9.9", "url": "http://x"}
        pcc.ensure_reshade_engine = lambda version, url=None, task_id=None: engine_dir
        pcc.ensure_default_shaders = lambda task_id=None: pcc.RESHADE_SHADERS_DIR
        try:
            r = pcc.install_reshade("12345", str(d), api_override="d3d11")
        finally:
            pcc.reshade_latest = real_latest
            pcc.ensure_reshade_engine = real_ensure_engine
            pcc.ensure_default_shaders = real_ensure_shaders

        self.assertEqual(r["proxy_dll"], "dxgi.dll")
        self.assertEqual(r["winedlloverride"], "dxgi=n,b")
        self.assertTrue(r["wrote_ini"])
        target = d / "dxgi.dll"
        self.assertEqual(target.read_bytes(), b"fake reshade64")
        ini = (d / "ReShade.ini").read_text()
        self.assertIn("EffectSearchPaths=", ini)

        # updating (same appid, same target, now tracked as ours) must be
        # allowed even though the target already exists.
        (engine_dir / "ReShade64.dll").write_bytes(b"fake reshade64 v2")
        pcc.reshade_latest = lambda: {"version": "9.9.9", "url": "http://x"}
        pcc.ensure_reshade_engine = lambda version, url=None, task_id=None: engine_dir
        pcc.ensure_default_shaders = lambda task_id=None: pcc.RESHADE_SHADERS_DIR
        try:
            r2 = pcc.install_reshade("12345", str(d), api_override="d3d11")
        finally:
            pcc.reshade_latest = real_latest
            pcc.ensure_reshade_engine = real_ensure_engine
            pcc.ensure_default_shaders = real_ensure_shaders
        self.assertTrue(r2["installed"])
        self.assertEqual(target.read_bytes(), b"fake reshade64 v2")
        self.assertFalse(r2["wrote_ini"])          # existing ini left alone

        rm = pcc.remove_reshade("12345")
        self.assertTrue(rm["removed"])
        self.assertFalse(target.exists())
        with self.assertRaises(RuntimeError):
            pcc.remove_reshade("12345")   # nothing tracked any more

    # ---- compile state ----
    def test_sgdb_fetch_and_cache(self):
        pcc.save_config({"sgdb_api_key": "k3y"})
        calls = []

        class FakeResp:
            def __init__(self, payload, ct="application/json"):
                self.payload, self.headers = payload, {"Content-Type": ct}
            def read(self): return self.payload
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_urlopen(req, timeout=0):
            calls.append(req.full_url)
            if "steamstatic.com" in req.full_url:
                raise OSError("404")           # CDN miss -> cascade continues
            if "/grids/steam/" in req.full_url:
                assert req.headers.get("Authorization") == "Bearer k3y"
                return FakeResp(json.dumps(
                    {"data": [{"url": "https://x/img.png"}]}).encode())
            return FakeResp(b"\x89PNG\r\n\x1a\n" + b"realdata", ct="image/png")

        pcc.urllib.request.urlopen = fake_urlopen
        img, ct = pcc.sgdb_art("777")
        self.assertEqual(ct, "image/png")
        pcc.sgdb_art("777")  # cache hit — no new network calls
        self.assertEqual(len(calls), 3)  # CDN miss + grids + image

    def test_sgdb_no_key_returns_none(self):
        pcc.save_config({"sgdb_api_key": ""})
        def cdn_down(req, timeout=0):
            raise OSError("404")
        pcc.urllib.request.urlopen = cdn_down
        self.assertIsNone(pcc.sgdb_art("888"))

    # ---- benchmarks (ported from Stutterless) ----
    def test_mangohud_csv_and_analysis(self):
        p = Path(self.tmp.name) / "log.csv"
        rows = ["os,cpu,gpu,ram,kernel,driver", "x,x,x,x,x,x",
                "fps,frametime,cpu_load"]
        rows += [f"120,{8300 if i % 50 else 45000},50" for i in range(200)]
        p.write_text("\n".join(rows))
        ft = pcc._parse_mangohud_csv(p)
        self.assertGreaterEqual(len(ft), 190)
        an = pcc._analyse_frametimes(ft)
        self.assertGreater(an["avg_fps"], 90)
        self.assertGreaterEqual(an["stutter_count"], 3)
        ds = pcc._downsample(ft, target=40)
        self.assertLessEqual(len(ds), 45)
        self.assertGreater(max(ds), 40)  # spikes preserved

    def test_smart_cache_clear_keeps_recordings(self):
        cache = self.root / "steamapps/shadercache/12345"
        (cache / "compiled_artifact.foz").write_bytes(b"compiled")
        r = pcc.clear_cache(self.root, "12345", keep_recordings=True)
        self.assertTrue(
            (cache / "fozpipelinesv6/steam_pipeline_cache.foz").exists())
        self.assertFalse((cache / "compiled_artifact.foz").exists())
        self.assertEqual(r["kept_recordings"], 1)

    def test_skip_list(self):
        self.assertIn("1493710", pcc.SKIP_APPIDS)
        self.assertTrue(pcc.SKIP_NAME_RE.match("Steam Linux Runtime 3.0"))

    def test_download_sr_part_file_bug(self):
        """Regression: downloader must not hand a .part-named file to import."""
        import struct as st
        blob = (b"MZ" + b"\x00" * 200 + st.pack("<I", 0xFEEF04BD)
                + st.pack("<I", 0x00010000)
                + st.pack("<II", (310 << 16) | 4, 0) + b"\x00" * 100)

        class FakeResp:
            def __init__(self, payload, ct="application/json"):
                self.payload, self._pos = payload, 0
                self.headers = {"Content-Type": ct,
                                "Content-Length": str(len(payload))}
            def read(self, n=None):
                if n is None:
                    return self.payload
                c = self.payload[self._pos:self._pos + n]
                self._pos += n
                return c
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake(req, timeout=0):
            u = req.full_url
            if u.endswith("/repos/NVIDIA/DLSS"):
                return FakeResp(json.dumps({"default_branch": "main"}).encode())
            if "git/trees" in u:
                return FakeResp(json.dumps({"tree": [
                    {"path": "lib/Windows_x86_64/rel/nvngx_dlss.dll",
                     "type": "blob"}]}).encode())
            return FakeResp(blob, ct="application/octet-stream")

        pcc.urllib.request.urlopen = fake
        pcc.download_latest_sr("task1")
        self.assertEqual(pcc.TASKS["task1"]["status"], "done",
                         pcc.TASKS["task1"])

    # ---- compat tools / launch / status semantics ----
    def _mock_config_vdf(self):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config/config.vdf").write_text(
            '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n'
            '\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t}\n\t\t}\n\t}\n}\n')

    def test_compat_tool_roundtrip(self):
        self._mock_config_vdf()
        td = self.root / "compatibilitytools.d/ge"
        td.mkdir(parents=True)
        (td / "compatibilitytool.vdf").write_text(
            '"compatibilitytools"\n{\n\t"compat_tools"\n\t{\n'
            '\t\t"GE-Proton10-4"\n\t\t{\n\t\t\t"display_name"\t\t'
            '"GE-Proton 10-4"\n\t\t}\n\t}\n}\n')
        names = [t["name"] for t in pcc.list_compat_tools(self.root)]
        self.assertIn("GE-Proton10-4", names)
        pcc.set_compat_tool(self.root, "12345", "GE-Proton10-4")
        self.assertEqual(pcc.get_compat_tool(self.root, "12345")["name"],
                         "GE-Proton10-4")
        pcc.set_compat_tool(self.root, "12345", "")
        self.assertEqual(pcc.get_compat_tool(self.root, "12345")["name"], "")

    def test_session_env_no_crash_without_display(self):
        env = pcc.session_env()
        self.assertIsInstance(env, dict)

    # ---- friendly versions + multi-repo downloader ----
    def test_friendly_dlss_versions(self):
        self.assertEqual(pcc.friendly_dlss("310.2.1.0"),
                         {"gen": "DLSS 4", "short": "310.2.1"})
        self.assertEqual(pcc.friendly_dlss("3.7.10.0")["gen"], "DLSS 3")
        self.assertTrue(pcc.version_tuple("310.4.0.0")
                        > pcc.version_tuple("310.2.1.0"))

    def test_download_dlss_tree_search(self):
        import struct as st
        blob = (b"MZ" + b"\x00" * 200 + st.pack("<I", 0xFEEF04BD)
                + st.pack("<I", 0x00010000)
                + st.pack("<II", (310 << 16) | 4, 0) + b"\x00" * 100)

        class FakeResp:
            def __init__(self, payload, ct="application/json"):
                self.payload, self._pos = payload, 0
                self.headers = {"Content-Type": ct,
                                "Content-Length": str(len(payload))}
            def read(self, n=None):
                if n is None:
                    return self.payload
                c = self.payload[self._pos:self._pos + n]
                self._pos += n
                return c
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake(req, timeout=0):
            u = req.full_url
            if u.endswith("/repos/NVIDIAGameWorks/Streamline"):
                return FakeResp(json.dumps({"default_branch": "main"}).encode())
            if "git/trees" in u:
                return FakeResp(json.dumps({"tree": [
                    {"path": "bin/x64/nvngx_dlssg.dll", "type": "blob"}]}).encode())
            return FakeResp(blob, ct="application/octet-stream")

        pcc.urllib.request.urlopen = fake
        pcc.download_dlss("t_fg", "fg")
        self.assertEqual(pcc.TASKS["t_fg"]["status"], "done",
                         pcc.TASKS["t_fg"])

    # ---- hardened downloader + system compat dirs ----
    def _fake_resp_class(self):
        class FakeResp:
            def __init__(self, payload, ct="application/json"):
                self.payload, self._pos = payload, 0
                self.headers = {"Content-Type": ct,
                                "Content-Length": str(len(payload))}
            def read(self, n=None):
                if n is None:
                    return self.payload
                c = self.payload[self._pos:self._pos + n]
                self._pos += n
                return c
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return FakeResp

    def test_download_truncated_tree_and_lfs(self):
        import struct as st
        FakeResp = self._fake_resp_class()
        blob = (b"MZ" + b"\x00" * 200 + st.pack("<I", 0xFEEF04BD)
                + st.pack("<I", 0x00010000)
                + st.pack("<II", (310 << 16) | 5, 0) + b"\x00" * 100)
        lfs = b"version https://git-lfs.github.com/spec/v1\noid sha256:x\nsize 1\n"

        def fake(req, timeout=0):
            u = req.full_url
            if u.endswith("/repos/NVIDIAGameWorks/Streamline"):
                return FakeResp(json.dumps({"default_branch": "main"}).encode())
            if "git/trees" in u:
                return FakeResp(json.dumps(
                    {"tree": [], "truncated": True}).encode())
            if "/contents/bin/x64?" in u:
                return FakeResp(json.dumps(
                    [{"name": "nvngx_dlssg.dll",
                      "path": "bin/x64/nvngx_dlssg.dll"}]).encode())
            if "/contents/" in u:
                raise OSError("404")
            if "raw.githubusercontent" in u:
                return FakeResp(lfs, ct="application/octet-stream")
            if "media.githubusercontent" in u:
                return FakeResp(blob, ct="application/octet-stream")
            raise OSError("unexpected " + u)

        pcc.urllib.request.urlopen = fake
        pcc.download_dlss("t_lfs", "fg")
        self.assertEqual(pcc.TASKS["t_lfs"]["status"], "done",
                         pcc.TASKS["t_lfs"])

    def test_download_release_zip_fallback(self):
        import struct as st
        import io
        import zipfile
        FakeResp = self._fake_resp_class()
        blob = (b"MZ" + b"\x00" * 200 + st.pack("<I", 0xFEEF04BD)
                + st.pack("<I", 0x00010000)
                + st.pack("<II", (310 << 16) | 5, 0) + b"\x00" * 100)
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as z:
            z.writestr("sdk/bin/x64/nvngx_dlssd.dll", blob)
        zb = zbuf.getvalue()

        def fake(req, timeout=0):
            u = req.full_url
            if u.endswith("/repos/NVIDIAGameWorks/Streamline"):
                return FakeResp(json.dumps({"default_branch": "main"}).encode())
            if "git/trees" in u:
                return FakeResp(json.dumps(
                    {"tree": [], "truncated": True}).encode())
            if "/contents/" in u:
                raise OSError("404")
            if "/releases/latest" in u:
                return FakeResp(json.dumps({"assets": [
                    {"name": "sdk.zip", "size": len(zb),
                     "browser_download_url": "https://gh/sdk.zip"}]}).encode())
            if "sdk.zip" in u:
                return FakeResp(zb, ct="application/zip")
            raise OSError("unexpected " + u)

        pcc.urllib.request.urlopen = fake
        pcc.download_dlss("t_zip", "rr")
        self.assertEqual(pcc.TASKS["t_zip"]["status"], "done",
                         pcc.TASKS["t_zip"])

    def test_compat_tools_extra_paths(self):
        extra = Path(self.tmp.name) / "sys-compat/proton-cachyos"
        extra.mkdir(parents=True)
        (extra / "compatibilitytool.vdf").write_text(
            '"compatibilitytools"\n{\n\t"compat_tools"\n\t{\n'
            '\t\t"proton-cachyos"\n\t\t{\n\t\t\t"display_name"\t\t'
            '"Proton-CachyOS"\n\t\t}\n\t}\n}\n')
        os.environ["STEAM_EXTRA_COMPAT_TOOLS_PATHS"] = str(extra.parent)
        try:
            names = [t["name"] for t in pcc.list_compat_tools(self.root)]
            self.assertIn("proton-cachyos", names)
        finally:
            del os.environ["STEAM_EXTRA_COMPAT_TOOLS_PATHS"]

    # ---- owned library ----
    def test_steamid_and_owned_games(self):
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config/loginusers.vdf").write_text(
            '"users"\n{\n\t"76561198012345678"\n\t{\n'
            '\t\t"MostRecent"\t\t"1"\n\t}\n}\n')
        self.assertEqual(pcc.steamid64(self.root), "76561198012345678")
        pcc.save_config({"steam_api_key": "K"})
        FakeResp = self._fake_resp_class()

        def fake(req, timeout=0):
            return FakeResp(json.dumps({"response": {"games": [
                {"appid": 42, "name": "Owned Game"}]}}).encode())

        pcc.urllib.request.urlopen = fake
        owned = pcc.owned_games(self.root)
        self.assertEqual(owned[0]["name"], "Owned Game")

    def test_owned_games_requires_key(self):
        pcc.save_config({"steam_api_key": ""})
        with self.assertRaises(RuntimeError):
            pcc.owned_games(self.root)

    # ---- auto-tune ----
    def test_steam_shader_settings_discovery_and_write(self):
        cfg = self.root / "userdata/12345678/config/localconfig.vdf"
        txt = cfg.read_text().replace(
            '"friends"',
            '"system"\n\t{\n\t\t"BackgroundShaderProcessing"\t\t"1"\n\t}\n\t"friends"', 1)
        cfg.write_text(txt)
        s = pcc.steam_shader_settings(self.root)
        self.assertTrue(s["found"])
        path = s["files"][0]["keys"][0]["path"]
        pcc.set_steam_shader_setting(self.root, s["files"][0]["file"], path, 0)
        s2 = pcc.steam_shader_settings(self.root)
        self.assertEqual(s2["files"][0]["keys"][0]["value"], "0")
        d = pcc.vdf_parse(cfg.read_text())
        self.assertEqual(
            d["UserLocalConfigStore"]["friends"]["VoiceReceiveVolume"], "0.75")

    def test_steam_shader_setting_rejects_odd_file(self):
        with self.assertRaises(RuntimeError):
            pcc.set_steam_shader_setting(self.root, "/etc/passwd", "a/b", 0)

    def test_backend_and_frontend_versions_match(self):
        """Guard: a missed version bump shipped 1.3.7 code labelled 1.3.0."""
        import re as _re
        here = Path(__file__).resolve().parent.parent
        be = _re.search(r'^VERSION = "([\d.]+)"',
                        (here / "pcc.py").read_text(), _re.M).group(1)
        fe = _re.search(r'FRONTEND_VERSION="([\d.]+)"',
                        (here / "index.html").read_text()).group(1)
        self.assertEqual(be, fe, "pcc.py and index.html versions must match")
        pk = _re.search(r'^pkgver=([\d.]+)',
                        (here / "PKGBUILD").read_text(), _re.M).group(1)
        self.assertEqual(be, pk, "PKGBUILD pkgver must match code version")

    def test_install_progress_states(self):
        m = self.root / "steamapps/appmanifest_999111.acf"
        (self.root / "steamapps/common/DL").mkdir(parents=True, exist_ok=True)
        m.write_text('"AppState"\n{\n\t"appid"\t\t"999111"\n'
                     '\t"name"\t\t"DL Game"\n\t"installdir"\t\t"DL"\n'
                     '\t"StateFlags"\t\t"1026"\n'
                     '\t"BytesDownloaded"\t\t"6400000000"\n'
                     '\t"BytesToDownload"\t\t"10000000000"\n}\n')
        g = {x["appid"]: x for x in pcc.install_progress(self.root)}["999111"]
        self.assertEqual(g["download_pct"], 64.0)
        self.assertFalse(g["fully_installed"])
        # a manifest with no pending bytes is never "installing"
        done = {x["appid"]: x for x in pcc.install_progress(self.root)}["12345"]
        self.assertTrue(done["fully_installed"])
        self.assertIsNone(done["download_pct"])

    # ---- hardware detection + MangoHud ----
    def test_nvidia_pci_normalisation(self):
        class R:
            returncode = 0
            stdout = "RTX 5070 Laptop GPU, 00000000:01:00.0, 8188 MiB, 610.43.02\n"
        real = pcc.subprocess.run
        pcc.subprocess.run = lambda *a, **k: R()
        try:
            g = pcc._nvidia_gpus()[0]
        finally:
            pcc.subprocess.run = real
        self.assertEqual(g["pci_dev"], "0000:01:00.0")
        self.assertEqual(g["vram_mb"], 8188)
        self.assertTrue(g["discrete"])

    def test_mangohud_config_pins_discrete_gpu(self):
        hw = {"cpu": "Test CPU", "cores": 16, "font": None, "hybrid": True,
              "gpus": [{"name": "RTX", "vendor": "NVIDIA", "pci_dev": "0000:01:00.0",
                        "vram_mb": 8188, "driver": "610", "discrete": True},
                       {"name": "iGPU", "vendor": "AMD", "pci_dev": "0000:65:00.0",
                        "vram_mb": None, "driver": None, "discrete": False}]}
        cfg = pcc.mangohud_config("benchmark", hw)
        self.assertIn("pci_dev=0000:01:00.0", cfg)
        self.assertIn("legacy_layout=false", cfg)
        self.assertIn("Test CPU", cfg)

    def test_mangohud_presets_have_no_disabled_params(self):
        """MangoHud 0.8.2 renders a column for every listed param, even =0."""
        for name, params in pcc.MANGOHUD_PRESETS.items():
            bad = [x for x in params if x.endswith("=0") and x != "frametime=0"]
            self.assertFalse(bad, f"{name}: {bad}")

    def test_mangohud_apply_backs_up(self):
        pcc.MANGOHUD_DIR = Path(self.tmp.name) / "MangoHud"
        pcc.MANGOHUD_DIR.mkdir(parents=True)
        (pcc.MANGOHUD_DIR / "MangoHud.conf").write_text("old\n")
        pcc._nvidia_gpus = lambda: []
        pcc._drm_gpus = lambda: []
        r = pcc.apply_mangohud_config("minimal")
        self.assertTrue(Path(r["written"]).read_text().startswith("### Generated"))
        self.assertEqual(Path(r["backup"]).read_text(), "old\n")

    def test_manifest_section_mapping(self):
        """SR/FG/RR map to the verified manifest section names."""
        self.assertEqual(pcc.DLSS_MANIFEST_SECTION["sr"], "dlss")
        self.assertEqual(pcc.DLSS_MANIFEST_SECTION["fg"], "dlss_g")
        self.assertEqual(pcc.DLSS_MANIFEST_SECTION["rr"], "dlss_d")

    def test_manifest_picks_highest_version(self):
        """The manifest parser must select the newest entry by version."""
        import io, zipfile, json as _json
        # build a fake manifest + a fake zip served via monkeypatched helpers
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("nvngx_dlss.dll", b"MZ" + b"\x00" * 100)
        zip_bytes = buf.getvalue()
        manifest = {"dlss": [
            {"version": "310.1.0.0", "version_number": 10,
             "download_url": "http://x/old.zip"},
            {"version": "310.5.2.0", "version_number": 99,
             "download_url": "http://x/new.zip"},
        ]}
        real_json, real_bytes = pcc._gh_json, pcc._gh_bytes
        pcc._gh_json = lambda url: manifest
        pcc._gh_bytes = lambda url, task=None: zip_bytes
        pcc.TASKS["t"] = {"status": "running", "progress": 0, "detail": ""}
        try:
            got = pcc._manifest_latest("sr", "t")
        finally:
            pcc._gh_json, pcc._gh_bytes = real_json, real_bytes
        self.assertIsNotNone(got)
        version, data = got
        self.assertEqual(version, "310.5.2.0")   # highest version_number wins
        self.assertTrue(data.startswith(b"MZ"))

    def test_pe_version_skips_false_signature(self):
        """Regression: a coincidental 0xFEEF04BD before the real version block
        produced garbage like 46863.0.46863.4696. Parser must validate the
        struct version and skip false matches."""
        import struct as _s, tempfile as _tf
        def make(blocks):
            data = b"\x00" * 64
            for struc, ms, ls in blocks:
                data += _s.pack("<I", 0xFEEF04BD)
                data += _s.pack("<I", struc)
                data += _s.pack("<II", ms, ls)
                data += b"\x00" * 32
            return data
        # garbage block (bad struc) followed by real DLSS 310.5.2.0
        blob = make([(0x12345678, 0xB6EF0000, 0xB6EF1250),
                     (0x00010000, (310 << 16) | 5, (2 << 16) | 0)])
        f = Path(_tf.mktemp(suffix=".dll"))
        f.write_bytes(blob)
        self.assertEqual(pcc.pe_version(f), "310.5.2.0")
        # garbage-only must return None, not the 46863 nonsense
        blob2 = make([(0x99999999, 0xB6EF0000, 0xB6EF1250)])
        f2 = Path(_tf.mktemp(suffix=".dll"))
        f2.write_bytes(blob2)
        self.assertIsNone(pcc.pe_version(f2))

    def test_dll_library_dedupes_same_version(self):
        """Two dirs holding the same real version (e.g. a garbage-named one from
        the old parser plus a correct one) collapse to a single entry."""
        import struct as _s
        def mk(a, b, c, d):
            data = b"\x00" * 64
            data += _s.pack("<I", 0xFEEF04BD) + _s.pack("<I", 0x00010000)
            data += _s.pack("<II", (a << 16) | b, (c << 16) | d) + b"\x00" * 32
            return b"MZ" + data
        lib = Path(self.tmp.name) / "dlls2"
        pcc.DLL_LIBRARY = lib
        fg = lib / "fg"
        blob = mk(310, 7, 0, 0)
        for name in ("46863.0.46863.4696", "310.7.0.0"):
            d = fg / name
            d.mkdir(parents=True)
            (d / "nvngx_dlssg.dll").write_bytes(blob)
        entries = [e for e in pcc.dll_library() if e["kind"] == "fg"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["version"], "310.7.0.0")
        self.assertTrue((fg / "310.7.0.0").exists())
        self.assertFalse((fg / "46863.0.46863.4696").exists())

    def test_scan_skips_development_dlls(self):
        """Debug DLSS copies in Development/ are not runtime DLLs; skip them."""
        import struct as _s
        def mk(a, b, c, d):
            data = b"MZ" + b"\x00" * 64 + _s.pack("<I", 0xFEEF04BD)
            data += _s.pack("<I", 0x00010000)
            data += _s.pack("<II", (a << 16) | b, (c << 16) | d) + b"\x00" * 32
            return data
        game = Path(self.tmp.name) / "game"
        rel = game / "Plugins" / "Win64"
        rel.mkdir(parents=True)
        (rel / "nvngx_dlssg.dll").write_bytes(mk(310, 7, 0, 0))
        dev = rel / "Development"
        dev.mkdir()
        (dev / "nvngx_dlssg.dll").write_bytes(mk(310, 1, 0, 0))
        found = pcc.scan_game_dlss(game)
        self.assertEqual(len(found), 1)
        self.assertNotIn("Development", found[0]["path"])

    def test_dlss_scan_climbs_for_engine_plugin_dlls(self):
        """Non-Steam shortcuts point install_path at the launch exe's own
        folder. Unreal Engine games ship DLSS under Engine/Plugins/.../
        ThirdParty/Win64, a sibling of the project's own Binaries/Win64 the
        exe sits in - a direct scan of install_path alone misses it, so the
        scan must climb toward the real game root."""
        import struct as _s
        def mk(a, b, c, d):
            data = b"MZ" + b"\x00" * 64 + _s.pack("<I", 0xFEEF04BD)
            data += _s.pack("<I", 0x00010000)
            data += _s.pack("<II", (a << 16) | b, (c << 16) | d) + b"\x00" * 32
            return data
        root = Path(self.tmp.name) / "Some Game"
        exe_dir = root / "Project" / "Binaries" / "Win64"
        exe_dir.mkdir(parents=True)
        plugin_dir = (root / "Engine" / "Plugins" / "External" / "DLSS" /
                     "Binaries" / "ThirdParty" / "Win64")
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "nvngx_dlss.dll").write_bytes(mk(310, 7, 0, 0))
        found = pcc.scan_game_dlss(str(exe_dir))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kind"], "sr")

    def test_dlss_scan_climb_is_bounded(self):
        """The climb-and-retry must not wander arbitrarily far up the tree -
        a DLL well outside the bound must not surface as a false match."""
        import struct as _s
        def mk(a, b, c, d):
            data = b"MZ" + b"\x00" * 64 + _s.pack("<I", 0xFEEF04BD)
            data += _s.pack("<I", 0x00010000)
            data += _s.pack("<II", (a << 16) | b, (c << 16) | d) + b"\x00" * 32
            return data
        tree = Path(self.tmp.name) / "a" / "b" / "c" / "d" / "e" / "f"
        tree.mkdir(parents=True)
        (tree.parents[4] / "nvngx_dlss.dll").write_bytes(mk(310, 7, 0, 0))
        found = pcc.scan_game_dlss(str(tree))
        self.assertEqual(found, [])

    def test_backup_export_and_restore(self):
        import tarfile
        data = Path(self.tmp.name) / "pccdata"
        data.mkdir()
        pcc.DATA_DIR = data
        (data / "state.json").write_text('{"x":1}')
        (data / "config.json").write_text('{"key":"secret"}')
        (data / "artcache").mkdir()
        (data / "artcache" / "j.jpg").write_bytes(b"x" * 100)
        out = Path(self.tmp.name) / "out"
        out.mkdir()
        r = pcc.export_backup(out)
        with tarfile.open(r["archive"]) as tar:
            names = tar.getnames()
        self.assertTrue(any("state.json" in n for n in names))
        self.assertFalse(any("artcache" in n for n in names))
        import shutil
        shutil.rmtree(data)
        data.mkdir()
        pcc.restore_backup(r["archive"])
        self.assertEqual((data / "state.json").read_text(), '{"x":1}')

    def test_ge_proton_list_flags_installed(self):
        import tempfile as _tf
        pcc.STATE_FILE = Path(_tf.mktemp())
        pcc.COMPAT_INSTALL_DIR = Path(self.tmp.name) / "compat"
        pcc.COMPAT_INSTALL_DIR.mkdir()
        (pcc.COMPAT_INSTALL_DIR / "GE-Proton9-27").mkdir()
        mock = [{"tag_name": "GE-Proton9-28", "name": "GE-Proton9-28",
                 "published_at": "2026-07-01T00:00:00Z",
                 "assets": [{"name": "GE-Proton9-28.tar.gz",
                             "browser_download_url": "http://x/28.tar.gz",
                             "size": 1}]},
                {"tag_name": "GE-Proton9-27", "name": "GE-Proton9-27",
                 "published_at": "2026-06-01T00:00:00Z",
                 "assets": [{"name": "GE-Proton9-27.tar.gz",
                             "browser_download_url": "http://x/27.tar.gz",
                             "size": 1}]}]
        real = pcc._gh_json
        pcc._gh_json = lambda u: mock
        try:
            r = pcc.list_ge_proton()
        finally:
            pcc._gh_json = real
        self.assertEqual(r["newest"], "GE-Proton9-28")
        self.assertFalse(r["up_to_date"])

    def test_ge_proton_selects_x86_not_arm(self):
        """GE-Proton 11+ ships aarch64 + x86_64 tarballs; must pick x86_64 even
        when the ARM build is listed first (regression: was grabbing ARM)."""
        import tempfile as _tf
        pcc.STATE_FILE = Path(_tf.mktemp())
        pcc.COMPAT_INSTALL_DIR = Path(self.tmp.name) / "compat_arch"
        pcc.COMPAT_INSTALL_DIR.mkdir()
        mock = [{"tag_name": "GE-Proton11-1", "name": "GE-Proton11-1",
                 "published_at": "2026-06-20T00:00:00Z",
                 "assets": [
                     {"name": "GE-Proton11-1-aarch64.tar.gz",
                      "browser_download_url": "http://x/arm.tar.gz", "size": 4},
                     {"name": "GE-Proton11-1.tar.gz",
                      "browser_download_url": "http://x/x86.tar.gz", "size": 4},
                 ]}]
        real = pcc._gh_json
        pcc._gh_json = lambda u: mock
        try:
            r = pcc.list_ge_proton()
        finally:
            pcc._gh_json = real
        self.assertEqual(r["releases"][0]["url"], "http://x/x86.tar.gz")

    def test_mangohud_short_names_and_order(self):
        """Overlay shows shortened labels (Ryzen AI 9 365 / RTX 5070) and
        orders CPU block before GPU block before the frame-time graph."""
        self.assertEqual(pcc._short_gpu_name("NVIDIA GeForce RTX 5070 Laptop GPU"),
                         "RTX 5070")
        self.assertEqual(pcc._short_cpu_name("AMD Ryzen AI 9 365 w/ Radeon 880M"),
                         "Ryzen AI 9 365")
        hw = {"cpu": "AMD Ryzen AI 9 365 w/ Radeon 880M", "cores": 10,
              "gpus": [{"name": "NVIDIA GeForce RTX 5070 Laptop GPU",
                        "vendor": "NVIDIA", "pci_dev": "0000:63:00.0",
                        "vram_mb": 8151, "discrete": True}],
              "hybrid": False, "font": None}
        cfg = pcc.mangohud_config("reference", hw)
        self.assertIn("cpu_text=Ryzen AI 9 365", cfg)
        self.assertIn("gpu_text=RTX 5070", cfg)
        self.assertLess(cfg.index("cpu_stats"), cfg.index("gpu_stats"))
        self.assertLess(cfg.index("gpu_stats"), cfg.index("frame_timing"))

    def test_stateflags_bitfield_and_stale_bytes(self):
        """StateFlags is a bitfield, and Steam leaves BytesDownloaded stale
        after a download finishes. Regression: a finished game showed a stuck
        percentage ("3% done" while Steam said installed), and queued/paused
        games read as 'forever downloading' because of a naive flags != 4."""
        # verified real-world values (see _is_installing docstring)
        self.assertFalse(pcc._is_installing(4))       # done
        self.assertTrue(pcc._is_installing(1026))     # fresh download
        self.assertTrue(pcc._is_installing(1062))     # repair
        self.assertFalse(pcc._is_installing(0))       # odd manifest

        lib = self.root / "steamapps"
        (lib / "common" / "DoneGame").mkdir(parents=True, exist_ok=True)
        # StateFlags says fully installed, but the byte counters are stale at 3%
        (lib / "appmanifest_555.acf").write_text(
            '"AppState"\n{\n"appid" "555"\n"name" "DoneGame"\n'
            '"installdir" "DoneGame"\n"StateFlags" "4"\n'
            '"BytesDownloaded" "3"\n"BytesToDownload" "100"\n'
            '"SizeOnDisk" "100"\n}\n')
        g = next(x for x in pcc.list_games(self.root) if x["appid"] == "555")
        self.assertTrue(g["fully_installed"])
        self.assertIsNone(g["download_pct"])   # NOT 3.0

    def test_pkgbuild_never_ships_skip_checksum(self):
        """Regression: the PKGBUILD template carried sha256sums=('SKIP'), which
        disables integrity checking for everyone who installs. It fails SILENTLY
        — makepkg prints 'Skipped' and builds happily — so a forgotten
        updpkgsums nearly published an unverified package. The placeholder is
        now a deliberately wrong hash, which fails loudly instead."""
        root = Path(__file__).resolve().parent.parent
        for rel in ("PKGBUILD", "aur/proton-command-center/PKGBUILD"):
            p = root / rel
            if not p.exists():
                continue
            body = "\n".join(l for l in p.read_text().splitlines()
                              if not l.lstrip().startswith("#"))
            self.assertNotIn("SKIP", body,
                             f"{rel} ships a SKIP checksum - run updpkgsums")
        s = root / "aur/proton-command-center/.SRCINFO"
        if s.exists():
            self.assertNotIn("sha256sums = SKIP", s.read_text(),
                             ".SRCINFO ships a SKIP checksum")

    def test_shader_threads_creates_missing_dev_cfg(self):
        """Steam never ships steam_dev.cfg -- it does not exist on a stock
        install -- so the override has to CREATE the file, not just edit it.
        Regression: the existing VDF writer only updates keys in files that
        already exist, so it would have silently done nothing here."""
        root = Path(self.tmp.name) / "steamroot"
        root.mkdir()
        real = pcc.logical_cores
        pcc.logical_cores = lambda: 16
        try:
            st = pcc.shader_threads_status(root)
            self.assertFalse(st["exists"])
            self.assertIsNone(st["current"])
            self.assertEqual(st["recommended"], 14)      # 16 - 2 reserved

            pcc.set_shader_threads(root, st["recommended"])
            cfg = root / "steam_dev.cfg"
            self.assertTrue(cfg.is_file(), "must create steam_dev.cfg")
            self.assertEqual(pcc.get_shader_threads(root), 14)

            # must not clobber unrelated lines, nor duplicate the key
            cfg.write_text("unSomethingElse 1\n"
                           "unShaderBackgroundProcessingThreads 4\n")
            pcc.set_shader_threads(root, 9)
            txt = cfg.read_text()
            self.assertIn("unSomethingElse 1", txt)
            self.assertEqual(txt.count("unShaderBackgroundProcessingThreads"), 1)
            self.assertEqual(pcc.get_shader_threads(root), 9)

            for bad in (0, 17, -1):
                with self.assertRaises(RuntimeError):
                    pcc.set_shader_threads(root, bad)
        finally:
            pcc.logical_cores = real

    def test_proton_capabilities_fail_open(self):
        """Builds differ: GE-Proton11-1 reads 29 vars Valve's Proton 11.0 does
        not, so a launch string valid under GE can be inert under Valve. The
        scan reads each build's launcher script - but it only sees what the
        SCRIPT reads. DXVK_NVAPI_VKREFLEX is consumed by the dxvk-nvapi DLL and
        appears in no proton script, yet works; treating unseen as unsupported
        would wrongly disable it. So absence only counts for vars we can prove
        we detect elsewhere (the union across builds); anything else fails open.
        """
        root = Path(self.tmp.name) / "sr"
        ge = root / "compatibilitytools.d/GE-Proton11-1"; ge.mkdir(parents=True)
        vp = root / "steamapps/common/Proton 11.0"; vp.mkdir(parents=True)
        ge.joinpath("proton").write_text(
            'PROTON_ENABLE_WAYLAND DXVK_HDR DXVK_ENABLE_NVAPI PROTON_USE_D7VK')
        vp.joinpath("proton").write_text('DXVK_ENABLE_NVAPI')

        # _compat_dirs deliberately includes absolute system paths
        # (~/.steam/root/..., /usr/share/steam/...) because real installs keep
        # compat tools there. That makes this test read the developer's own
        # machine unless it's pinned: it passed in a container with no Steam and
        # failed on a real one, where actual builds leaked into the union.
        real = pcc._compat_dirs
        pcc._compat_dirs = lambda r: [Path(r) / "compatibilitytools.d"]
        try:
            cap = pcc.proton_capabilities(root)
        finally:
            pcc._compat_dirs = real
        # Keyed on the name Steam writes to CompatToolMapping, not the folder:
        # official builds get a slug ("Proton 11.0" -> proton_11), custom ones
        # use their own name. Verified against a real config.vdf.
        self.assertIn("GE-Proton11-1", cap["tools"])
        self.assertIn("proton_11", cap["tools"])
        self.assertNotIn("Proton 11.0", cap["tools"])
        self.assertIn("PROTON_ENABLE_WAYLAND", cap["known"])

        def supported(tool, var):
            if var not in cap["known"]:
                return True                      # invisible to the scan
            return var in cap["tools"].get(tool, [])

        # proven absent on Valve -> safe to grey out
        self.assertFalse(supported("proton_11", "PROTON_ENABLE_WAYLAND"))
        self.assertFalse(supported("proton_11", "DXVK_HDR"))
        # present on GE
        self.assertTrue(supported("GE-Proton11-1", "PROTON_ENABLE_WAYLAND"))
        # in both
        self.assertTrue(supported("proton_11", "DXVK_ENABLE_NVAPI"))
        # never seen by the scan but genuinely works -> must stay enabled
        self.assertTrue(supported("proton_11", "DXVK_NVAPI_VKREFLEX"))

    def test_official_slug_matches_steam(self):
        """Steam names official builds with a slug but custom ones with their
        directory name - two schemes, which is why fuzzy label matching never
        worked. Verified against a real config.vdf CompatToolMapping."""
        self.assertEqual(pcc._official_slug("Proton 11.0"), "proton_11")
        self.assertEqual(pcc._official_slug("Proton 9.0"), "proton_9")
        self.assertEqual(pcc._official_slug("Proton - Experimental"),
                         "proton_experimental")
        self.assertEqual(pcc._official_slug("Proton Hotfix"), "proton_hotfix")
        # unknown shapes must return None rather than a guess: a wrong slug
        # would write a name Steam doesn't know and break the game's setting
        self.assertIsNone(pcc._official_slug("GE-Proton11-1"))
        self.assertIsNone(pcc._official_slug("Proton EasyAntiCheat Runtime"))

    def test_compat_tools_only_lists_installed(self):
        """The hardcoded list offered proton_9/proton_10 whether installed or
        not, and stopped at 10 - so a real Proton 11.0 couldn't be selected
        while two absent builds could. Selecting an absent build also gave the
        capability scan nothing to read, which looked like the validation was
        broken."""
        root = Path(self.tmp.name) / "sr2"
        common = root / "steamapps/common"
        (common / "Proton 11.0").mkdir(parents=True)
        (common / "Proton 11.0" / "proton").write_text("x")
        (common / "Proton - Experimental").mkdir(parents=True)
        (common / "Proton - Experimental" / "proton").write_text("x")
        names = [t["name"] for t in pcc.list_compat_tools(root)]
        self.assertIn("proton_11", names)
        self.assertIn("proton_experimental", names)
        self.assertNotIn("proton_9", names)     # not installed -> not offered
        self.assertNotIn("proton_10", names)

    def test_shader_settings_excludes_per_game_sizes(self):
        """ShaderCacheManager/App/<id>/ShaderCacheSize is a byte count Steam
        records per game (8198563848 on a real config), not a setting. Matching
        on the key name alone rendered 17 of them as checkboxes; flipping one
        would write "1" into a size field and corrupt Steam's config. Only
        genuine 0/1 settings outside the App subtree may be shown or written."""
        root = Path(self.tmp.name) / "ss"
        (root / "config").mkdir(parents=True)
        apps = "".join('"%s" { "ShaderCacheSize" "%d" }\n' % (a, s)
                       for a, s in [("553850", 8198563848),
                                    ("1971870", 3446367914),
                                    ("228980", 0)])
        (root / "config/config.vdf").write_text(
            '"InstallConfigStore"{"Software"{"Valve"{"Steam"'
            '{"ShaderCacheManager"{"EnableShaderBackgroundProcessing" "1"\n'
            '"App"{' + apps + '}}}}}}')

        d = pcc.steam_shader_settings(root)
        keys = [k for f in d["files"] for k in f["keys"]]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["key"], "EnableShaderBackgroundProcessing")

        # the sizes are still readable as a total, just never as switches
        sizes = pcc.steam_shader_cache_sizes(root)
        self.assertEqual(sizes["games"], 2)
        self.assertEqual(sizes["total_bytes"], 8198563848 + 3446367914)

        # and writing into that subtree must be refused outright
        with self.assertRaises(RuntimeError):
            pcc.set_steam_shader_setting(
                root, str(root / "config/config.vdf"),
                "InstallConfigStore/Software/Valve/Steam/ShaderCacheManager/"
                "App/553850/ShaderCacheSize", "1")
        with self.assertRaises(RuntimeError):
            pcc.set_steam_shader_setting(
                root, str(root / "config/config.vdf"),
                "InstallConfigStore/Software/Valve/Steam/ShaderCacheManager/"
                "EnableShaderBackgroundProcessing", "8198563848")

    def test_shader_cache_size_is_configurable(self):
        """The ceiling was hardcoded at 10 GiB. It's a limit, not an
        allocation, so bigger costs nothing until shaders accumulate - but only
        the offered sizes may be written, since this goes into /etc/environment
        as root."""
        self.assertEqual([gb for gb, _ in pcc.SHADER_CACHE_SIZES],
                         [10, 30, 50, 100])
        for _, b in pcc.SHADER_CACHE_SIZES:
            self.assertEqual(b % (1024 ** 3), 0)
        # anything not on the list is refused rather than written blindly
        for bad in (12345, 0, -1, 999 * 1024 ** 3):
            with self.assertRaises(RuntimeError):
                pcc.set_environment_shaders(True, bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
