#!/usr/bin/env python3
"""Proton Command Center test suite. Stdlib only, no Steam required:
builds a mock Steam install in a temp dir. Run:  python3 tests/test_pcc.py"""

import json
import os
import struct
import subprocess
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
    return root


def _build_fake_pe64(regular_dlls=(), delay_dlls=()):
    """Minimal but structurally real PE32+ exe: DOS stub, PE/COFF/optional
    headers, one section whose VirtualAddress == PointerToRawData (so
    RVA-to-file-offset is the identity function within it), containing a
    regular import descriptor table and/or a delay-load import descriptor
    table, each pointing at real null-terminated DLL name strings. Enough
    for pe_imports()/detect_graphics_api() to parse exactly like a real exe."""
    HEADER_SIZE = 64 + 4 + 20 + 240 + 40  # dos stub + PE sig + COFF + opt hdr + 1 section hdr
    section_start = HEADER_SIZE
    body = bytearray()

    def add_cstr(s):
        off = section_start + len(body)
        body.extend(s.encode("ascii") + b"\x00")
        return off

    def add_descriptors(dlls, entry_size, name_field_off):
        if not dlls:
            return 0
        name_offs = [add_cstr(d) for d in dlls]
        arr_off = section_start + len(body)
        for noff in name_offs:
            entry = bytearray(entry_size)
            struct.pack_into("<I", entry, name_field_off, noff)
            body.extend(entry)
        body.extend(bytearray(entry_size))  # null terminator entry
        return arr_off

    import_rva = add_descriptors(regular_dlls, 20, 12)
    delay_rva = add_descriptors(delay_dlls, 32, 4)

    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<i", dos, 0x3C, 64)

    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 240, 0x0022)

    opt = bytearray(240)
    struct.pack_into("<H", opt, 0, 0x20B)   # PE32+ magic
    dir_array_off = 112
    struct.pack_into("<I", opt, dir_array_off + 1 * 8, import_rva)
    struct.pack_into("<I", opt, dir_array_off + 13 * 8, delay_rva)

    section = bytearray(40)
    section[0:8] = b".rdata\x00\x00"
    struct.pack_into("<II", section, 8, len(body), section_start)   # VirtualSize, VirtualAddress
    struct.pack_into("<I", section, 20, section_start)               # PointerToRawData

    return bytes(dos) + b"PE\x00\x00" + coff + bytes(opt) + bytes(section) + bytes(body)


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
        pcc.RHI_DATA_DIR = pcc.DATA_DIR / "rhi"
        pcc.RESHADE_STAGING_DIR = pcc.RHI_DATA_DIR / "reshade"
        pcc.RESHADE_NORMAL_STAGING_DIR = pcc.RHI_DATA_DIR / "reshade-normal"
        pcc.RESHADE_NIGHTLY_STAGING_DIR = pcc.RHI_DATA_DIR / "reshade-nightly"
        pcc.RESHADE_LEGACY_STAGING_DIR = pcc.RHI_DATA_DIR / "reshade-legacy"
        pcc.RESHADE_CUSTOM_DIR = pcc.RHI_DATA_DIR / "reshade-custom"
        pcc.RESHADE_CUSTOM_ADDONS_DIR = pcc.RHI_DATA_DIR / "addons-custom"
        pcc.RESHADE_SHADERS_STAGE_DIR = pcc.RHI_DATA_DIR / "shaders" / "Shaders"
        pcc.RESHADE_TEXTURES_STAGE_DIR = pcc.RHI_DATA_DIR / "shaders" / "Textures"
        pcc.RESHADE_ADDONS_CACHE_FILE = pcc.RHI_DATA_DIR / "addons_cache.ini"
        pcc.OPTISCALER_DATA_DIR = pcc.RHI_DATA_DIR / "optiscaler"
        pcc.OPTISCALER_STAGING_DIR = pcc.OPTISCALER_DATA_DIR / "stable"
        pcc.OPTISCALER_NIGHTLY_DIR = pcc.OPTISCALER_DATA_DIR / "nightly"
        pcc.OPTISCALER_INIS_DIR = pcc.OPTISCALER_DATA_DIR / "inis"
        pcc.OPTIPATCHER_STAGING_DIR = pcc.OPTISCALER_DATA_DIR / "optipatcher"
        pcc.OPTISCALER_DLSS_DIR = pcc.OPTISCALER_DATA_DIR / "dlss"
        pcc.OPTISCALER_CUSTOM_DIR = pcc.RHI_DATA_DIR / "optiscaler-custom"
        pcc.DXVK_DATA_DIR = pcc.RHI_DATA_DIR / "dxvk"
        pcc.DXVK_DEV_DIR = pcc.DXVK_DATA_DIR / "development"
        pcc.DXVK_STABLE_DIR = pcc.DXVK_DATA_DIR / "stable"
        pcc.DXVK_LILIUM_DIR = pcc.DXVK_DATA_DIR / "lilium"
        pcc.DLSSNR_CACHE_DIR = pcc.RHI_DATA_DIR / "dlssnr"
        pcc.STREAMLINE_DATA_DIR = pcc.RHI_DATA_DIR / "streamline"
        for d in (pcc.DLL_LIBRARY, pcc.BACKUP_DIR, pcc.ART_DIR):
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

    def test_add_shortcut_computes_real_install_size(self):
        game_dir = Path(self.tmp.name) / "MyGame"
        game_dir.mkdir()
        (game_dir / "MyGame.exe").write_bytes(b"x" * 1000)
        (game_dir / "data.pak").write_bytes(b"y" * 2000)
        pcc.add_shortcut(self.root, "My Game", str(game_dir / "MyGame.exe"))
        shortcuts = pcc.list_shortcuts(self.root)
        self.assertEqual(shortcuts[0]["size_bytes"], 3000)

    def test_shortcut_size_bytes_is_cached(self):
        game_dir = Path(self.tmp.name) / "MyGame2"
        game_dir.mkdir()
        (game_dir / "a.bin").write_bytes(b"x" * 500)
        size1 = pcc._shortcut_size_bytes("99999", str(game_dir))
        self.assertEqual(size1, 500)
        (game_dir / "b.bin").write_bytes(b"y" * 500)   # grows after first check
        size2 = pcc._shortcut_size_bytes("99999", str(game_dir))
        self.assertEqual(size2, 500)   # still cached, doesn't re-walk yet
        # force cache expiry
        state = pcc.load_state()
        state["shortcut_size_cache"]["99999"]["ts"] = 0
        pcc.save_state(state)
        size3 = pcc._shortcut_size_bytes("99999", str(game_dir))
        self.assertEqual(size3, 1000)

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

    def test_find_game_exe_skips_installers_picks_largest(self):
        d = self.root / "steamapps/common/TestGame"
        (d / "UnInstall.exe").write_bytes(b"x" * 500)
        (d / "EasyAntiCheat_Setup.exe").write_bytes(b"x" * 5000)
        (d / "Binaries/Win64").mkdir(parents=True, exist_ok=True)
        real = d / "Binaries/Win64/Game-Win64-Shipping.exe"
        real.write_bytes(b"x" * 2000)
        found = pcc._find_game_exe(d)
        self.assertEqual(found, real)

    # ---- RHI: graphics API detection ----
    def test_pe_imports_parses_regular_and_delay_load(self):
        exe = Path(self.tmp.name) / "game.exe"
        exe.write_bytes(_build_fake_pe64(
            regular_dlls=["d3d11.dll", "kernel32.dll"],
            delay_dlls=["d3d12.dll"]))
        bitness, regular, delay = pcc.pe_imports(exe)
        self.assertEqual(bitness, 64)
        self.assertEqual(regular, {"d3d11.dll", "kernel32.dll"})
        self.assertEqual(delay, {"d3d12.dll"})

    def test_pe_imports_rejects_non_pe(self):
        exe = Path(self.tmp.name) / "notreally.exe"
        exe.write_bytes(b"not a pe file")
        bitness, regular, delay = pcc.pe_imports(exe)
        self.assertIsNone(bitness)
        self.assertEqual(regular, set())
        self.assertEqual(delay, set())

    def test_detect_graphics_api_priority_and_dxgi_inference(self):
        self.assertEqual(pcc.detect_graphics_api({"d3d11.dll", "d3d9.dll"}), "d3d11")
        self.assertEqual(pcc.detect_graphics_api({"opengl32.dll"}), "opengl")
        self.assertEqual(pcc.detect_graphics_api({"dxgi.dll"}), "d3d12")
        self.assertEqual(pcc.detect_graphics_api({"dxgi.dll", "d3d11.dll"}), "d3d11")
        self.assertIsNone(pcc.detect_graphics_api(set()))

    def test_detect_graphics_api_delay_load_fallback(self):
        # regular import has nothing >= DX11 -> delay-loaded d3d12 promotes
        self.assertEqual(
            pcc.detect_graphics_api({"opengl32.dll"}, {"d3d12.dll"}), "d3d12")
        # regular import already has d3d11 (>= DX11 priority) -> delay-load
        # d3d12 must NOT override it (UE4/5 often delay-loads d3d12 as an
        # optional path while d3d11 is the real default API)
        self.assertEqual(
            pcc.detect_graphics_api({"d3d11.dll"}, {"d3d12.dll"}), "d3d11")

    def test_detect_graphics_api_dxgi_short_circuits_before_delay_load(self):
        # Regular-imports dxgi.dll alone (no >=DX11 explicit match) -> DX12
        # is inferred immediately, WITHOUT even consulting delay-loads -
        # matches RHI's GraphicsApiDetector.Detect() exactly (the dxgi-only
        # inference happens right after the regular scan, before delay-load
        # scanning). A game that regular-imports only dxgi.dll but
        # delay-loads d3d11.dll is unambiguously DX12, not DX11.
        self.assertEqual(
            pcc.detect_graphics_api({"dxgi.dll"}, {"d3d11.dll"}), "d3d12")
        # But if dxgi.dll ISN'T regular-imported, delay-load scanning still
        # applies normally.
        self.assertEqual(
            pcc.detect_graphics_api({"opengl32.dll"}, {"d3d11.dll"}), "d3d11")

    def test_detect_unity_api_boot_config_and_fallback(self):
        d = Path(self.tmp.name) / "unity_game"
        d.mkdir()
        exe = d / "Game.exe"
        exe.write_bytes(b"MZ")
        data_dir = d / "Game_Data"
        data_dir.mkdir()
        # no boot.config at all -> Unity default DX11
        self.assertEqual(pcc._detect_unity_api(exe), "d3d11")
        (data_dir / "boot.config").write_text("gfx-device-type=21\n")
        self.assertEqual(pcc._detect_unity_api(exe), "vulkan")
        # not a Unity game at all (no _Data dir) -> None
        exe2 = Path(self.tmp.name) / "NotUnity.exe"
        exe2.write_bytes(b"MZ")
        self.assertIsNone(pcc._detect_unity_api(exe2))

    def test_detect_game_graphics_api_caches_by_mtime(self):
        exe = Path(self.tmp.name) / "game2.exe"
        exe.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        r1 = pcc.detect_game_graphics_api(exe)
        self.assertEqual(r1["api"], "d3d11")
        self.assertEqual(r1["bitness"], 64)
        state = pcc.load_state()
        self.assertIn(str(exe), state["rhi_api_cache"])
        # mutate on disk without touching mtime - cached result must stick
        real_stat = exe.stat()
        exe.write_bytes(_build_fake_pe64(regular_dlls=["opengl32.dll"]))
        os.utime(exe, (real_stat.st_atime, real_stat.st_mtime))
        r2 = pcc.detect_game_graphics_api(exe)
        self.assertEqual(r2["api"], "d3d11")

    def test_find_game_exe_prefers_exe_with_graphics_api_import(self):
        """Regression: live-testing surfaced a real case (The Witcher 3)
        where a 642MB setup_redlauncher.exe at the game root dwarfs the
        real 86MB bin/x64/witcher3.exe, so the old plain largest-file
        heuristic always picked the launcher stub. A launcher/installer has
        no reason to import a graphics API DLL; the real game exe always
        does - a checkable PE-import signal, not another size/name guess."""
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        huge_launcher = d / "setup_launcher.exe"
        huge_launcher.write_bytes(b"x" * 5_000_000)  # not a valid PE - no imports at all
        (d / "bin/x64").mkdir(parents=True)
        real_exe = d / "bin/x64/Game.exe"
        real_exe.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        self.assertEqual(pcc._find_game_exe(d), real_exe)

    def test_find_game_exe_falls_back_to_largest_when_none_import_graphics_api(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        small = d / "small.exe"
        small.write_bytes(b"x" * 1_000)
        large = d / "large.exe"
        large.write_bytes(b"y" * 5_000)
        self.assertEqual(pcc._find_game_exe(d), large)

    # ---- RHI: ReShade install ----
    def _fake_game_exe(self, dlls=("d3d11.dll",)):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        exe = d / "Game.exe"
        exe.write_bytes(_build_fake_pe64(regular_dlls=list(dlls)))
        return d, exe

    def test_install_reshade_full_flow_and_remove(self):
        d, exe = self._fake_game_exe()
        engine_dir = pcc.RESHADE_STAGING_DIR / "9.9.9"
        engine_dir.mkdir(parents=True)
        (engine_dir / "ReShade64.dll").write_bytes(b"R" * 1_100_000)
        (engine_dir / "ReShade32.dll").write_bytes(b"r" * 1_100_000)
        real_latest, real_engine = pcc.reshade_latest, pcc.ensure_reshade_engine
        pcc.reshade_latest = lambda: {"version": "9.9.9", "url": "http://x"}
        pcc.ensure_reshade_engine = lambda version, url=None, task_id=None: engine_dir
        try:
            r = pcc.install_reshade("12345", str(d), exe_override=str(exe))
        finally:
            pcc.reshade_latest = real_latest
            pcc.ensure_reshade_engine = real_engine

        self.assertTrue(r["installed"])
        self.assertEqual(r["api"], "d3d11")
        self.assertEqual(r["bitness"], 64)
        target = d / "dxgi.dll"
        self.assertEqual(target.read_bytes(), b"R" * 1_100_000)

        status = pcc.scan_game_reshade("12345", str(d), exe_path=str(exe))
        self.assertTrue(status["installed"])
        self.assertEqual(status["channel"], "stable")

        rm = pcc.remove_reshade("12345")
        self.assertTrue(rm["removed"])
        self.assertFalse(target.exists())
        with self.assertRaises(RuntimeError):
            pcc.remove_reshade("12345")

    def test_scan_game_reshade_remembers_override_exe(self):
        """Regression: after installing against a manually-overridden exe
        (because a manual exe-override was needed in the first place - e.g.
        two candidates both import a graphics API, a genuinely ambiguous
        case the import-based heuristic still can't resolve on its own),
        status must keep reporting the exe that was actually used, not
        re-run the auto-detect guess."""
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        # Both are valid PE64s that import a graphics DLL, so the
        # graphics-API-import heuristic can't distinguish them by that
        # signal alone - it falls back to size, picking the larger decoy.
        decoy = d / "launcher.exe"
        decoy.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]) + b"\x00" * 10_000)
        (d / "bin/x64").mkdir(parents=True)
        real_exe = d / "bin/x64/Game.exe"
        real_exe.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        # sanity: the auto-detect heuristic really would pick the (larger) decoy
        self.assertEqual(pcc._find_game_exe(d), decoy)

        engine_dir = pcc.RESHADE_STAGING_DIR / "9.9.9"
        engine_dir.mkdir(parents=True)
        (engine_dir / "ReShade64.dll").write_bytes(b"R" * 1_100_000)
        (engine_dir / "ReShade32.dll").write_bytes(b"r" * 1_100_000)
        real_latest, real_engine = pcc.reshade_latest, pcc.ensure_reshade_engine
        pcc.reshade_latest = lambda: {"version": "9.9.9", "url": "http://x"}
        pcc.ensure_reshade_engine = lambda version, url=None, task_id=None: engine_dir
        try:
            pcc.install_reshade("12345", str(d), exe_override=str(real_exe))
        finally:
            pcc.reshade_latest = real_latest
            pcc.ensure_reshade_engine = real_engine

        status = pcc.scan_game_reshade("12345", str(d))   # no exe_path override this time
        self.assertEqual(status["exe"], str(real_exe))
        self.assertEqual(status["detected_api"], "d3d11")
        self.assertEqual(status["path"], str(d / "bin/x64/dxgi.dll"))

    def test_install_reshade_backs_up_foreign_dxgi_not_deletes(self):
        d, exe = self._fake_game_exe()
        (d / "dxgi.dll").write_bytes(b"totally unrelated vendor DLL content")
        engine_dir = pcc.RESHADE_STAGING_DIR / "9.9.9"
        engine_dir.mkdir(parents=True)
        (engine_dir / "ReShade64.dll").write_bytes(b"R" * 1_100_000)
        (engine_dir / "ReShade32.dll").write_bytes(b"r" * 1_100_000)
        real_latest, real_engine = pcc.reshade_latest, pcc.ensure_reshade_engine
        pcc.reshade_latest = lambda: {"version": "9.9.9", "url": "http://x"}
        pcc.ensure_reshade_engine = lambda version, url=None, task_id=None: engine_dir
        try:
            pcc.install_reshade("12345", str(d), exe_override=str(exe))
        finally:
            pcc.reshade_latest = real_latest
            pcc.ensure_reshade_engine = real_engine

        backup = d / "dxgi.dll.original"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), b"totally unrelated vendor DLL content")
        self.assertEqual((d / "dxgi.dll").read_bytes(), b"R" * 1_100_000)

        pcc.remove_reshade("12345")
        self.assertFalse(backup.exists())
        self.assertEqual((d / "dxgi.dll").read_bytes(),
                         b"totally unrelated vendor DLL content")

    def test_backup_foreign_dll_refreshes_stale_backup_instead_of_discarding(self):
        # If a foreign DLL was already backed up once and then updated (e.g.
        # a game patch replaced it) before install/backup runs again, the
        # backup must be refreshed with the CURRENT foreign content, not
        # silently discarded - the old behavior unlink()'d the new foreign
        # file and left the stale backup in place.
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        target = d / "dxgi.dll"
        (target.with_name("dxgi.dll.original")).write_bytes(b"stale old backup")
        target.write_bytes(b"updated foreign content")
        pcc._backup_foreign_dll(target)
        self.assertFalse(target.exists())
        self.assertEqual(target.with_name("dxgi.dll.original").read_bytes(),
                         b"updated foreign content")

    def test_backup_foreign_dll_does_not_back_up_dxvk_managed_name(self):
        # A DXVK file sitting at a DXVK-managed filename (e.g. d3d9.dll for
        # a DX9 game where ReShade also wants to install as d3d9.dll) isn't
        # "foreign" - the two coexist there rather than one backing up the
        # other.
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        target = d / "d3d9.dll"
        target.write_bytes(b"totally not dxvk or reshade, just filler " + b"DXVK_" + b"x" * 100)
        pcc._backup_foreign_dll(target)
        self.assertTrue(target.is_file())
        self.assertFalse(target.with_name("d3d9.dll.original").exists())

    def test_install_reshade_refuses_vulkan(self):
        d, exe = self._fake_game_exe(dlls=["vulkan-1.dll"])
        with self.assertRaises(RuntimeError):
            pcc.install_reshade("12345", str(d), exe_override=str(exe))

    def test_resolve_auto_reshade_filename(self):
        self.assertEqual(pcc.resolve_auto_reshade_filename({"d3d11"}), "dxgi.dll")
        self.assertEqual(pcc.resolve_auto_reshade_filename({"d3d12"}), "dxgi.dll")
        # DX11/12 take precedence even if the game also legacy-imports d3d9
        self.assertEqual(pcc.resolve_auto_reshade_filename({"d3d11", "d3d9"}), "dxgi.dll")
        self.assertEqual(pcc.resolve_auto_reshade_filename({"d3d9"}), "d3d9.dll")
        self.assertEqual(pcc.resolve_auto_reshade_filename({"d3d8"}), "d3d8.dll")
        # DX9 beats OpenGL even without any DX11/12 present
        self.assertEqual(pcc.resolve_auto_reshade_filename({"d3d9", "opengl"}), "d3d9.dll")
        # OpenGL only applies when it's the ONLY api detected
        self.assertEqual(pcc.resolve_auto_reshade_filename({"opengl"}), "opengl32.dll")
        self.assertEqual(pcc.resolve_auto_reshade_filename(set()), "dxgi.dll")

    def test_detect_all_graphics_apis(self):
        self.assertEqual(pcc._detect_all_graphics_apis({"d3d9.dll"}), {"d3d9"})
        # dxgi-only regular import with no explicit DX -> infers d3d12 too
        self.assertEqual(pcc._detect_all_graphics_apis({"dxgi.dll"}), {"d3d12"})
        # explicit regular match suppresses the dxgi-only inference
        self.assertEqual(pcc._detect_all_graphics_apis({"dxgi.dll", "d3d11.dll"}), {"d3d11"})
        # delay-loads count unconditionally here (unlike detect_graphics_api)
        self.assertEqual(
            pcc._detect_all_graphics_apis({"d3d9.dll"}, {"d3d11.dll"}), {"d3d9", "d3d11"})

    def test_install_reshade_dx9_installs_as_d3d9_dll(self):
        d, exe = self._fake_game_exe(dlls=["d3d9.dll"])
        engine_dir = pcc.RESHADE_STAGING_DIR / "9.9.9"
        engine_dir.mkdir(parents=True)
        (engine_dir / "ReShade64.dll").write_bytes(b"R" * 1_100_000)
        (engine_dir / "ReShade32.dll").write_bytes(b"r" * 1_100_000)
        real_latest, real_engine = pcc.reshade_latest, pcc.ensure_reshade_engine
        pcc.reshade_latest = lambda: {"version": "9.9.9", "url": "http://x"}
        pcc.ensure_reshade_engine = lambda version, url=None, task_id=None: engine_dir
        try:
            r = pcc.install_reshade("12345", str(d), exe_override=str(exe))
        finally:
            pcc.reshade_latest = real_latest
            pcc.ensure_reshade_engine = real_engine
        self.assertEqual(r["path"], str(d / "d3d9.dll"))
        self.assertTrue((d / "d3d9.dll").is_file())
        self.assertFalse((d / "dxgi.dll").exists())

    def test_install_reshade_opengl_only_installs_as_opengl32_dll(self):
        d, exe = self._fake_game_exe(dlls=["opengl32.dll"])
        engine_dir = pcc.RESHADE_STAGING_DIR / "9.9.9"
        engine_dir.mkdir(parents=True)
        (engine_dir / "ReShade64.dll").write_bytes(b"R" * 1_100_000)
        (engine_dir / "ReShade32.dll").write_bytes(b"r" * 1_100_000)
        real_latest, real_engine = pcc.reshade_latest, pcc.ensure_reshade_engine
        pcc.reshade_latest = lambda: {"version": "9.9.9", "url": "http://x"}
        pcc.ensure_reshade_engine = lambda version, url=None, task_id=None: engine_dir
        try:
            r = pcc.install_reshade("12345", str(d), exe_override=str(exe))
        finally:
            pcc.reshade_latest = real_latest
            pcc.ensure_reshade_engine = real_engine
        self.assertEqual(r["path"], str(d / "opengl32.dll"))

    # ---- RHI: ReShade channels (No Addons / Legacy / Nightly / Custom) ----
    def test_reshade_no_addons_regex_excludes_addon_build(self):
        html = ('<a href="downloads/ReShade_Setup_6.8.0_Addon.exe">Addon</a>'
                '<a href="downloads/ReShade_Setup_6.8.0.exe">Standard</a>')
        m = pcc.re.search(r"downloads/ReShade_Setup_([\d.]+)\.exe", html)
        self.assertEqual(m.group(1), "6.8.0")
        self.assertNotIn("_Addon", m.group(0))

    def test_install_reshade_no_addons_channel(self):
        d, exe = self._fake_game_exe()
        engine_dir = pcc.RESHADE_NORMAL_STAGING_DIR / "6.8.0"
        engine_dir.mkdir(parents=True)
        (engine_dir / "ReShade64.dll").write_bytes(b"N" * 1_100_000)
        (engine_dir / "ReShade32.dll").write_bytes(b"n" * 1_100_000)
        real_latest, real_engine = pcc.reshade_no_addons_latest, pcc.ensure_reshade_no_addons_engine
        pcc.reshade_no_addons_latest = lambda: {"version": "6.8.0", "url": "http://x"}
        pcc.ensure_reshade_no_addons_engine = lambda version, url=None, task_id=None: engine_dir
        try:
            r = pcc.install_reshade("12345", str(d), exe_override=str(exe),
                                    channel="no_addons")
        finally:
            pcc.reshade_no_addons_latest = real_latest
            pcc.ensure_reshade_no_addons_engine = real_engine
        self.assertTrue(r["installed"])
        self.assertEqual((d / "dxgi.dll").read_bytes(), b"N" * 1_100_000)
        rec = pcc.load_state()["rhi_reshade_installs"]["12345"]
        self.assertEqual(rec["channel"], "no_addons")

    def test_install_reshade_legacy_channel_pinned_version(self):
        d, exe = self._fake_game_exe()
        engine_dir = pcc.RESHADE_LEGACY_STAGING_DIR / "5.9.2"
        engine_dir.mkdir(parents=True)
        (engine_dir / "ReShade64.dll").write_bytes(b"L" * 1_100_000)
        (engine_dir / "ReShade32.dll").write_bytes(b"l" * 1_100_000)
        real = pcc.ensure_reshade_legacy_engine
        pcc.ensure_reshade_legacy_engine = lambda version, task_id=None: engine_dir
        try:
            r = pcc.install_reshade("12345", str(d), exe_override=str(exe),
                                    channel="legacy", legacy_version="5.9.2")
        finally:
            pcc.ensure_reshade_legacy_engine = real
        self.assertEqual(r["version"], "5.9.2")
        self.assertEqual((d / "dxgi.dll").read_bytes(), b"L" * 1_100_000)

    def test_install_reshade_legacy_requires_version(self):
        d, exe = self._fake_game_exe()
        with self.assertRaises(RuntimeError):
            pcc.install_reshade("12345", str(d), exe_override=str(exe), channel="legacy")

    def test_ensure_reshade_nightly_engine_downloads_and_tracks_size(self):
        import zipfile, io as _io
        def fake_zip(dll_name, content):
            buf = _io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(dll_name, content)
            return buf.getvalue()
        payloads = {64: fake_zip("ReShade64.dll", b"N64" * 500_000),
                   32: fake_zip("ReShade32.dll", b"n32" * 500_000)}
        real_bytes = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: (
            payloads[64] if "64" in url else payloads[32])
        try:
            r1 = pcc.ensure_reshade_nightly_engine()
            self.assertTrue(r1["changed"])
            self.assertTrue((pcc.RESHADE_NIGHTLY_STAGING_DIR / "ReShade64.dll").is_file())
            self.assertTrue((pcc.RESHADE_NIGHTLY_STAGING_DIR / "ReShade32.dll").is_file())
            # same content again -> no change
            r2 = pcc.ensure_reshade_nightly_engine()
            self.assertFalse(r2["changed"])
        finally:
            pcc._gh_bytes = real_bytes

    def test_install_reshade_custom_channel_picks_dropped_file(self):
        d, exe = self._fake_game_exe()
        pcc.RESHADE_CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
        self.assertEqual(pcc.list_custom_reshade_files(), [])
        (pcc.RESHADE_CUSTOM_DIR / "MyCustomReShade64.dll").write_bytes(b"C" * 2000)
        self.assertEqual(pcc.list_custom_reshade_files(), ["MyCustomReShade64.dll"])
        r = pcc.install_reshade("12345", str(d), exe_override=str(exe),
                                channel="custom", custom_filename="MyCustomReShade64.dll")
        self.assertEqual(r["version"], "MyCustomReShade64.dll")
        self.assertEqual((d / "dxgi.dll").read_bytes(), b"C" * 2000)

    def test_check_custom_reshade_updates_redeploys_on_hash_change(self):
        d, exe = self._fake_game_exe()
        pcc.RESHADE_CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
        (pcc.RESHADE_CUSTOM_DIR / "Custom.dll").write_bytes(b"v1 content")
        pcc.install_reshade("12345", str(d), exe_override=str(exe),
                            channel="custom", custom_filename="Custom.dll")
        target = Path(pcc.load_state()["rhi_reshade_installs"]["12345"]["path"])
        self.assertEqual(target.read_bytes(), b"v1 content")

        # first check just establishes the baseline hash - nothing changed yet
        r0 = pcc.check_custom_reshade_updates()
        self.assertEqual(r0["changed"], [])
        self.assertEqual(r0["redeployed"], 0)

        # user drops a new build over the same filename
        (pcc.RESHADE_CUSTOM_DIR / "Custom.dll").write_bytes(b"v2 content, updated")
        r1 = pcc.check_custom_reshade_updates()
        self.assertEqual(r1["changed"], ["Custom.dll"])
        self.assertEqual(r1["redeployed"], 1)
        self.assertEqual(target.read_bytes(), b"v2 content, updated")

        # unchanged on the next check
        r2 = pcc.check_custom_reshade_updates()
        self.assertEqual(r2["changed"], [])
        self.assertEqual(r2["redeployed"], 0)

    def test_check_custom_reshade_updates_ignores_non_custom_channel_games(self):
        d, exe = self._fake_game_exe()
        pcc.RESHADE_CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
        engine_dir = pcc.RESHADE_STAGING_DIR / "9.9.9"
        engine_dir.mkdir(parents=True)
        (engine_dir / "ReShade64.dll").write_bytes(b"R" * 1_100_000)
        (engine_dir / "ReShade32.dll").write_bytes(b"r" * 1_100_000)
        real_latest, real_engine = pcc.reshade_latest, pcc.ensure_reshade_engine
        pcc.reshade_latest = lambda: {"version": "9.9.9", "url": "http://x"}
        pcc.ensure_reshade_engine = lambda version, url=None, task_id=None: engine_dir
        try:
            pcc.install_reshade("12345", str(d), exe_override=str(exe))   # stable channel
        finally:
            pcc.reshade_latest = real_latest
            pcc.ensure_reshade_engine = real_engine
        # an unrelated file in the Custom folder changing must not touch
        # this stable-channel install at all
        (pcc.RESHADE_CUSTOM_DIR / "Unrelated.dll").write_bytes(b"v1")
        pcc.check_custom_reshade_updates()
        (pcc.RESHADE_CUSTOM_DIR / "Unrelated.dll").write_bytes(b"v2")
        r = pcc.check_custom_reshade_updates()
        self.assertEqual(r["changed"], ["Unrelated.dll"])
        self.assertEqual(r["redeployed"], 0)
        self.assertEqual((d / "dxgi.dll").read_bytes(), b"R" * 1_100_000)

    def test_install_reshade_custom_channel_no_files_raises(self):
        d, exe = self._fake_game_exe()
        with self.assertRaises(RuntimeError):
            pcc.install_reshade("12345", str(d), exe_override=str(exe), channel="custom")

    def test_get_staged_reshade_path_unknown_channel_raises(self):
        with self.assertRaises(RuntimeError):
            pcc.get_staged_reshade_path("bogus", 64)

    def test_identify_dxgi_file_by_string_scan(self):
        d, _ = self._fake_game_exe()
        p = d / "dxgi.dll"
        p.write_bytes(b"junk before " + b"ReShade" + b" junk " + b"reshade.me" + b" junk")
        self.assertEqual(pcc._identify_dxgi_file(p), "reshade")
        p.write_bytes(b"not a reshade dll at all, just some other vendor's file")
        self.assertEqual(pcc._identify_dxgi_file(p), "unknown")
        p.write_bytes(b"x" * 20_000_000)  # too big to ever be ReShade
        self.assertEqual(pcc._identify_dxgi_file(p), "unknown")

    # ---- RHI: RE Framework ----
    def test_is_re_engine_game_detects_signature_file(self):
        d = self.root / "steamapps/common/REGame"
        d.mkdir(parents=True)
        self.assertFalse(pcc.is_re_engine_game(d))
        (d / "re_chunk_000.pak").write_bytes(b"x")
        self.assertTrue(pcc.is_re_engine_game(d))

    def test_install_re_framework_full_flow_and_remove(self):
        import zipfile, io as _io
        d = self.root / "steamapps/common/REGame"
        d.mkdir(parents=True)
        (d / "re_chunk_000.pak").write_bytes(b"x")
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dinput8.dll", b"fake refw dll")
        zip_bytes = buf.getvalue()

        real_latest, real_bytes = pcc.re_framework_latest, pcc._gh_bytes
        pcc.re_framework_latest = lambda: {"version": "nightly-99999-abc", "url": "http://x"}
        pcc._gh_bytes = lambda url, task=None: zip_bytes
        try:
            r = pcc.install_re_framework("12345", str(d))
        finally:
            pcc.re_framework_latest = real_latest
            pcc._gh_bytes = real_bytes

        self.assertTrue(r["installed"])
        target = d / "dinput8.dll"
        self.assertEqual(target.read_bytes(), b"fake refw dll")

        status = pcc.scan_re_framework("12345", str(d))
        self.assertTrue(status["is_re_engine"])
        self.assertTrue(status["installed"])
        self.assertFalse(status["update_available"])

        rm = pcc.remove_re_framework("12345")
        self.assertTrue(rm["removed"])
        self.assertFalse(target.exists())
        with self.assertRaises(RuntimeError):
            pcc.remove_re_framework("12345")

    def test_remove_re_framework_restores_standard_build_when_pd_upscaler_active(self):
        d = self.root / "steamapps/common/REGame"
        d.mkdir(parents=True)
        (d / "dinput8.dll").write_bytes(b"pd-upscaler build")
        (d / "dinput8.dll.rhi_standard_backup").write_bytes(b"standard build")
        state = pcc.load_state()
        state.setdefault("rhi_reframework_installs", {})["12345"] = {
            "path": str(d / "dinput8.dll"), "version": "PD-Upscaler",
        }
        pcc.save_state(state)
        rm = pcc.remove_re_framework("12345")
        self.assertTrue(rm["removed"])
        self.assertEqual((d / "dinput8.dll").read_bytes(), b"standard build")
        self.assertFalse((d / "dinput8.dll.rhi_standard_backup").exists())

    def test_scan_re_framework_reports_update_available(self):
        d = self.root / "steamapps/common/REGame"
        d.mkdir(parents=True)
        (d / "re_chunk_000.pak").write_bytes(b"x")
        state = pcc.load_state()
        state.setdefault("rhi_reframework_installs", {})["12345"] = {
            "path": str(d / "dinput8.dll"), "version": "nightly-1111-old",
        }
        (d / "dinput8.dll").write_bytes(b"old build")
        pcc.save_state(state)
        real_latest = pcc.re_framework_latest
        pcc.re_framework_latest = lambda: {"version": "nightly-2222-new", "url": "http://x"}
        try:
            status = pcc.scan_re_framework("12345", str(d))
        finally:
            pcc.re_framework_latest = real_latest
        self.assertTrue(status["installed"])
        self.assertTrue(status["update_available"])

    # ---- RHI: shader packs ----
    def test_expand_pack_dependencies_pulls_in_requires(self):
        expanded = pcc._expand_pack_dependencies(["Azen"])
        self.assertIn("Azen", expanded)
        self.assertIn("SmolbbsoopShaders", expanded)  # Azen's declared dependency

    def test_expand_pack_dependencies_handles_cycles_and_unknown_ids(self):
        # must not infinite-loop, and silently drops unknown pack ids
        expanded = pcc._expand_pack_dependencies(["Azen", "bogus-pack-id"])
        self.assertNotIn("bogus-pack-id", expanded)

    def test_resolve_rhi_target_dir_prefers_reshade_install_path(self):
        """Regression: shader pack deploy/remove routes were passing the
        raw Steam install root instead of the exe's actual directory -
        live-testing surfaced this on The Witcher 3, whose real exe (and
        ReShade's own install) sits in bin/x64_dx12/, several levels below
        the Steam root. resolve_rhi_target_dir must return the ReShade
        record's own directory when one exists, matching what addons
        already did correctly (Part 1f)."""
        d = self.root / "steamapps/common/TestGame"
        (d / "bin/x64_dx12").mkdir(parents=True)
        state = pcc.load_state()
        state.setdefault("rhi_reshade_installs", {})["12345"] = {
            "path": str(d / "bin/x64_dx12/dxgi.dll"), "channel": "stable",
            "version": "6.8.0", "bitness": 64, "exe": str(d / "bin/x64_dx12/Game.exe"),
        }
        pcc.save_state(state)
        self.assertEqual(pcc.resolve_rhi_target_dir("12345", str(d)), d / "bin/x64_dx12")

    def test_resolve_rhi_target_dir_falls_back_to_exe_detection(self):
        d, exe = self._fake_game_exe()  # bin: TestGame/Game.exe (no subdir)
        self.assertEqual(pcc.resolve_rhi_target_dir("12345", str(d)), d)

        d2 = self.root / "steamapps/common/NestedGame"
        (d2 / "bin/x64").mkdir(parents=True)
        real_exe = d2 / "bin/x64/Game.exe"
        real_exe.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        self.assertEqual(pcc.resolve_rhi_target_dir("99999", str(d2)), d2 / "bin/x64")

    def test_deploy_shader_packs_route_uses_reshade_directory(self):
        """End-to-end: deploying shader packs via the HTTP route for a game
        whose ReShade install lives in a subdirectory must land the files
        there, not at the raw Steam install root."""
        d, exe = self._fake_game_exe()
        (d / "bin/x64").mkdir(parents=True)
        nested_exe = d / "bin/x64/Game.exe"
        nested_exe.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        state = pcc.load_state()
        state.setdefault("rhi_reshade_installs", {})["12345"] = {
            "path": str(d / "bin/x64/dxgi.dll"), "channel": "stable",
            "version": "6.8.0", "bitness": 64, "exe": str(nested_exe),
        }
        pcc.save_state(state)
        target = pcc.resolve_rhi_target_dir("12345", str(d))
        self.assertEqual(target, d / "bin/x64")

    def test_resolve_rhi_target_dir_falls_back_to_optiscaler_then_dxvk(self):
        """A game with OptiScaler or DXVK installed but no ReShade must
        still reuse that mod's own resolved directory, not re-guess via
        fresh exe detection - otherwise a second mod installed after the
        first could land in a different folder than the one already
        confirmed working."""
        d = self.root / "steamapps/common/OptiOnly"
        (d / "bin/x64_dx12").mkdir(parents=True)
        state = pcc.load_state()
        state.setdefault("rhi_optiscaler_installs", {})["111"] = {
            "install_path": str(d / "bin/x64_dx12"), "installed_as": "dxgi.dll",
        }
        pcc.save_state(state)
        self.assertEqual(pcc.resolve_rhi_target_dir("111", str(d)), d / "bin/x64_dx12")

        d2 = self.root / "steamapps/common/DxvkOnly"
        (d2 / "bin/x64").mkdir(parents=True)
        state = pcc.load_state()
        state.setdefault("rhi_dxvk_installs", {})["222"] = {
            "install_path": str(d2 / "bin/x64"),
        }
        pcc.save_state(state)
        self.assertEqual(pcc.resolve_rhi_target_dir("222", str(d2)), d2 / "bin/x64")

    def test_infer_api_from_path_detects_dx12_folder_name(self):
        """The Witcher 3's real bin/x64_dx12/witcher3.exe imports neither
        dxgi.dll nor d3d12.dll (loaded dynamically at runtime), so the
        static PE scan alone gives up - the folder name itself is the only
        remaining signal, and it's worth surfacing rather than silently
        saying nothing."""
        self.assertEqual(pcc._infer_api_from_path("/g/bin/x64_dx12/witcher3.exe"), "d3d12")
        self.assertEqual(pcc._infer_api_from_path("/g/bin/x64/witcher3.exe"), None)
        self.assertEqual(pcc._infer_api_from_path("/g/bin/DX11/game.exe"), "d3d11")

    def test_describe_graphics_api_labels_known_and_inferred(self):
        known = pcc.describe_graphics_api("d3d11", "/g/bin/x64/game.exe")
        self.assertEqual(known, {"label": "DirectX 11", "inferred": False})
        inferred = pcc.describe_graphics_api(None, "/g/bin/x64_dx12/game.exe")
        self.assertEqual(inferred["label"], "DirectX 12 (from folder name)")
        self.assertTrue(inferred["inferred"])
        unknown = pcc.describe_graphics_api(None, "/g/bin/x64/game.exe")
        self.assertEqual(unknown, {"label": None, "inferred": False})

    def test_find_game_exe_candidates_detects_dual_dx11_dx12_builds(self):
        """Regression for the exact case that caused ReShade to silently
        install into the wrong folder: a game shipping BOTH a statically-
        detectable DX11 build and a DX12 build whose exe imports nothing
        (dynamic LoadLibrary), distinguishable only by its folder name."""
        d = self.root / "steamapps/common/DualBuild"
        (d / "bin/x64").mkdir(parents=True)
        (d / "bin/x64_dx12").mkdir(parents=True)
        dx11_exe = d / "bin/x64/witcher3.exe"
        dx11_exe.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        dx12_exe = d / "bin/x64_dx12/witcher3.exe"
        dx12_exe.write_bytes(_build_fake_pe64())  # no graphics imports at all

        candidates = pcc.find_game_exe_candidates(str(d))
        paths = {c["path"]: c for c in candidates}
        self.assertIn(str(dx11_exe), paths)
        self.assertIn(str(dx12_exe), paths)
        self.assertEqual(paths[str(dx11_exe)]["label"], "DirectX 11")
        self.assertFalse(paths[str(dx11_exe)]["inferred"])
        self.assertEqual(paths[str(dx12_exe)]["label"], "DirectX 12 (from folder name)")
        self.assertTrue(paths[str(dx12_exe)]["inferred"])
        # real detections rank ahead of folder-name inference
        self.assertLess(candidates.index(paths[str(dx11_exe)]),
                        candidates.index(paths[str(dx12_exe)]))

    def test_detect_game_builds_flags_multiple_builds(self):
        d = self.root / "steamapps/common/DualBuild2"
        (d / "bin/x64").mkdir(parents=True)
        (d / "bin/x64_dx12").mkdir(parents=True)
        (d / "bin/x64/game.exe").write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        (d / "bin/x64_dx12/game.exe").write_bytes(_build_fake_pe64())
        result = pcc.detect_game_builds(str(d))
        self.assertTrue(result["has_multiple_builds"])

        single = self.root / "steamapps/common/SingleBuild"
        single.mkdir(parents=True)
        (single / "game.exe").write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        self.assertFalse(pcc.detect_game_builds(str(single))["has_multiple_builds"])

    def test_shader_pack_catalog_matches_ported_data(self):
        catalog = pcc.get_shader_pack_catalog()
        self.assertGreaterEqual(len(catalog), 40)   # ~46 packs per RHI's README
        ids = {p["id"] for p in catalog}
        self.assertIn("Lilium", ids)
        self.assertIn("CrosireMaster", ids)
        lilium = next(p for p in catalog if p["id"] == "Lilium")
        self.assertEqual(lilium["category"], "essential")
        self.assertFalse(lilium["cached"])   # nothing downloaded yet in this test

    def _fake_shader_zip(self, root_folder, files):
        import zipfile, io as _io
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for rel, content in files.items():
                zf.writestr(f"{root_folder}/{rel}" if root_folder else rel, content)
        return buf.getvalue()

    def test_ensure_shader_pack_extracts_shaders_and_textures(self):
        zip_bytes = self._fake_shader_zip("reshade-shaders-main", {
            "Shaders/HDR.fx": b"fx content",
            "Shaders/ReShade.fxh": b"framework header",
            "Textures/noise.png": b"texture bytes",
            "README.md": b"not a shader, must be skipped",
        })
        real = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: zip_bytes
        try:
            files = pcc.ensure_shader_pack("CrosireMaster")
        finally:
            pcc._gh_bytes = real
        self.assertIn("Shaders/CrosireMaster/HDR.fx", files)
        self.assertIn("Textures/CrosireMaster/noise.png", files)
        self.assertTrue((pcc.RHI_DATA_DIR / "shaders" / "Shaders/CrosireMaster/HDR.fx").is_file())
        # shared framework header copied to the staging root too
        self.assertTrue((pcc.RESHADE_SHADERS_STAGE_DIR / "ReShade.fxh").is_file())
        self.assertFalse(any("README" in f for f in files))
        # second call is served from the recorded file list, no re-download
        pcc._gh_bytes = lambda url, task=None: (_ for _ in ()).throw(
            RuntimeError("should not re-download"))
        try:
            files2 = pcc.ensure_shader_pack("CrosireMaster")
        finally:
            pcc._gh_bytes = real
        self.assertEqual(files, files2)

    def test_ensure_shader_pack_excludes_known_bad_files(self):
        zip_bytes = self._fake_shader_zip("pack-main", {
            "Shaders/Good.fx": b"fine",
            "Shaders/NTSCCustom.fx": b"known broken",
        })
        real = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: zip_bytes
        try:
            files = pcc.ensure_shader_pack("CrosireMaster")
        finally:
            pcc._gh_bytes = real
        self.assertTrue(any("Good.fx" in f for f in files))
        self.assertFalse(any("NTSCCustom.fx" in f for f in files))

    def test_ensure_shader_pack_extracts_7z_archives(self):
        """Regression: Lilium HDR Shaders' real GitHub release ships as a
        .7z (its catalog entry even says asset_ext=".7z"), but
        ensure_shader_pack always fed the download straight into
        zipfile.ZipFile regardless of format - live-testing confirmed this
        crashes with "File is not a zip file" for any pack that isn't
        actually a zip. Must now sniff the real magic bytes and extract via
        the system 7z binary instead."""
        if not pcc._find_7z_binary():
            self.skipTest("no system 7z binary available")
        src = self.root / "_7z_src"
        (src / "Shaders").mkdir(parents=True)
        (src / "Shaders" / "HDR.fx").write_bytes(b"hdr fx content")
        archive = self.root / "pack.7z"
        subprocess.run([pcc._find_7z_binary(), "a", str(archive), str(src / "Shaders")],
                       check=True, capture_output=True)
        seven_zip_bytes = archive.read_bytes()
        self.assertEqual(seven_zip_bytes[:6], b"7z\xbc\xaf\x27\x1c")

        real_bytes, real_json = pcc._gh_bytes, pcc._gh_json
        pcc._gh_bytes = lambda url, task=None: seven_zip_bytes
        pcc._gh_json = lambda url: {"assets": [
            {"name": "Lilium.7z", "browser_download_url": "https://example/Lilium.7z"}]}
        try:
            files = pcc.ensure_shader_pack("Lilium")
        finally:
            pcc._gh_bytes, pcc._gh_json = real_bytes, real_json
        self.assertIn("Shaders/Lilium/HDR.fx", files)
        self.assertTrue((pcc.RHI_DATA_DIR / "shaders" / "Shaders/Lilium/HDR.fx").is_file())

    def test_ensure_shader_pack_unknown_id_raises(self):
        with self.assertRaises(RuntimeError):
            pcc.ensure_shader_pack("NotARealPack")

    def test_ensure_shader_pack_force_redownloads_and_prunes_stale_files(self):
        zip_v1 = self._fake_shader_zip("pack-main", {
            "Shaders/Old.fx": b"old", "Shaders/Keep.fx": b"keep v1",
        })
        real_bytes = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: zip_v1
        try:
            files1 = pcc.ensure_shader_pack("CrosireMaster")
        finally:
            pcc._gh_bytes = real_bytes
        self.assertIn("Shaders/CrosireMaster/Old.fx", files1)
        self.assertTrue((pcc.RHI_DATA_DIR / "shaders" / "Shaders/CrosireMaster/Old.fx").is_file())

        # Old.fx renamed away upstream, Keep.fx's content changed
        zip_v2 = self._fake_shader_zip("pack-main", {
            "Shaders/Keep.fx": b"keep v2", "Shaders/New.fx": b"new",
        })
        pcc._gh_bytes = lambda url, task=None: zip_v2
        try:
            files2 = pcc.ensure_shader_pack("CrosireMaster", force=True)
        finally:
            pcc._gh_bytes = real_bytes
        self.assertNotIn("Shaders/CrosireMaster/Old.fx", files2)
        self.assertIn("Shaders/CrosireMaster/New.fx", files2)
        # stale file actually deleted from staging, not just untracked
        self.assertFalse((pcc.RHI_DATA_DIR / "shaders" / "Shaders/CrosireMaster/Old.fx").exists())
        self.assertEqual((pcc.RHI_DATA_DIR / "shaders" / "Shaders/CrosireMaster/Keep.fx")
                         .read_bytes(), b"keep v2")

    def test_check_shader_pack_update_establishes_baseline_then_detects_change(self):
        zip_bytes = self._fake_shader_zip("pack-main", {"Shaders/A.fx": b"a"})
        real_bytes = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: zip_bytes
        try:
            pcc.ensure_shader_pack("CrosireMaster")
        finally:
            pcc._gh_bytes = real_bytes

        real_signal = pcc._shader_pack_latest_signal
        pcc._shader_pack_latest_signal = lambda pack, release=None: "sha-v1"
        try:
            # First check ever: nothing to compare against yet -> establishes
            # baseline, reports no update.
            self.assertFalse(pcc.check_shader_pack_update("CrosireMaster", force=True))
            # Same signal again -> still no update.
            self.assertFalse(pcc.check_shader_pack_update("CrosireMaster", force=True))
            pcc._shader_pack_latest_signal = lambda pack, release=None: "sha-v2"
            self.assertTrue(pcc.check_shader_pack_update("CrosireMaster", force=True))
            # Cached (not forced) - stays True without re-checking, even if
            # the live signal reverted (simulates staying within the 6h window).
            pcc._shader_pack_latest_signal = lambda pack, release=None: "sha-v1"
            self.assertTrue(pcc.check_shader_pack_update("CrosireMaster", force=False))
        finally:
            pcc._shader_pack_latest_signal = real_signal

    def test_check_shader_pack_update_false_for_uncached_pack(self):
        self.assertFalse(pcc.check_shader_pack_update("CrosireMaster"))

    def test_get_shader_pack_catalog_reports_update_available(self):
        zip_bytes = self._fake_shader_zip("pack-main", {"Shaders/A.fx": b"a"})
        real_bytes = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: zip_bytes
        try:
            pcc.ensure_shader_pack("CrosireMaster")
        finally:
            pcc._gh_bytes = real_bytes
        real_signal = pcc._shader_pack_latest_signal
        pcc._shader_pack_latest_signal = lambda pack, release=None: "sha-v1"
        try:
            catalog = {p["id"]: p for p in pcc.get_shader_pack_catalog()}
            self.assertTrue(catalog["CrosireMaster"]["cached"])
            self.assertFalse(catalog["CrosireMaster"]["update_available"])  # baseline just set
            pcc._shader_pack_latest_signal = lambda pack, release=None: "sha-v2"
            state = pcc.load_state()
            state["rhi_shader_pack_update_checks"]["CrosireMaster"]["ts"] = 0   # expire cache
            pcc.save_state(state)
            catalog2 = {p["id"]: p for p in pcc.get_shader_pack_catalog()}
            self.assertTrue(catalog2["CrosireMaster"]["update_available"])
            self.assertFalse(catalog2["SweetFX"]["update_available"])   # never downloaded
        finally:
            pcc._shader_pack_latest_signal = real_signal

    def test_extract_fx_files(self):
        self.assertEqual(pcc._extract_fx_files("TechniqueA@HDR.fx,TechniqueB@Bloom.fx"),
                         {"HDR.fx", "Bloom.fx"})
        self.assertEqual(pcc._extract_fx_files(""), set())
        self.assertEqual(pcc._extract_fx_files("no-at-sign-here"), set())
        # duplicate technique referencing the same file, plus stray whitespace
        self.assertEqual(pcc._extract_fx_files(" TechA@HDR.fx , TechB@HDR.fx "), {"HDR.fx"})

    def test_extract_fx_files_from_preset(self):
        preset = (
            "[TECHNIQUES]\n"
            "Techniques=TechA@HDR.fx,TechB@Bloom.fx\n"
            "TechniqueSorting=TechA@HDR.fx\n"   # not a Techniques= line, ignored
            "[HDR.fx]\n"
            "Enabled=1\n"
        )
        self.assertEqual(pcc._extract_fx_files_from_preset(preset), {"HDR.fx", "Bloom.fx"})

    def _seed_shader_pack_cache(self, pack_id, files):
        """Writes real files under RHI_DATA_DIR/shaders/ and records them in
        state, matching what ensure_shader_pack would leave behind - lets
        resolve/apply-preset tests avoid mocking network downloads for all
        14+ catalog packs."""
        for rel, content in files.items():
            p = pcc.RHI_DATA_DIR / "shaders" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        state = pcc.load_state()
        state.setdefault("rhi_shader_packs", {})[pack_id] = {
            "files": list(files.keys()), "fetched_at": "2026-01-01T00:00:00Z", "signal": None}
        pcc.save_state(state)

    def test_resolve_preset_shader_packs_matches_and_reports_unresolved(self):
        self._seed_shader_pack_cache("CrosireMaster", {"Shaders/CrosireMaster/HDR.fx": b"x"})
        self._seed_shader_pack_cache("SweetFX", {"Shaders/SweetFX/Bloom.fx": b"x"})
        real_ensure = pcc.ensure_shader_pack
        pcc.ensure_shader_pack = (lambda pid, task_id=None, force=False:
            pcc.load_state().get("rhi_shader_packs", {}).get(pid, {}).get("files", []))
        try:
            r = pcc.resolve_preset_shader_packs({"HDR.fx", "Bloom.fx", "Nonexistent.fx"})
        finally:
            pcc.ensure_shader_pack = real_ensure
        self.assertEqual(r["matched"], ["CrosireMaster", "SweetFX"])
        self.assertEqual(r["unresolved"], ["Nonexistent.fx"])

    def test_apply_preset_shader_packs_full_flow(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        self._seed_shader_pack_cache("CrosireMaster", {"Shaders/CrosireMaster/HDR.fx": b"hdr content"})
        real_ensure = pcc.ensure_shader_pack
        pcc.ensure_shader_pack = (lambda pid, task_id=None, force=False:
            pcc.load_state().get("rhi_shader_packs", {}).get(pid, {}).get("files", []))
        preset = "Techniques=SomeTechnique@HDR.fx,OtherTechnique@Missing.fx\n"
        try:
            r = pcc.apply_preset_shader_packs("12345", str(d), preset)
        finally:
            pcc.ensure_shader_pack = real_ensure
        self.assertEqual(r["matched"], ["CrosireMaster"])
        self.assertEqual(r["unresolved"], ["Missing.fx"])
        self.assertEqual(r["deployed"], 1)
        self.assertIn("CrosireMaster", pcc.get_game_shader_selection("12345"))
        deployed_file = d / "reshade-shaders/Shaders/CrosireMaster/HDR.fx"
        self.assertEqual(deployed_file.read_bytes(), b"hdr content")

    def test_apply_preset_shader_packs_no_techniques_is_noop(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        r = pcc.apply_preset_shader_packs("12345", str(d), "not a real preset at all")
        self.assertEqual(r, {"matched": [], "unresolved": [], "deployed": 0})
        self.assertEqual(pcc.get_game_shader_selection("12345"), [])

    def test_deploy_shader_packs_full_flow_and_remove(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        zip_bytes = self._fake_shader_zip("pack-main", {"Shaders/HDR.fx": b"fx"})
        real = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: zip_bytes
        try:
            r = pcc.deploy_shader_packs(str(d), ["CrosireMaster"])
        finally:
            pcc._gh_bytes = real

        self.assertEqual(r["deployed"], 1)
        deployed_file = d / "reshade-shaders/Shaders/CrosireMaster/HDR.fx"
        self.assertTrue(deployed_file.is_file())
        marker = d / "reshade-shaders" / pcc.RESHADE_SHADERS_MANAGED_MARKER
        self.assertTrue(marker.is_file())

        rm = pcc.remove_reshade_shaders(str(d))
        self.assertTrue(rm["removed"])
        self.assertFalse((d / "reshade-shaders").exists())

    def test_deploy_shader_packs_preserves_preexisting_user_folder(self):
        d = self.root / "steamapps/common/TestGame"
        (d / "reshade-shaders").mkdir(parents=True)
        (d / "reshade-shaders" / "MyOwnShader.fx").write_bytes(b"user's own file")
        zip_bytes = self._fake_shader_zip("pack-main", {"Shaders/HDR.fx": b"fx"})
        real = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: zip_bytes
        try:
            pcc.deploy_shader_packs(str(d), ["CrosireMaster"])
        finally:
            pcc._gh_bytes = real

        original = d / "reshade-shaders-original"
        self.assertTrue(original.is_dir())
        self.assertEqual((original / "MyOwnShader.fx").read_bytes(), b"user's own file")

        pcc.remove_reshade_shaders(str(d))
        # the user's original folder must come back once PCC's managed one is gone
        self.assertTrue((d / "reshade-shaders" / "MyOwnShader.fx").is_file())
        self.assertFalse(original.exists())

    def test_deploy_shader_packs_prunes_deselected_pack_files(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        zip_a = self._fake_shader_zip("a-main", {"Shaders/A.fx": b"a"})
        zip_b = self._fake_shader_zip("b-main", {"Shaders/B.fx": b"b"})
        real = pcc._gh_bytes

        pcc._gh_bytes = lambda url, task=None: zip_a
        try:
            pcc.deploy_shader_packs(str(d), ["CrosireMaster"])
        finally:
            pcc._gh_bytes = real
        self.assertTrue((d / "reshade-shaders/Shaders/CrosireMaster/A.fx").is_file())

        pcc._gh_bytes = lambda url, task=None: zip_b
        try:
            pcc.deploy_shader_packs(str(d), ["SweetFX"])
        finally:
            pcc._gh_bytes = real
        self.assertFalse((d / "reshade-shaders/Shaders/CrosireMaster/A.fx").exists())
        self.assertTrue((d / "reshade-shaders/Shaders/SweetFX/B.fx").is_file())

    def test_game_shader_selection_get_set(self):
        self.assertEqual(pcc.get_game_shader_selection("12345"), [])
        pcc.set_game_shader_selection("12345", ["Lilium", "SweetFX"])
        self.assertEqual(pcc.get_game_shader_selection("12345"), ["Lilium", "SweetFX"])
        pcc.set_game_shader_selection("12345", [])
        self.assertEqual(pcc.get_game_shader_selection("12345"), [])

    # ---- RHI: ReShade addons ----
    def test_parse_addons_ini_skips_disabled_sections(self):
        content = (
            "[02]\n"
            "PackageName=Swap chain override by crosire\n"
            "PackageDescription=Force windowed/fullscreen\n"
            "DownloadUrl32=http://x/swapchain.addon32\n"
            "DownloadUrl64=http://x/swapchain.addon64\n"
            "RepositoryUrl=http://repo\n"
            "\n"
            "# [02]\n"
            "# PackageName=Framerate Limiter by crosire\n"
            "# PackageDescription=disabled entry, must be skipped\n"
            "\n"
            "[01]\n"
            "PackageName=FreePIE by crosire\n"
            "DownloadUrl32=http://x/freepie.addon32\n"
            "DownloadUrl64=http://x/freepie.addon64\n"
        )
        parsed = pcc._parse_addons_ini(content)
        names = [a["PackageName"] for a in parsed]
        self.assertIn("Swap chain override by crosire", names)
        self.assertIn("FreePIE by crosire", names)
        self.assertNotIn("Framerate Limiter by crosire", names)
        self.assertEqual(len(parsed), 2)

    def test_parse_addons_ini_skips_excluded_section_ids(self):
        # "00"/"21"/"26" are excluded even when active (uncommented) - they're
        # managed by RHI itself or N/A, per RHI's AddonsIniParser.ExcludedSections.
        content = (
            "[00]\n"
            "PackageName=Swap chain override by crosire\n"
            "DownloadUrl32=http://x/swapchain.addon32\n"
            "DownloadUrl64=http://x/swapchain.addon64\n"
            "\n"
            "[21]\n"
            "PackageName=Excluded Twentyone\n"
            "DownloadUrl64=http://x/twentyone.addon64\n"
            "\n"
            "[26]\n"
            "PackageName=Excluded Twentysix\n"
            "DownloadUrl64=http://x/twentysix.addon64\n"
            "\n"
            "[03]\n"
            "PackageName=A Real Addon\n"
            "DownloadUrl64=http://x/real.addon64\n"
        )
        parsed = pcc._parse_addons_ini(content)
        names = [a["PackageName"] for a in parsed]
        self.assertEqual(names, ["A Real Addon"])

    def test_slugify_addon_name(self):
        self.assertEqual(pcc._slugify_addon_name("Swap chain override by crosire"),
                         "swap-chain-override-by-crosire")

    def test_reshade_addons_catalog_fetches_and_caches(self):
        ini_text = ("[02]\nPackageName=Test Addon\n"
                   "PackageDescription=desc\n"
                   "DownloadUrl32=http://x/a.addon32\n"
                   "DownloadUrl64=http://x/a.addon64\n")

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return ini_text.encode()
        real_urlopen = pcc.urllib.request.urlopen
        pcc.urllib.request.urlopen = lambda req, timeout=30: FakeResp()
        try:
            catalog = pcc.reshade_addons_catalog()
        finally:
            pcc.urllib.request.urlopen = real_urlopen
        # + the always-present renodx-devkit/renodx-dlssfix hardcoded entries
        self.assertEqual(len(catalog), 3)
        self.assertEqual(catalog[0]["id"], "test-addon")
        self.assertEqual(catalog[0]["download_url64"], "http://x/a.addon64")
        ids = {a["id"] for a in catalog}
        self.assertIn("renodx-devkit", ids)
        self.assertIn("renodx-dlssfix", ids)
        # second call is served from the 6h cache, no network needed
        pcc.urllib.request.urlopen = lambda req, timeout=30: (_ for _ in ()).throw(
            RuntimeError("should not fetch again"))
        try:
            catalog2 = pcc.reshade_addons_catalog()
        finally:
            pcc.urllib.request.urlopen = real_urlopen
        self.assertEqual(catalog2, catalog)

    def test_deploy_reshade_addons_full_flow_and_remove(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        state = pcc.load_state()
        state["reshade_addons_catalog"] = {"ts": pcc.time.time(), "data": [
            {"id": "test-addon", "name": "Test Addon", "description": "",
             "download_url32": "http://x/a.addon32", "download_url64": "http://x/a.addon64",
             "repository_url": ""}]}
        pcc.save_state(state)
        real = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: b"fake addon binary"
        try:
            r = pcc.deploy_reshade_addons(str(d), ["test-addon"], 64)
        finally:
            pcc._gh_bytes = real
        self.assertEqual(r["deployed"], 1)
        target = d / "test-addon.addon64"
        self.assertEqual(target.read_bytes(), b"fake addon binary")

        rm = pcc.remove_reshade_addons(str(d))
        self.assertEqual(rm["removed"], 1)
        self.assertFalse(target.exists())

    def test_custom_addons_catalog_groups_by_base_name(self):
        pcc.RESHADE_CUSTOM_ADDONS_DIR.mkdir(parents=True, exist_ok=True)
        (pcc.RESHADE_CUSTOM_ADDONS_DIR / "renodx-dlss5.addon64").write_bytes(b"64-bit build")
        (pcc.RESHADE_CUSTOM_ADDONS_DIR / "onlyone.addon32").write_bytes(b"32-bit only")
        (pcc.RESHADE_CUSTOM_ADDONS_DIR / "not-an-addon.txt").write_bytes(b"ignored")
        catalog = {a["id"]: a for a in pcc.custom_addons_catalog()}
        self.assertEqual(set(catalog), {"custom-renodx-dlss5", "custom-onlyone"})
        dlss5 = catalog["custom-renodx-dlss5"]
        self.assertTrue(dlss5["is_custom"])
        self.assertEqual(dlss5["custom_path64"],
                         str(pcc.RESHADE_CUSTOM_ADDONS_DIR / "renodx-dlss5.addon64"))
        self.assertIsNone(dlss5["custom_path32"])
        self.assertIsNone(catalog["custom-onlyone"]["custom_path64"])

    def test_custom_addons_catalog_empty_when_no_dir(self):
        self.assertEqual(pcc.custom_addons_catalog(), [])

    def test_reshade_addons_catalog_includes_custom_addons_live_not_cached(self):
        state = pcc.load_state()
        state["reshade_addons_catalog"] = {"ts": pcc.time.time(), "data": [
            {"id": "test-addon", "name": "Test Addon", "description": "",
             "download_url32": None, "download_url64": "http://x/a.addon64",
             "repository_url": ""}]}
        pcc.save_state(state)
        pcc.RESHADE_CUSTOM_ADDONS_DIR.mkdir(parents=True, exist_ok=True)
        (pcc.RESHADE_CUSTOM_ADDONS_DIR / "mytest.addon64").write_bytes(b"x")
        catalog = pcc.reshade_addons_catalog()
        ids = {a["id"] for a in catalog}
        self.assertIn("test-addon", ids)
        self.assertIn("custom-mytest", ids)
        # dropping a second file shows up immediately, without waiting on
        # the 6h cache (unlike the rest of the catalog, which is cached)
        (pcc.RESHADE_CUSTOM_ADDONS_DIR / "mytest2.addon64").write_bytes(b"y")
        catalog2 = pcc.reshade_addons_catalog()
        self.assertIn("custom-mytest2", {a["id"] for a in catalog2})
        # and the underlying cache itself was never polluted with it
        self.assertNotIn("custom-mytest2",
                         {a["id"] for a in pcc.load_state()["reshade_addons_catalog"]["data"]})

    def test_deploy_reshade_addons_custom_addon_from_local_file(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        pcc.RESHADE_CUSTOM_ADDONS_DIR.mkdir(parents=True, exist_ok=True)
        (pcc.RESHADE_CUSTOM_ADDONS_DIR / "devbuild.addon64").write_bytes(b"dev build content")
        state = pcc.load_state()
        state["reshade_addons_catalog"] = {"ts": pcc.time.time(), "data": []}
        pcc.save_state(state)
        real = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: (_ for _ in ()).throw(
            RuntimeError("custom addons must not hit the network"))
        try:
            r = pcc.deploy_reshade_addons(str(d), ["custom-devbuild"], 64)
        finally:
            pcc._gh_bytes = real
        self.assertEqual(r["deployed"], 1)
        self.assertEqual((d / "custom-devbuild.addon64").read_bytes(), b"dev build content")

    def test_deploy_reshade_addons_custom_addon_skips_missing_bitness(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        pcc.RESHADE_CUSTOM_ADDONS_DIR.mkdir(parents=True, exist_ok=True)
        (pcc.RESHADE_CUSTOM_ADDONS_DIR / "devbuild.addon64").write_bytes(b"64-bit only")
        state = pcc.load_state()
        state["reshade_addons_catalog"] = {"ts": pcc.time.time(), "data": []}
        pcc.save_state(state)
        r = pcc.deploy_reshade_addons(str(d), ["custom-devbuild"], 32)
        self.assertEqual(r["deployed"], 0)
        self.assertIn("no 32-bit build available", r["skipped"][0])

    def test_deploy_reshade_addons_prunes_deselected(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        state = pcc.load_state()
        state["reshade_addons_catalog"] = {"ts": pcc.time.time(), "data": [
            {"id": "addon-a", "name": "A", "description": "",
             "download_url32": "http://x/a.addon32", "download_url64": "http://x/a.addon64",
             "repository_url": ""},
            {"id": "addon-b", "name": "B", "description": "",
             "download_url32": "http://x/b.addon32", "download_url64": "http://x/b.addon64",
             "repository_url": ""},
        ]}
        pcc.save_state(state)
        real = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: b"binary"
        try:
            pcc.deploy_reshade_addons(str(d), ["addon-a", "addon-b"], 64)
            self.assertTrue((d / "addon-a.addon64").is_file())
            self.assertTrue((d / "addon-b.addon64").is_file())
            pcc.deploy_reshade_addons(str(d), ["addon-b"], 64)
        finally:
            pcc._gh_bytes = real
        self.assertFalse((d / "addon-a.addon64").exists())
        self.assertTrue((d / "addon-b.addon64").is_file())

    def test_renodx_dlss5_latest_picks_highest_version(self):
        releases = [
            {"tag_name": "renodx-dlss5-2.5", "assets": [
                {"name": "renodx-dlss5_2.5.zip", "browser_download_url": "http://x/2.5.zip"}]},
            {"tag_name": "renodx-dlss5-2.4", "assets": [
                {"name": "renodx-dlss5_2.4.zip", "browser_download_url": "http://x/2.4.zip"}]},
            {"tag_name": "RHI-2.4.4", "assets": []},  # unrelated release, must be ignored
        ]
        real_gh_json = pcc._gh_json
        pcc._gh_json = lambda url: releases
        try:
            info = pcc.renodx_dlss5_latest()
        finally:
            pcc._gh_json = real_gh_json
        self.assertEqual(info["version"], "2.5")
        self.assertEqual(info["url"], "http://x/2.5.zip")

    def test_reshade_addons_catalog_includes_renodx_dlss5(self):
        real_gh_json = pcc._gh_json
        pcc._gh_json = lambda url: [{"tag_name": "renodx-dlss5-2.5", "assets": [
            {"name": "renodx-dlss5_2.5.zip", "browser_download_url": "http://x/2.5.zip"}]}]
        real_urlopen = pcc.urllib.request.urlopen

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"[00]\nPackageName=Test\nDownloadUrl64=http://x/t.addon64\n"
        pcc.urllib.request.urlopen = lambda req, timeout=30: FakeResp()
        try:
            catalog = pcc.reshade_addons_catalog()
        finally:
            pcc._gh_json = real_gh_json
            pcc.urllib.request.urlopen = real_urlopen
        ids = {a["id"] for a in catalog}
        self.assertIn("renodx-dlss5", ids)
        dlss5 = next(a for a in catalog if a["id"] == "renodx-dlss5")
        self.assertEqual(dlss5["download_url64"], "http://x/2.5.zip")
        self.assertIsNone(dlss5["download_url32"])
        self.assertEqual(dlss5["zip_member"], "renodx-dlss5.addon64")

    def test_deploy_renodx_dlss5_extracts_from_zip_and_deploys_nr_dll(self):
        import zipfile, io
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        state = pcc.load_state()
        state["reshade_addons_catalog"] = {"ts": pcc.time.time(), "data": [
            {"id": "renodx-dlss5", "name": "RenoDX DLSS5 Setup (RTX 50 Series only)",
             "description": "", "download_url32": None, "download_url64": "http://x/dlss5.zip",
             "zip_member": "renodx-dlss5.addon64", "repository_url": ""}]}
        pcc.save_state(state)

        addon_zip_buf = io.BytesIO()
        with zipfile.ZipFile(addon_zip_buf, "w") as zf:
            zf.writestr("renodx-dlss5.addon64", b"fake renodx dlss5 addon binary")
        nr_zip_buf = io.BytesIO()
        with zipfile.ZipFile(nr_zip_buf, "w") as zf:
            zf.writestr("nvngx_dlssnr.dll", b"fake dlssnr dll")

        real_gh_bytes = pcc._gh_bytes
        real_manifest = pcc._rhi_dlss_manifest
        pcc._gh_bytes = (lambda url, task=None:
                         addon_zip_buf.getvalue() if url == "http://x/dlss5.zip" else nr_zip_buf.getvalue())
        pcc._rhi_dlss_manifest = lambda: {"dlssnr": [{"version": "310.8.0", "url": "http://x/nr.zip"}]}
        try:
            r = pcc.deploy_reshade_addons(str(d), ["renodx-dlss5"], 64)
        finally:
            pcc._gh_bytes = real_gh_bytes
            pcc._rhi_dlss_manifest = real_manifest
        self.assertEqual(r["deployed"], 1)
        self.assertEqual((d / "renodx-dlss5.addon64").read_bytes(), b"fake renodx dlss5 addon binary")
        self.assertEqual((d / "nvngx_dlssnr.dll").read_bytes(), b"fake dlssnr dll")

    def test_deploy_reshade_addons_reports_skipped_with_reason(self):
        """Regression: a real user hit this - RenoDX DLSS5 has no 32-bit
        build, and their ReShade install had (incorrectly, from a separate
        exe-detection bug) recorded bitness=32, so deploy silently returned
        0 with no explanation. Skipped addons must now say why."""
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        state = pcc.load_state()
        state["reshade_addons_catalog"] = {"ts": pcc.time.time(), "data": [
            {"id": "renodx-dlss5", "name": "RenoDX DLSS5 Setup (RTX 50 Series only)",
             "description": "", "download_url32": None, "download_url64": "http://x/dlss5.zip",
             "zip_member": "renodx-dlss5.addon64", "repository_url": ""}]}
        pcc.save_state(state)
        r = pcc.deploy_reshade_addons(str(d), ["renodx-dlss5"], 32)
        self.assertEqual(r["deployed"], 0)
        self.assertEqual(len(r["skipped"]), 1)
        self.assertIn("32-bit", r["skipped"][0])
        self.assertFalse((d / "renodx-dlss5.addon32").exists())

    def test_deploy_renodx_dlss5_never_overwrites_existing_nr_dll(self):
        import zipfile, io
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        (d / "nvngx_dlssnr.dll").write_bytes(b"user's own existing copy")
        state = pcc.load_state()
        state["reshade_addons_catalog"] = {"ts": pcc.time.time(), "data": [
            {"id": "renodx-dlss5", "name": "RenoDX DLSS5 Setup (RTX 50 Series only)",
             "description": "", "download_url32": None, "download_url64": "http://x/dlss5.zip",
             "zip_member": "renodx-dlss5.addon64", "repository_url": ""}]}
        pcc.save_state(state)
        addon_zip_buf = io.BytesIO()
        with zipfile.ZipFile(addon_zip_buf, "w") as zf:
            zf.writestr("renodx-dlss5.addon64", b"fake addon")
        real_gh_bytes = pcc._gh_bytes
        pcc._gh_bytes = lambda url, task=None: addon_zip_buf.getvalue()
        try:
            pcc.deploy_reshade_addons(str(d), ["renodx-dlss5"], 64)
        finally:
            pcc._gh_bytes = real_gh_bytes
        self.assertEqual((d / "nvngx_dlssnr.dll").read_bytes(), b"user's own existing copy")

    def test_ensure_dlssnr_cached_downloads_from_rhi_manifest(self):
        import zipfile, io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("nvngx_dlssnr.dll", b"fake dlssnr dll")
        real_manifest, real_bytes = pcc._rhi_dlss_manifest, pcc._gh_bytes
        pcc._rhi_dlss_manifest = lambda: {"dlssnr": [{"version": "310.8.0", "url": "http://x/nr.zip"}]}
        pcc._gh_bytes = lambda url, task=None: buf.getvalue()
        try:
            p = pcc.ensure_dlssnr_cached()
        finally:
            pcc._rhi_dlss_manifest = real_manifest
            pcc._gh_bytes = real_bytes
        self.assertTrue(p.is_file())
        self.assertEqual(p.read_bytes(), b"fake dlssnr dll")
        # second call is served from disk cache, no network needed
        pcc._gh_bytes = lambda url, task=None: (_ for _ in ()).throw(RuntimeError("should not fetch again"))
        pcc._rhi_dlss_manifest = lambda: {"dlssnr": [{"version": "310.8.0", "url": "http://x/nr.zip"}]}
        try:
            p2 = pcc.ensure_dlssnr_cached()
        finally:
            pcc._gh_bytes = real_bytes
            pcc._rhi_dlss_manifest = real_manifest
        self.assertEqual(p2, p)

    def test_find_dlssnr_target_dir_prefers_sr_then_fg_then_rr(self):
        import struct as _s
        def mk(a, b, c, d):
            data = b"MZ" + b"\x00" * 64 + _s.pack("<I", 0xFEEF04BD)
            data += _s.pack("<I", 0x00010000)
            data += _s.pack("<II", (a << 16) | b, (c << 16) | d) + b"\x00" * 32
            return data
        game = Path(self.tmp.name) / "GameNR"
        sr_dir = game / "Engine" / "DLSS"
        sr_dir.mkdir(parents=True)
        (sr_dir / "nvngx_dlss.dll").write_bytes(mk(310, 7, 0, 0))
        fg_dir = game / "Engine" / "Streamline"
        fg_dir.mkdir(parents=True)
        (fg_dir / "nvngx_dlssg.dll").write_bytes(mk(310, 7, 0, 0))
        target = pcc.find_dlssnr_target_dir(str(game))
        self.assertEqual(target, sr_dir)

    def test_find_dlssnr_target_dir_none_without_other_dlss(self):
        game = Path(self.tmp.name) / "GameNoDLSS"
        game.mkdir()
        # block the climb from wandering into the mock library's TestGame
        # fixture, which does have a DLSS DLL of its own
        other = str(self.root / "steamapps/common/TestGame")
        self.assertIsNone(pcc.find_dlssnr_target_dir(str(game), other_roots=[other]))

    def test_deploy_dlssnr_to_game_copies_into_sr_dir(self):
        game = Path(self.tmp.name) / "GameDeployNR"
        sr_dir = game / "Binaries"
        sr_dir.mkdir(parents=True)
        import struct as _s
        data = b"MZ" + b"\x00" * 64 + _s.pack("<I", 0xFEEF04BD)
        data += _s.pack("<I", 0x00010000)
        data += _s.pack("<II", (310 << 16) | 7, 0) + b"\x00" * 32
        (sr_dir / "nvngx_dlss.dll").write_bytes(data)

        nr_src = Path(self.tmp.name) / "cached_nr.dll"
        nr_src.write_bytes(b"fake nr dll v310.8.0")
        real_ensure = pcc.ensure_dlssnr_cached
        pcc.ensure_dlssnr_cached = lambda: nr_src
        try:
            r = pcc.deploy_dlssnr_to_game(str(game))
        finally:
            pcc.ensure_dlssnr_cached = real_ensure
        self.assertTrue(r["deployed"])
        dest = sr_dir / "nvngx_dlssnr.dll"
        self.assertEqual(dest.read_bytes(), b"fake nr dll v310.8.0")

    def test_deploy_dlssnr_to_game_backs_up_existing_before_overwrite(self):
        game = Path(self.tmp.name) / "GameRedeployNR"
        sr_dir = game / "Binaries"
        sr_dir.mkdir(parents=True)
        import struct as _s
        data = b"MZ" + b"\x00" * 64 + _s.pack("<I", 0xFEEF04BD)
        data += _s.pack("<I", 0x00010000)
        data += _s.pack("<II", (310 << 16) | 7, 0) + b"\x00" * 32
        (sr_dir / "nvngx_dlss.dll").write_bytes(data)
        (sr_dir / "nvngx_dlssnr.dll").write_bytes(b"user's own existing NR copy")

        nr_src = Path(self.tmp.name) / "cached_nr.dll"
        nr_src.write_bytes(b"new nr dll")
        real_ensure = pcc.ensure_dlssnr_cached
        pcc.ensure_dlssnr_cached = lambda: nr_src
        try:
            pcc.deploy_dlssnr_to_game(str(game))
        finally:
            pcc.ensure_dlssnr_cached = real_ensure
        dest = sr_dir / "nvngx_dlssnr.dll"
        self.assertEqual(dest.read_bytes(), b"new nr dll")
        bak = pcc._backup_path(dest)
        self.assertEqual(bak.read_bytes(), b"user's own existing NR copy")

    def test_deploy_dlssnr_to_game_raises_without_other_dlss(self):
        game = Path(self.tmp.name) / "GameNoDLSSDeploy"
        game.mkdir()
        other = str(self.root / "steamapps/common/TestGame")
        with self.assertRaises(RuntimeError):
            pcc.deploy_dlssnr_to_game(str(game), other_roots=[other])

    def test_game_addon_selection_get_set(self):
        self.assertEqual(pcc.get_game_addon_selection("12345"), [])
        pcc.set_game_addon_selection("12345", ["addon-a"])
        self.assertEqual(pcc.get_game_addon_selection("12345"), ["addon-a"])

    # ---- RHI: OptiScaler ----
    def _fake_optiscaler_staging(self, nightly=False, version=None):
        """Pre-populates a ready staging dir AND stubs optiscaler_latest() to
        report the same version, so install/update flows take the
        already-staged fast path in ensure_optiscaler_staging() without
        hitting the real network (that function always calls
        optiscaler_latest() first, even when staging looks ready, to check
        for updates - matching RHI's own EnsureStagingAsync)."""
        d = pcc._optiscaler_staging_dir(nightly)
        d.mkdir(parents=True, exist_ok=True)
        (d / "OptiScaler.dll").write_bytes(b"OptiScaler fake build" + b"\x00" * 500)
        template = pcc.get_optiscaler_ini_template_path("NVIDIA", True, nightly)
        import shutil
        shutil.copy2(template, d / "OptiScaler.ini")
        version = version or ("20260101" if nightly else "v1.2.3")
        (d / "version.txt").write_text(version)
        real_latest = pcc.optiscaler_latest
        pcc.optiscaler_latest = (lambda nightly=False, _v=version: {
            "version": _v, "url": "http://x", "asset_name": "OptiScaler.7z"})
        self.addCleanup(lambda: setattr(pcc, "optiscaler_latest", real_latest))
        return d

    def _fake_optipatcher_asi(self):
        p = Path(self.tmp.name) / "OptiPatcher.asi"
        p.write_bytes(b"fake asi")
        return p

    def test_optiscaler_ini_template_paths_resolve_to_real_bundled_files(self):
        # Confirms the 4 real files ported from RHI are actually on disk and
        # distinct where they should be (dlss vs nodlss), identical where
        # RHI's own source is identical (nvidia.ini == amd-dlss.ini).
        for gpu_type, dlss_inputs, nightly in pcc._OPTISCALER_INI_CONFIGS:
            p = pcc.get_optiscaler_ini_template_path(gpu_type, dlss_inputs, nightly)
            self.assertTrue(p.is_file(), p)
        dlss_path = pcc.get_optiscaler_ini_template_path("NVIDIA", True, False)
        nodlss_path = pcc.get_optiscaler_ini_template_path("AMD", False, False)
        self.assertNotEqual(dlss_path.read_text(), nodlss_path.read_text())
        self.assertEqual(
            pcc.get_optiscaler_ini_template_path("NVIDIA", True, False),
            pcc.get_optiscaler_ini_template_path("AMD", True, False))

    def test_resolve_optiscaler_dll_name(self):
        self.assertEqual(pcc._resolve_optiscaler_dll_name("d3d11"), "dxgi.dll")
        self.assertEqual(pcc._resolve_optiscaler_dll_name("vulkan"), "winmm.dll")
        self.assertEqual(pcc._resolve_optiscaler_dll_name("vulkan", user_override="d3d12.dll"),
                         "d3d12.dll")

    def test_identify_dxgi_file_recognizes_optiscaler(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "dxgi.dll"
        p.write_bytes(b"junk header OptiScaler build info trailer" + b"\x00" * 1000)
        self.assertEqual(pcc._identify_dxgi_file(p), "optiscaler")
        p.write_bytes(b"totally unrelated content")
        self.assertEqual(pcc._identify_dxgi_file(p), "unknown")

    def test_identify_dxgi_file_optiscaler_signature_beats_size_gate(self):
        """Regression: a real OptiScaler.dll is ~25MB, well over the 15MB
        cutoff that (before this fix) short-circuited the whole function to
        "unknown" before the signature scan ever ran - which would have let
        a later ReShade/DXVK install silently clobber OptiScaler's file as
        if it were foreign, instead of correctly identifying and backing it
        up. The size cutoff must only gate the ReShade-specific staged-size
        fallback, never the signature scans themselves."""
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "dxgi.dll"
        p.write_bytes(b"OptiScaler build marker" + b"\x00" * (20 * 1024 * 1024))
        self.assertEqual(pcc._identify_dxgi_file(p), "optiscaler")

    def test_identify_dxgi_file_recognizes_dxvk(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "dxgi.dll"
        p.write_bytes(b"header DXVK_ build marker" + b"\x00" * 1000)
        self.assertEqual(pcc._identify_dxgi_file(p), "dxvk")
        p.write_bytes(b"header dxvk lowercase marker" + b"\x00" * 1000)
        self.assertEqual(pcc._identify_dxgi_file(p), "dxvk")

    def test_is_optiscaler_file_and_detect_installation(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        (d / "dxgi.dll").write_bytes(b"header OptiScaler marker" + b"\x00" * 2000)
        self.assertTrue(pcc._is_optiscaler_file(d / "dxgi.dll"))
        self.assertEqual(pcc.detect_optiscaler_installation(str(d)), "dxgi.dll")
        self.assertIsNone(pcc.detect_optiscaler_installation(str(d / "nope")))

    def test_ensure_optiscaler_staging_requires_7z(self):
        real_find = pcc._find_7z_binary
        real_latest = pcc.optiscaler_latest
        pcc._find_7z_binary = lambda: None
        pcc.optiscaler_latest = lambda nightly=False: {
            "version": "v9.9.9", "url": "http://x", "asset_name": "OptiScaler.7z"}
        try:
            with self.assertRaises(RuntimeError) as ctx:
                pcc.ensure_optiscaler_staging()
            self.assertIn("7-Zip", str(ctx.exception))
        finally:
            pcc._find_7z_binary = real_find
            pcc.optiscaler_latest = real_latest

    def test_ensure_optiscaler_staging_downloads_and_extracts_real_7z(self):
        if not pcc._find_7z_binary():
            self.skipTest("7z not installed on this machine")
        import subprocess
        src = Path(self.tmp.name) / "optiscaler_src"
        src.mkdir()
        (src / "OptiScaler.dll").write_bytes(b"fake OptiScaler build" + b"\x00" * 500)
        (src / "OptiScaler.ini").write_text("[Upscalers]\nSpoofing=auto\n")
        (src / "README.txt").write_text("readme")
        archive = Path(self.tmp.name) / "OptiScaler.7z"
        subprocess.run(["7z", "a", str(archive), str(src) + "/."],
                       check=True, capture_output=True)
        data = archive.read_bytes()

        real_latest, real_bytes = pcc.optiscaler_latest, pcc._gh_bytes
        pcc.optiscaler_latest = lambda nightly=False: {
            "version": "v9.9.9", "url": "http://x", "asset_name": "OptiScaler.7z"}
        pcc._gh_bytes = lambda url, task=None: data
        try:
            staging_dir = pcc.ensure_optiscaler_staging()
        finally:
            pcc.optiscaler_latest = real_latest
            pcc._gh_bytes = real_bytes
        self.assertTrue((staging_dir / "OptiScaler.dll").is_file())
        self.assertEqual(pcc.optiscaler_staging_version(), "v9.9.9")
        self.assertTrue(pcc.optiscaler_staging_ready())
        # Staging mirrors the extracted archive as-is (matches RHI's own
        # EnsureStagingAsync) - doc/script filtering happens later, only at
        # per-game deploy time in install_optiscaler().
        self.assertTrue((staging_dir / "README.txt").is_file())

    def test_check_optiscaler_update(self):
        self._fake_optiscaler_staging()
        real_latest = pcc.optiscaler_latest
        try:
            pcc.optiscaler_latest = lambda nightly=False: {
                "version": "v1.2.3", "url": "x", "asset_name": "x"}
            self.assertFalse(pcc.check_optiscaler_update())
            pcc.optiscaler_latest = lambda nightly=False: {
                "version": "v9.9.9", "url": "x", "asset_name": "x"}
            self.assertTrue(pcc.check_optiscaler_update())
        finally:
            pcc.optiscaler_latest = real_latest

    def test_remove_optiscaler_cleans_up_all_deployed_companions(self):
        """Regression: live-testing against a real OptiScaler release found
        loose companion DLLs (libxess.dll, amd_fidelityfx_*.dll, etc - a
        full real release ships ~10 of these, not just the 2 RHI happens to
        hardcode a constant for) and a companion subdirectory
        (D3D12_Optiscaler) left behind after Remove, because the old code
        only ever cleaned up a short hardcoded allowlist. install_optiscaler
        must now track every file/subdir it actually deploys, and
        remove_optiscaler must delete exactly that recorded footprint."""
        d, exe = self._fake_game_exe()
        staging = self._fake_optiscaler_staging()
        (staging / "libxess.dll").write_bytes(b"xess")
        (staging / "amd_fidelityfx_dx12.dll").write_bytes(b"ffx")
        (staging / "D3D12_Optiscaler").mkdir()
        (staging / "D3D12_Optiscaler" / "extra.dll").write_bytes(b"extra")
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        self.assertTrue((d / "libxess.dll").is_file())
        self.assertTrue((d / "amd_fidelityfx_dx12.dll").is_file())
        self.assertTrue((d / "D3D12_Optiscaler" / "extra.dll").is_file())

        pcc.remove_optiscaler("12345")
        self.assertFalse((d / "libxess.dll").exists())
        self.assertFalse((d / "amd_fidelityfx_dx12.dll").exists())
        self.assertFalse((d / "D3D12_Optiscaler").exists())

    def test_update_optiscaler_removes_companion_dropped_by_new_release(self):
        d, exe = self._fake_game_exe()
        staging = self._fake_optiscaler_staging(version="v1.0.0")
        (staging / "old_companion.dll").write_bytes(b"old")
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        self.assertTrue((d / "old_companion.dll").is_file())

        # Simulate a newer release that no longer ships old_companion.dll.
        (staging / "old_companion.dll").unlink()
        (staging / "version.txt").write_text("v2.0.0")
        real_ensure = pcc.ensure_optiscaler_staging
        pcc.ensure_optiscaler_staging = (
            lambda nightly=False, task_id=None: pcc._optiscaler_staging_dir(nightly))
        try:
            r = pcc.update_optiscaler("12345")
        finally:
            pcc.ensure_optiscaler_staging = real_ensure
        self.assertEqual(r["version"], "v2.0.0")
        self.assertFalse((d / "old_companion.dll").exists())

    def test_install_optiscaler_full_flow_and_remove(self):
        d, exe = self._fake_game_exe()
        self._fake_optiscaler_staging()
        r = pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        self.assertTrue(r["installed"])
        self.assertEqual(r["installed_as"], "dxgi.dll")
        target = d / "dxgi.dll"
        self.assertTrue(target.is_file())
        ini_text = (d / "OptiScaler.ini").read_text()
        self.assertIn("LoadReshade=true", ini_text)
        self.assertIn("LoadAsiPlugins=true", ini_text)

        status = pcc.scan_game_optiscaler("12345", str(d), exe_path=str(exe))
        self.assertTrue(status["installed"])
        self.assertEqual(status["installed_as"], "dxgi.dll")

        rm = pcc.remove_optiscaler("12345")
        self.assertTrue(rm["removed"])
        self.assertFalse(target.exists())
        self.assertFalse((d / "OptiScaler.ini").exists())
        with self.assertRaises(RuntimeError):
            pcc.remove_optiscaler("12345")

    def _fake_pd_upscaler_zip(self, artifact, dll_content):
        import io, zipfile
        inner_buf = io.BytesIO()
        with zipfile.ZipFile(inner_buf, "w") as z:
            z.writestr("dinput8.dll", dll_content)
        outer_buf = io.BytesIO()
        with zipfile.ZipFile(outer_buf, "w") as z:
            z.writestr(f"{artifact}.zip", inner_buf.getvalue())
        return outer_buf.getvalue()

    def test_install_optiscaler_swaps_pd_upscaler_re_framework(self):
        d, exe = self._fake_game_exe()
        (d / "re_chunk_000.pak").write_bytes(b"x")   # RE Engine marker
        (d / "dinput8.dll").write_bytes(b"standard REFramework build")
        state = pcc.load_state()
        state.setdefault("rhi_reframework_installs", {})["12345"] = {
            "path": str(d / "dinput8.dll"), "version": "nightly-1111",
        }
        pcc.save_state(state)
        self._fake_optiscaler_staging()
        real_manifest, real_bytes = pcc.rhi_manifest, pcc._gh_bytes
        pcc.rhi_manifest = lambda: {"pdUpscalerGames": {"Test Game": "RE2"}}
        pcc._gh_bytes = lambda url, task=None: self._fake_pd_upscaler_zip("RE2", b"pd-upscaler build")
        try:
            r = pcc.install_optiscaler("12345", str(d), exe_override=str(exe),
                                       gpu_type="NVIDIA", game_name="Test Game")
        finally:
            pcc.rhi_manifest, pcc._gh_bytes = real_manifest, real_bytes
        self.assertTrue(r["pd_upscaler_installed"])
        self.assertEqual((d / "dinput8.dll").read_bytes(), b"pd-upscaler build")
        backup = d / "dinput8.dll.rhi_standard_backup"
        self.assertEqual(backup.read_bytes(), b"standard REFramework build")
        ref_rec = pcc.load_state()["rhi_reframework_installs"]["12345"]
        self.assertEqual(ref_rec["version"], "PD-Upscaler")

        # removing OptiScaler restores the standard build
        real_latest = pcc.re_framework_latest
        pcc.re_framework_latest = lambda: {"version": "nightly-2222", "url": "http://x"}
        try:
            pcc.remove_optiscaler("12345")
        finally:
            pcc.re_framework_latest = real_latest
        self.assertEqual((d / "dinput8.dll").read_bytes(), b"standard REFramework build")
        self.assertFalse(backup.exists())
        ref_rec2 = pcc.load_state()["rhi_reframework_installs"]["12345"]
        self.assertEqual(ref_rec2["version"], "nightly-2222")

    def test_install_optiscaler_skips_pd_upscaler_when_no_standard_reframework(self):
        d, exe = self._fake_game_exe()
        self._fake_optiscaler_staging()
        real_manifest = pcc.rhi_manifest
        pcc.rhi_manifest = lambda: {"pdUpscalerGames": {"Test Game": "RE2"}}
        try:
            r = pcc.install_optiscaler("12345", str(d), exe_override=str(exe),
                                       gpu_type="NVIDIA", game_name="Test Game")
        finally:
            pcc.rhi_manifest = real_manifest
        self.assertFalse(r["pd_upscaler_installed"])
        self.assertNotIn("12345", pcc.load_state().get("rhi_reframework_installs", {}))

    def test_install_optiscaler_skips_pd_upscaler_for_unlisted_game(self):
        d, exe = self._fake_game_exe()
        (d / "dinput8.dll").write_bytes(b"standard build")
        self._fake_optiscaler_staging()
        real_manifest = pcc.rhi_manifest
        pcc.rhi_manifest = lambda: {"pdUpscalerGames": {"Resident Evil 2": "RE2"}}
        try:
            r = pcc.install_optiscaler("12345", str(d), exe_override=str(exe),
                                       gpu_type="NVIDIA", game_name="Some Other Game")
        finally:
            pcc.rhi_manifest = real_manifest
        self.assertFalse(r["pd_upscaler_installed"])
        self.assertEqual((d / "dinput8.dll").read_bytes(), b"standard build")

    def test_install_optiscaler_backs_up_game_original_dxgi(self):
        d, exe = self._fake_game_exe()
        (d / "dxgi.dll").write_bytes(b"totally unrelated game-owned dxgi")
        self._fake_optiscaler_staging()
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        backup = d / "dxgi.dll.original"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), b"totally unrelated game-owned dxgi")
        pcc.remove_optiscaler("12345")
        self.assertEqual((d / "dxgi.dll").read_bytes(), b"totally unrelated game-owned dxgi")

    def test_install_optiscaler_vulkan_uses_winmm(self):
        d, exe = self._fake_game_exe(dlls=["vulkan-1.dll"])
        self._fake_optiscaler_staging()
        r = pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        self.assertEqual(r["installed_as"], "winmm.dll")
        self.assertTrue((d / "winmm.dll").is_file())

    def test_install_optiscaler_renames_conflicting_reshade(self):
        d, exe = self._fake_game_exe()
        (d / "dxgi.dll").write_bytes(b"R" * 100)  # stands in for an installed ReShade
        state = pcc.load_state()
        state.setdefault("rhi_reshade_installs", {})["12345"] = {
            "path": str(d / "dxgi.dll"), "channel": "stable", "version": "6.8.0",
            "bitness": 64, "exe": str(exe),
        }
        pcc.save_state(state)
        self._fake_optiscaler_staging()
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")

        self.assertTrue((d / "ReShade64.dll").is_file())
        self.assertEqual((d / "ReShade64.dll").read_bytes(), b"R" * 100)
        rs_rec = pcc.load_state()["rhi_reshade_installs"]["12345"]
        self.assertEqual(rs_rec["path"], str(d / "ReShade64.dll"))
        self.assertIn(b"OptiScaler", (d / "dxgi.dll").read_bytes())

        pcc.remove_optiscaler("12345")
        # ReShade reclaims dxgi.dll (its detected-API default) once OptiScaler is gone
        self.assertTrue((d / "dxgi.dll").is_file())
        self.assertEqual((d / "dxgi.dll").read_bytes(), b"R" * 100)
        self.assertFalse((d / "ReShade64.dll").exists())

    def test_install_optiscaler_ignores_reshade_in_a_different_directory(self):
        """Regression: live-testing surfaced a real bug where a wrong
        auto-detected exe (the same largest-.exe heuristic issue that hit
        the ReShade port) put OptiScaler's target directory somewhere other
        than where ReShade was actually installed. The old coexistence
        check matched on filename alone and did a bare Path.rename(), which
        silently DRAGGED ReShade's dxgi.dll across directories into
        OptiScaler's (wrong) folder - the fix requires the two paths' own
        parent directories to match before treating it as a real conflict."""
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bin/x64").mkdir(parents=True)
        real_exe = d / "bin/x64/Game.exe"
        real_exe.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        wrong_exe = d / "launcher.exe"
        wrong_exe.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        (d / "bin/x64/dxgi.dll").write_bytes(b"R" * 100)  # ReShade, correctly nested
        state = pcc.load_state()
        state.setdefault("rhi_reshade_installs", {})["12345"] = {
            "path": str(d / "bin/x64/dxgi.dll"), "channel": "stable", "version": "6.8.0",
            "bitness": 64, "exe": str(real_exe),
        }
        pcc.save_state(state)
        self._fake_optiscaler_staging()
        # Installed against the WRONG exe (game root, not bin/x64/) - simulates
        # the auto-detect heuristic picking a launcher stub.
        pcc.install_optiscaler("12345", str(d), exe_override=str(wrong_exe), gpu_type="NVIDIA")

        # ReShade must be left exactly where it was - not renamed, not moved.
        self.assertEqual((d / "bin/x64/dxgi.dll").read_bytes(), b"R" * 100)
        self.assertFalse((d / "bin/x64/ReShade64.dll").exists())
        self.assertFalse((d / "ReShade64.dll").exists())
        rs_rec = pcc.load_state()["rhi_reshade_installs"]["12345"]
        self.assertEqual(rs_rec["path"], str(d / "bin/x64/dxgi.dll"))
        # OptiScaler deployed at the (wrong) root, independent of ReShade.
        self.assertIn(b"OptiScaler", (d / "dxgi.dll").read_bytes())

    def test_seed_optiscaler_user_inis_never_overwrites(self):
        pcc.seed_optiscaler_user_inis()
        p = pcc.get_optiscaler_user_ini_path("NVIDIA", True, False)
        p.write_text("EDITED_BY_USER=true\n")
        pcc.seed_optiscaler_user_inis()
        self.assertEqual(p.read_text(), "EDITED_BY_USER=true\n")

    def test_optiscaler_hotkey_writes_to_all_templates_and_games(self):
        pcc.seed_optiscaler_user_inis()
        pcc.set_optiscaler_hotkey("F5")
        for gpu_type, dlss_inputs, nightly in pcc._OPTISCALER_INI_CONFIGS:
            p = pcc.get_optiscaler_user_ini_path(gpu_type, dlss_inputs, nightly)
            self.assertIn("ShortcutKey=0x74", p.read_text())
        self.assertEqual(pcc.load_state()["rhi_optiscaler_hotkey"], "F5")

        d, exe = self._fake_game_exe()
        self._fake_optiscaler_staging()
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        self.assertIn("ShortcutKey=0x74", (d / "OptiScaler.ini").read_text())

        updated = pcc.apply_optiscaler_hotkey_to_all_games("F1")
        self.assertEqual(updated, 1)
        self.assertIn("ShortcutKey=0x70", (d / "OptiScaler.ini").read_text())

    def test_set_optiscaler_fg_writes_frame_gen_section(self):
        d, exe = self._fake_game_exe()
        self._fake_optiscaler_staging()
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        r = pcc.set_optiscaler_fg("12345", "fsrfg", "xefg")
        self.assertTrue(r["applied"])
        ini_text = (d / "OptiScaler.ini").read_text()
        self.assertIn("FGInput=fsrfg", ini_text)
        self.assertIn("FGOutput=xefg", ini_text)

        with self.assertRaises(RuntimeError):
            pcc.set_optiscaler_fg("99999", "auto", "auto")

    def test_update_optiscaler_preserves_user_ini_changes(self):
        d, exe = self._fake_game_exe()
        self._fake_optiscaler_staging()
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        pcc.set_optiscaler_ini_value(str(d), "FrameGen", "FGInput", "fsrfg")

        (pcc._optiscaler_staging_dir() / "version.txt").write_text("v2.0.0")
        real_ensure = pcc.ensure_optiscaler_staging
        pcc.ensure_optiscaler_staging = (
            lambda nightly=False, task_id=None: pcc._optiscaler_staging_dir(nightly))
        try:
            r = pcc.update_optiscaler("12345")
        finally:
            pcc.ensure_optiscaler_staging = real_ensure
        self.assertEqual(r["version"], "v2.0.0")
        ini_text = (d / "OptiScaler.ini").read_text()
        self.assertIn("FGInput=fsrfg", ini_text)  # user's change preserved across the update

    def test_optipatcher_latest_parses_build_commit(self):
        real_gh_json = pcc._gh_json
        pcc._gh_json = lambda url: {
            "body": "Rolling build\n**Build commit:** abc1234\n",
            "assets": [{"name": "OptiPatcher.asi", "browser_download_url": "http://x/p.asi"}],
        }
        try:
            info = pcc.optipatcher_latest()
        finally:
            pcc._gh_json = real_gh_json
        self.assertEqual(info["version"], "abc1234")
        self.assertEqual(info["url"], "http://x/p.asi")

    def test_install_optiscaler_deploys_optipatcher_for_amd(self):
        d, exe = self._fake_game_exe()
        self._fake_optiscaler_staging()
        real_optipatcher = pcc.ensure_optipatcher_staged
        pcc.ensure_optipatcher_staged = lambda task_id=None: self._fake_optipatcher_asi()
        try:
            pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="AMD",
                                   dlss_inputs=False)
        finally:
            pcc.ensure_optipatcher_staged = real_optipatcher
        self.assertTrue((d / "plugins" / "OptiPatcher.asi").is_file())

    # ---- RHI: OptiScaler Custom variant ----
    def _fake_custom_optiscaler_build(self, name="DLSSNR-v0.1.0", extra_companion=True):
        """Drops a fake user-provided build into OPTISCALER_CUSTOM_DIR, same
        shape as a real extracted release (OptiScaler.dll + .ini directly
        inside, no version.txt - Custom builds are user-managed, the folder
        name itself is the version label)."""
        b = pcc.OPTISCALER_CUSTOM_DIR / name
        b.mkdir(parents=True, exist_ok=True)
        (b / "OptiScaler.dll").write_bytes(b"OptiScaler fake custom build" + b"\x00" * 500)
        (b / "OptiScaler.ini").write_text("[Upscalers]\nSpoofing=auto\n")
        if extra_companion:
            (b / "nvngx.dll_dlssnr.dll").write_bytes(b"fake dlssnr companion")
        return b

    def test_list_custom_optiscaler_builds_only_lists_real_builds(self):
        self._fake_custom_optiscaler_build("DLSSNR-v0.1.0")
        (pcc.OPTISCALER_CUSTOM_DIR / "not-a-build").mkdir(parents=True)
        self.assertEqual(pcc.list_custom_optiscaler_builds(), ["DLSSNR-v0.1.0"])

    def test_install_optiscaler_custom_variant_deploys_own_files_and_ini(self):
        d, exe = self._fake_game_exe()
        self._fake_custom_optiscaler_build("DLSSNR-v0.1.0")
        r = pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA",
                                   variant="custom", custom_build="DLSSNR-v0.1.0")
        self.assertTrue(r["installed"])
        self.assertEqual(r["version"], "DLSSNR-v0.1.0")
        self.assertTrue((d / "dxgi.dll").is_file())
        self.assertIn(b"OptiScaler", (d / "dxgi.dll").read_bytes())
        # The build's own companion DLL (not one of PCC's hardcoded names)
        # deploys like any other loose file in the source folder.
        self.assertTrue((d / "nvngx.dll_dlssnr.dll").is_file())
        ini_text = (d / "OptiScaler.ini").read_text()
        self.assertIn("[Upscalers]", ini_text)          # seeded from the build's own ini
        self.assertIn("LoadReshade=true", ini_text)      # still enforced same as any variant

        status = pcc.scan_game_optiscaler("12345", str(d), exe_path=str(exe))
        self.assertEqual(status["variant"], "custom")
        self.assertEqual(status["custom_build"], "DLSSNR-v0.1.0")
        self.assertFalse(status["update_available"])     # no network check for Custom

        rm = pcc.remove_optiscaler("12345")
        self.assertTrue(rm["removed"])
        self.assertFalse((d / "dxgi.dll").exists())
        self.assertFalse((d / "nvngx.dll_dlssnr.dll").exists())

    def test_install_optiscaler_custom_variant_defaults_to_first_available_build(self):
        d, exe = self._fake_game_exe()
        self._fake_custom_optiscaler_build("OnlyBuild")
        r = pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA",
                                   variant="custom")
        self.assertEqual(r["version"], "OnlyBuild")

    def test_install_optiscaler_custom_variant_without_any_build_raises(self):
        d, exe = self._fake_game_exe()
        with self.assertRaises(RuntimeError):
            pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA",
                                   variant="custom")

    def test_check_custom_optiscaler_updates_first_run_only_establishes_baseline(self):
        self._fake_custom_optiscaler_build("DLSSNR-v0.1.0")
        r = pcc.check_custom_optiscaler_updates()
        self.assertEqual(r, {"changed": [], "redeployed": 0})

    def test_check_custom_optiscaler_updates_redeploys_changed_build(self):
        d, exe = self._fake_game_exe()
        self._fake_custom_optiscaler_build("DLSSNR-v0.1.0")
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA",
                               variant="custom", custom_build="DLSSNR-v0.1.0")
        pcc.check_custom_optiscaler_updates()   # establish baseline
        # User drops a newer build over the same folder name.
        (pcc.OPTISCALER_CUSTOM_DIR / "DLSSNR-v0.1.0" / "OptiScaler.dll").write_bytes(
            b"OptiScaler fake custom build v2" + b"\x00" * 500)
        r = pcc.check_custom_optiscaler_updates()
        self.assertEqual(r["changed"], ["DLSSNR-v0.1.0"])
        self.assertEqual(r["redeployed"], 1)
        self.assertEqual((d / "dxgi.dll").read_bytes(),
                         (pcc.OPTISCALER_CUSTOM_DIR / "DLSSNR-v0.1.0" / "OptiScaler.dll").read_bytes())

    def test_ensure_dlss_dll_cached_downloads_from_manifest(self):
        import zipfile, io
        manifest = {"dlss": [{"version": "310.7.129.0", "is_dev_file": False,
                              "download_url": "http://x/dlss.zip"}]}
        real_manifest, real_bytes = pcc._dlss_manifest, pcc._gh_bytes
        pcc._dlss_manifest = lambda: manifest
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("nvngx_dlss.dll", b"fake dlss dll")
        pcc._gh_bytes = lambda url, task=None: buf.getvalue()
        try:
            p = pcc.ensure_dlss_dll_cached("dlss")
        finally:
            pcc._dlss_manifest = real_manifest
            pcc._gh_bytes = real_bytes
        self.assertTrue(p.is_file())
        self.assertEqual(p.read_bytes(), b"fake dlss dll")
        self.assertEqual(pcc.get_staged_dlss_dll("dlss"), p)

    # ---- RHI: DXVK ----
    def _fake_dxvk_staging(self, variant="stable", version=None):
        d = pcc._dxvk_staging_dir(variant)
        (d / "x64").mkdir(parents=True, exist_ok=True)
        for name in ("d3d9.dll", "d3d10core.dll", "d3d11.dll", "dxgi.dll"):
            (d / "x64" / name).write_bytes(b"DXVK_ fake build" + b"\x00" * 200)
        version = version or "v1.0.0"
        (d / "version.txt").write_text(version)
        real_latest = pcc.dxvk_latest
        pcc.dxvk_latest = (lambda variant=variant, _v=version: {
            "version": _v, "url": "http://x", "asset_name": "dxvk.zip"})
        self.addCleanup(lambda: setattr(pcc, "dxvk_latest", real_latest))
        return d

    def test_dxvk_required_dlls_and_unsupported_api(self):
        self.assertEqual(pcc._dxvk_required_dlls("d3d9"), ["d3d9.dll"])
        self.assertEqual(pcc._dxvk_required_dlls("d3d10"), ["d3d10core.dll", "dxgi.dll"])
        self.assertEqual(pcc._dxvk_required_dlls("d3d11"), ["d3d11.dll", "dxgi.dll"])
        with self.assertRaises(RuntimeError):
            pcc._dxvk_required_dlls("d3d12")
        with self.assertRaises(RuntimeError):
            pcc._dxvk_required_dlls("vulkan")
        with self.assertRaises(RuntimeError):
            pcc._dxvk_required_dlls(None)

    def test_get_dxvk_lilium_conf_picks_preset_set_by_api(self):
        self.assertEqual(pcc.get_dxvk_lilium_conf("d3d9", 0), pcc.DXVK_LILIUM_D3D9_PRESETS[0][1])
        self.assertEqual(pcc.get_dxvk_lilium_conf("d3d11", 0), pcc.DXVK_LILIUM_D3D11_PRESETS[0][1])
        self.assertEqual(len(pcc.DXVK_LILIUM_D3D9_PRESETS), 6)
        self.assertEqual(len(pcc.DXVK_LILIUM_D3D11_PRESETS), 7)
        self.assertEqual(pcc.get_dxvk_lilium_conf("d3d11", 99), pcc.DXVK_LILIUM_D3D11_PRESETS[0][1])

    def test_check_dxvk_update(self):
        self._fake_dxvk_staging(version="v1.0.0")
        real_latest = pcc.dxvk_latest
        try:
            pcc.dxvk_latest = lambda variant: {"version": "v1.0.0", "url": "x", "asset_name": "x"}
            self.assertFalse(pcc.check_dxvk_update("stable"))
            pcc.dxvk_latest = lambda variant: {"version": "v9.9.9", "url": "x", "asset_name": "x"}
            self.assertTrue(pcc.check_dxvk_update("stable"))
        finally:
            pcc.dxvk_latest = real_latest

    def test_ensure_dxvk_staging_stable_tar_gz(self):
        import tarfile
        src = Path(self.tmp.name) / "dxvk_src"
        (src / "x64").mkdir(parents=True)
        (src / "x64" / "d3d9.dll").write_bytes(b"DXVK_ fake" + b"\x00" * 200)
        archive = Path(self.tmp.name) / "dxvk-3.1.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(src, arcname="dxvk-3.1")
        data = archive.read_bytes()

        real_latest, real_bytes = pcc.dxvk_latest, pcc._gh_bytes
        pcc.dxvk_latest = lambda variant: {"version": "v3.1", "url": "http://x",
                                           "asset_name": "dxvk-3.1.tar.gz"}
        pcc._gh_bytes = lambda url, task=None: data
        try:
            staging_dir = pcc.ensure_dxvk_staging("stable")
        finally:
            pcc.dxvk_latest = real_latest
            pcc._gh_bytes = real_bytes
        self.assertTrue((staging_dir / "x64" / "d3d9.dll").is_file())
        self.assertEqual(pcc.dxvk_staging_version("stable"), "v3.1")

    def test_ensure_dxvk_staging_development_zip(self):
        import zipfile, io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("x64/d3d9.dll", b"DXVK_ fake" + b"\x00" * 200)
        data = buf.getvalue()

        real_latest, real_bytes = pcc.dxvk_latest, pcc._gh_bytes
        pcc.dxvk_latest = lambda variant: {"version": "abc1234", "url": "http://x",
                                           "asset_name": "dxvk-master-abc1234.zip"}
        pcc._gh_bytes = lambda url, task=None: data
        try:
            staging_dir = pcc.ensure_dxvk_staging("development")
        finally:
            pcc.dxvk_latest = real_latest
            pcc._gh_bytes = real_bytes
        self.assertTrue((staging_dir / "x64" / "d3d9.dll").is_file())
        self.assertEqual(pcc.dxvk_staging_version("development"), "abc1234")

    def test_ensure_dxvk_staging_lilium_requires_7z(self):
        real_find, real_latest = pcc._find_7z_binary, pcc.dxvk_latest
        pcc._find_7z_binary = lambda: None
        pcc.dxvk_latest = lambda variant: {"version": "v3.0.2-HDR-mod-v0.3.4", "url": "http://x",
                                           "asset_name": "dxvk_lilium.7z"}
        try:
            with self.assertRaises(RuntimeError) as ctx:
                pcc.ensure_dxvk_staging("lilium")
            self.assertIn("7-Zip", str(ctx.exception))
        finally:
            pcc._find_7z_binary = real_find
            pcc.dxvk_latest = real_latest

    def test_ensure_dxvk_staging_lilium_real_7z_normal_subfolder(self):
        if not pcc._find_7z_binary():
            self.skipTest("7z not installed on this machine")
        import subprocess
        src = Path(self.tmp.name) / "lilium_src" / "normal"
        (src / "x64").mkdir(parents=True)
        (src / "x64" / "d3d9.dll").write_bytes(b"DXVK_ fake lilium" + b"\x00" * 200)
        archive = Path(self.tmp.name) / "dxvk_lilium.7z"
        subprocess.run(["7z", "a", str(archive), str(src.parent) + "/."],
                       check=True, capture_output=True)
        data = archive.read_bytes()

        real_latest, real_bytes = pcc.dxvk_latest, pcc._gh_bytes
        pcc.dxvk_latest = lambda variant: {"version": "v3.0.2-HDR-mod-v0.3.4", "url": "http://x",
                                           "asset_name": "dxvk_lilium.7z"}
        pcc._gh_bytes = lambda url, task=None: data
        try:
            staging_dir = pcc.ensure_dxvk_staging("lilium")
        finally:
            pcc.dxvk_latest = real_latest
            pcc._gh_bytes = real_bytes
        self.assertTrue((staging_dir / "x64" / "d3d9.dll").is_file())

    def test_detect_dxvk_installation(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        (d / "d3d11.dll").write_bytes(b"DXVK_ marker" + b"\x00" * 200)
        self.assertEqual(pcc.detect_dxvk_installation(str(d), "d3d11"), "d3d11.dll")
        self.assertIsNone(pcc.detect_dxvk_installation(str(d / "nope"), "d3d11"))

    def test_install_dxvk_refuses_unsupported_api(self):
        d, exe = self._fake_game_exe(dlls=["vulkan-1.dll"])
        self._fake_dxvk_staging()
        with self.assertRaises(RuntimeError):
            pcc.install_dxvk("12345", str(d), "stable", exe_override=str(exe))

    def test_install_dxvk_full_flow_and_remove(self):
        d, exe = self._fake_game_exe()
        self._fake_dxvk_staging()
        r = pcc.install_dxvk("12345", str(d), "stable", exe_override=str(exe))
        self.assertTrue(r["installed"])
        self.assertEqual(sorted(r["installed_dlls"]), ["d3d11.dll", "dxgi.dll"])
        self.assertTrue((d / "d3d11.dll").is_file())
        self.assertTrue((d / "dxgi.dll").is_file())
        self.assertTrue((d / "dxvk.conf").is_file())
        self.assertIn("dxgi.enableHDR", (d / "dxvk.conf").read_text())

        status = pcc.scan_game_dxvk("12345", str(d), exe_path=str(exe))
        self.assertTrue(status["installed"])

        rm = pcc.remove_dxvk("12345")
        self.assertTrue(rm["removed"])
        self.assertFalse((d / "d3d11.dll").exists())
        self.assertFalse((d / "dxgi.dll").exists())
        self.assertFalse((d / "dxvk.conf").exists())
        with self.assertRaises(RuntimeError):
            pcc.remove_dxvk("12345")

    def test_install_dxvk_backs_up_game_original_dlls(self):
        d, exe = self._fake_game_exe()
        (d / "dxgi.dll").write_bytes(b"totally unrelated game-owned dxgi")
        self._fake_dxvk_staging()
        pcc.install_dxvk("12345", str(d), "stable", exe_override=str(exe))
        backup = d / "dxgi.dll.original"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), b"totally unrelated game-owned dxgi")
        pcc.remove_dxvk("12345")
        self.assertEqual((d / "dxgi.dll").read_bytes(), b"totally unrelated game-owned dxgi")

    def test_install_dxvk_refuses_to_orphan_reshade_without_optiscaler(self):
        # Without OptiScaler present, nothing in the game folder would ever
        # chainload a renamed ReShade64.dll - DXVK must refuse rather than
        # silently break a working ReShade install.
        d, exe = self._fake_game_exe()
        (d / "dxgi.dll").write_bytes(b"R" * 100)
        state = pcc.load_state()
        state.setdefault("rhi_reshade_installs", {})["12345"] = {
            "path": str(d / "dxgi.dll"), "channel": "stable", "version": "6.8.0",
            "bitness": 64, "exe": str(exe),
        }
        pcc.save_state(state)
        self._fake_dxvk_staging()
        with self.assertRaises(RuntimeError):
            pcc.install_dxvk("12345", str(d), "stable", exe_override=str(exe))
        # ReShade untouched, DXVK never deployed
        self.assertTrue((d / "dxgi.dll").is_file())
        self.assertEqual((d / "dxgi.dll").read_bytes(), b"R" * 100)
        self.assertFalse((d / "ReShade64.dll").exists())
        self.assertNotIn("12345", pcc.load_state().get("rhi_dxvk_installs", {}))

    def test_install_dxvk_renames_conflicting_reshade_when_optiscaler_present(self):
        d, exe = self._fake_game_exe()
        (d / "dxgi.dll").write_bytes(b"R" * 100)
        state = pcc.load_state()
        state.setdefault("rhi_reshade_installs", {})["12345"] = {
            "path": str(d / "dxgi.dll"), "channel": "stable", "version": "6.8.0",
            "bitness": 64, "exe": str(exe),
        }
        pcc.save_state(state)
        # Simulate OptiScaler already having chainloaded/renamed ReShade
        # itself and taken over dxgi.dll (the normal ReShade->OptiScaler
        # install order) - here we instead pretend it's tracked but the
        # ReShade record still points at dxgi.dll, to isolate DXVK's own
        # rename-permission check from OptiScaler's.
        (d / "dxgi.dll").write_bytes(b"R" * 100)   # OptiScaler's own dxgi.dll stand-in
        state = pcc.load_state()
        state.setdefault("rhi_optiscaler_installs", {})["12345"] = {
            "install_path": str(d), "installed_as": "dxgi.dll", "variant": "stable",
            "gpu_type": "NVIDIA", "version": "v1.0.0", "exe": str(exe),
        }
        pcc.save_state(state)
        self._fake_dxvk_staging()
        pcc.install_dxvk("12345", str(d), "stable", exe_override=str(exe))

        self.assertTrue((d / "ReShade64.dll").is_file())
        self.assertEqual((d / "ReShade64.dll").read_bytes(), b"R" * 100)
        rs_rec = pcc.load_state()["rhi_reshade_installs"]["12345"]
        self.assertEqual(rs_rec["path"], str(d / "ReShade64.dll"))
        self.assertIn(b"DXVK_", (d / "dxgi.dll").read_bytes())

        pcc.remove_dxvk("12345")
        self.assertTrue((d / "dxgi.dll").is_file())
        self.assertEqual((d / "dxgi.dll").read_bytes(), b"R" * 100)
        self.assertFalse((d / "ReShade64.dll").exists())

    def _seed_rhi_manifest(self, **fields):
        state = pcc.load_state()
        state["rhi_manifest"] = {"ts": pcc.time.time(), "data": fields}
        pcc.save_state(state)

    def test_is_dxvk_blacklisted(self):
        self._seed_rhi_manifest(dxvkBlacklist=["Fortnite", "Apex Legends"])
        self.assertTrue(pcc.is_dxvk_blacklisted("Fortnite"))
        self.assertTrue(pcc.is_dxvk_blacklisted("fortnite"))   # case-insensitive
        self.assertFalse(pcc.is_dxvk_blacklisted("Some Other Game"))
        self.assertFalse(pcc.is_dxvk_blacklisted(None))

    def test_is_dxvk_blacklisted_no_manifest_data_is_not_blacklisted(self):
        self.assertFalse(pcc.is_dxvk_blacklisted("Fortnite"))

    def test_install_dxvk_refuses_blacklisted_game(self):
        d, exe = self._fake_game_exe()
        self._fake_dxvk_staging()
        self._seed_rhi_manifest(dxvkBlacklist=["Fortnite"])
        with self.assertRaises(RuntimeError):
            pcc.install_dxvk("12345", str(d), "stable", exe_override=str(exe),
                             game_name="Fortnite")
        self.assertNotIn("12345", pcc.load_state().get("rhi_dxvk_installs", {}))
        self.assertFalse((d / "dxgi.dll").exists())

    def test_install_dxvk_allows_non_blacklisted_game(self):
        d, exe = self._fake_game_exe()
        self._fake_dxvk_staging()
        self._seed_rhi_manifest(dxvkBlacklist=["Fortnite"])
        pcc.install_dxvk("12345", str(d), "stable", exe_override=str(exe),
                         game_name="Some Other Game")
        self.assertIn("12345", pcc.load_state()["rhi_dxvk_installs"])

    def test_scan_game_dxvk_reports_blacklisted_flag(self):
        d, exe = self._fake_game_exe()
        self._seed_rhi_manifest(dxvkBlacklist=["Fortnite"])
        status = pcc.scan_game_dxvk("12345", str(d), exe_path=str(exe), game_name="Fortnite")
        self.assertTrue(status["blacklisted"])
        status2 = pcc.scan_game_dxvk("12345", str(d), exe_path=str(exe), game_name="Other Game")
        self.assertFalse(status2["blacklisted"])

    def test_install_dxvk_ignores_reshade_in_a_different_directory(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        (d / "bin/x64").mkdir(parents=True)
        real_exe = d / "bin/x64/Game.exe"
        real_exe.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        wrong_exe = d / "launcher.exe"
        wrong_exe.write_bytes(_build_fake_pe64(regular_dlls=["d3d11.dll"]))
        (d / "bin/x64/dxgi.dll").write_bytes(b"R" * 100)
        state = pcc.load_state()
        state.setdefault("rhi_reshade_installs", {})["12345"] = {
            "path": str(d / "bin/x64/dxgi.dll"), "channel": "stable", "version": "6.8.0",
            "bitness": 64, "exe": str(real_exe),
        }
        pcc.save_state(state)
        self._fake_dxvk_staging()
        pcc.install_dxvk("12345", str(d), "stable", exe_override=str(wrong_exe))

        self.assertEqual((d / "bin/x64/dxgi.dll").read_bytes(), b"R" * 100)
        self.assertFalse((d / "bin/x64/ReShade64.dll").exists())
        self.assertFalse((d / "ReShade64.dll").exists())

    def test_install_dxvk_lilium_writes_selected_preset(self):
        d, exe = self._fake_game_exe()
        self._fake_dxvk_staging(variant="lilium", version="v3.0.2-HDR-mod-v0.3.4")
        r = pcc.install_dxvk("12345", str(d), "lilium", exe_override=str(exe), lilium_preset=2)
        self.assertTrue(r["installed"])
        conf = (d / "dxvk.conf").read_text()
        self.assertEqual(conf, pcc.DXVK_LILIUM_D3D11_PRESETS[2][1])
        self.assertIn("scRGB", conf)

    def test_reset_dxvk_conf_restores_default_template(self):
        d, exe = self._fake_game_exe()
        self._fake_dxvk_staging()
        pcc.install_dxvk("12345", str(d), "stable", exe_override=str(exe))
        (d / "dxvk.conf").write_text("dxvk.enableGraphicsPipelineLibrary = False\n# user edit")
        r = pcc.reset_dxvk_conf("12345")
        self.assertTrue(r["reset"])
        self.assertEqual((d / "dxvk.conf").read_text(), pcc.DXVK_DEFAULT_CONF)

    def test_reset_dxvk_conf_restores_lilium_preset(self):
        d, exe = self._fake_game_exe()
        self._fake_dxvk_staging(variant="lilium", version="v3.0.2-HDR-mod-v0.3.4")
        pcc.install_dxvk("12345", str(d), "lilium", exe_override=str(exe), lilium_preset=2)
        (d / "dxvk.conf").write_text("garbage")
        pcc.reset_dxvk_conf("12345")
        self.assertEqual((d / "dxvk.conf").read_text(), pcc.DXVK_LILIUM_D3D11_PRESETS[2][1])

    def test_reset_dxvk_conf_no_install_raises(self):
        with self.assertRaises(RuntimeError):
            pcc.reset_dxvk_conf("99999")

    def test_dxvk_routes_conflicting_dll_to_optiscaler_plugins(self):
        d, exe = self._fake_game_exe()
        state = pcc.load_state()
        state.setdefault("rhi_optiscaler_installs", {})["12345"] = {
            "install_path": str(d), "installed_as": "dxgi.dll", "variant": "stable",
            "gpu_type": "NVIDIA", "dlss_inputs": True, "version": "v0.9.4", "exe": str(exe),
            "deployed_files": [], "deployed_subdirs": [],
        }
        pcc.save_state(state)
        (d / "dxgi.dll").write_bytes(b"OptiScaler fake build" + b"\x00" * 500)
        self._fake_dxvk_staging()
        r = pcc.install_dxvk("12345", str(d), "stable", exe_override=str(exe))
        self.assertEqual(r["installed_dlls"], ["d3d11.dll"])
        self.assertEqual(r["plugin_dlls"], ["dxgi.dll"])
        self.assertTrue((d / "d3d11.dll").is_file())
        self.assertTrue((d / "OptiScaler" / "plugins" / "dxgi.dll").is_file())
        self.assertIn(b"OptiScaler", (d / "dxgi.dll").read_bytes())

        pcc.remove_dxvk("12345")
        self.assertFalse((d / "d3d11.dll").exists())
        self.assertFalse((d / "OptiScaler" / "plugins" / "dxgi.dll").exists())

    def test_optiscaler_moves_conflicting_dxvk_dll_to_plugins(self):
        d, exe = self._fake_game_exe()
        self._fake_dxvk_staging()
        pcc.install_dxvk("12345", str(d), "stable", exe_override=str(exe))
        self.assertTrue((d / "dxgi.dll").is_file())
        self.assertIn(b"DXVK_", (d / "dxgi.dll").read_bytes())

        self._fake_optiscaler_staging()
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        self.assertIn(b"OptiScaler", (d / "dxgi.dll").read_bytes())
        self.assertTrue((d / "OptiScaler" / "plugins" / "dxgi.dll").is_file())
        self.assertIn(b"DXVK_", (d / "OptiScaler" / "plugins" / "dxgi.dll").read_bytes())
        dxvk_rec = pcc.load_state()["rhi_dxvk_installs"]["12345"]
        self.assertNotIn("dxgi.dll", dxvk_rec["installed_dlls"])
        self.assertIn("dxgi.dll", dxvk_rec["plugin_dlls"])
        self.assertIn("d3d11.dll", dxvk_rec["installed_dlls"])

        pcc.remove_optiscaler("12345")
        self.assertTrue((d / "dxgi.dll").is_file())
        self.assertIn(b"DXVK_", (d / "dxgi.dll").read_bytes())
        self.assertFalse((d / "OptiScaler").exists())
        dxvk_rec = pcc.load_state()["rhi_dxvk_installs"]["12345"]
        self.assertIn("dxgi.dll", dxvk_rec["installed_dlls"])
        self.assertEqual(dxvk_rec["plugin_dlls"], [])

    def test_optiscaler_does_not_relocate_stale_dxvk_record(self):
        # If the state record claims a DLL is DXVK's but the actual file on
        # disk isn't (a stale record, or the user manually swapped it), it
        # must NOT get relocated into OptiScaler/plugins/ as if it were.
        d, exe = self._fake_game_exe()
        (d / "dxgi.dll").write_bytes(b"some totally unrelated vendor binary content")
        state = pcc.load_state()
        state.setdefault("rhi_dxvk_installs", {})["12345"] = {
            "install_path": str(d), "variant": "stable", "api": "d3d11", "bitness": 64,
            "installed_dlls": ["dxgi.dll"], "plugin_dlls": [], "backed_up_files": [],
            "exe": str(exe),
        }
        pcc.save_state(state)
        self._fake_optiscaler_staging()
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        self.assertFalse((d / "OptiScaler" / "plugins" / "dxgi.dll").exists())
        dxvk_rec = pcc.load_state()["rhi_dxvk_installs"]["12345"]
        self.assertIn("dxgi.dll", dxvk_rec["installed_dlls"])
        self.assertEqual(dxvk_rec.get("plugin_dlls", []), [])

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

    def test_primary_gpu_vendor_nvidia_only(self):
        pcc._nvidia_gpus = lambda: [{"vendor": "NVIDIA"}]
        pcc._drm_gpus = lambda: []
        self.assertEqual(pcc.primary_gpu_vendor(), "NVIDIA")

    def test_primary_gpu_vendor_amd_only(self):
        pcc._nvidia_gpus = lambda: []
        pcc._drm_gpus = lambda: [{"vendor": "AMD"}]
        self.assertEqual(pcc.primary_gpu_vendor(), "AMD")

    def test_primary_gpu_vendor_hybrid_nvidia_wins(self):
        pcc._nvidia_gpus = lambda: [{"vendor": "NVIDIA"}]
        pcc._drm_gpus = lambda: [{"vendor": "AMD"}]
        self.assertEqual(pcc.primary_gpu_vendor(), "NVIDIA")

    def test_primary_gpu_vendor_intel_only_is_unknown(self):
        pcc._nvidia_gpus = lambda: []
        pcc._drm_gpus = lambda: [{"vendor": "Intel"}]
        self.assertEqual(pcc.primary_gpu_vendor(), "unknown")

    def test_primary_gpu_vendor_no_gpus(self):
        pcc._nvidia_gpus = lambda: []
        pcc._drm_gpus = lambda: []
        self.assertEqual(pcc.primary_gpu_vendor(), "unknown")

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

    def test_rhi_manifest_section_mapping(self):
        self.assertEqual(pcc.RHI_DLSS_MANIFEST_SECTION["sr"], "dlss")
        self.assertEqual(pcc.RHI_DLSS_MANIFEST_SECTION["fg"], "dlssg")
        self.assertEqual(pcc.RHI_DLSS_MANIFEST_SECTION["rr"], "dlssd")

    def test_rhi_manifest_latest_picks_highest_version(self):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("nvngx_dlss.dll", b"MZ" + b"\x00" * 100)
        zip_bytes = buf.getvalue()
        real_manifest, real_bytes = pcc._rhi_dlss_manifest, pcc._gh_bytes
        pcc._rhi_dlss_manifest = lambda: {"dlss": [
            {"version": "310.7.129", "url": "http://x/old.zip"},
            {"version": "310.8.0", "url": "http://x/new.zip"},
        ]}
        pcc._gh_bytes = lambda url, task=None: zip_bytes
        pcc.TASKS["t"] = {"status": "running", "progress": 0, "detail": ""}
        try:
            got = pcc._rhi_manifest_latest("sr", "t")
        finally:
            pcc._rhi_dlss_manifest, pcc._gh_bytes = real_manifest, real_bytes
        self.assertIsNotNone(got)
        version, data = got
        self.assertEqual(version, "310.8.0")
        self.assertTrue(data.startswith(b"MZ"))

    def test_download_dlss_prefers_higher_of_the_two_manifests(self):
        """Regression: the DLSS Swapper community manifest lagged RHI's own
        manifest by a full build (310.7.129 vs 310.8.0) at least once in
        practice. download_dlss() must check both and import whichever is
        actually newer, not always trust one source. Both fakes return a
        real version-bearing PE blob (not just raw bytes) - download_dlss()
        gates on pe_version() succeeding before importing, and a blob that
        fails that gate falls through to the real NVIDIA-repo network path,
        which a unit test must never hit."""
        import struct as _s
        def mk(a, b, c, d):
            data = b"MZ" + b"\x00" * 64 + _s.pack("<I", 0xFEEF04BD)
            data += _s.pack("<I", 0x00010000)
            data += _s.pack("<II", (a << 16) | b, (c << 16) | d) + b"\x00" * 32
            return data
        old_blob = mk(310, 7, 129, 0)
        new_blob = mk(310, 8, 0, 0)

        real_manifest_latest = pcc._manifest_latest
        real_rhi_latest = pcc._rhi_manifest_latest
        real_import = pcc.import_dll
        pcc._manifest_latest = lambda kind, tid: ("310.7.129", old_blob)
        pcc._rhi_manifest_latest = lambda kind, tid: ("310.8.0", new_blob)
        imported = {}
        def fake_import(p):
            imported["bytes"] = Path(p).read_bytes()
            return {"kind": "sr", "version": "310.8.0"}
        pcc.import_dll = fake_import
        try:
            pcc.download_dlss("t2", "sr")
        finally:
            pcc._manifest_latest = real_manifest_latest
            pcc._rhi_manifest_latest = real_rhi_latest
            pcc.import_dll = real_import
        self.assertEqual(pcc.TASKS["t2"]["status"], "done")
        self.assertEqual(imported["bytes"], new_blob)

    def test_streamline_sdk_latest_picks_highest_version(self):
        real_manifest = pcc._rhi_dlss_manifest
        pcc._rhi_dlss_manifest = lambda: {"streamline": [
            {"version": "2.12.129.0", "url": "http://x/old.zip"},
            {"version": "2.13.0.0", "url": "http://x/new.zip"},
        ]}
        try:
            latest = pcc.streamline_sdk_latest()
        finally:
            pcc._rhi_dlss_manifest = real_manifest
        self.assertEqual(latest, {"version": "2.13.0.0", "url": "http://x/new.zip"})

    def test_streamline_sdk_latest_none_without_url(self):
        real_manifest = pcc._rhi_dlss_manifest
        pcc._rhi_dlss_manifest = lambda: {"streamline": [{"version": "2.13.0.0"}]}
        try:
            self.assertIsNone(pcc.streamline_sdk_latest())
        finally:
            pcc._rhi_dlss_manifest = real_manifest

    def test_download_streamline_sdk_extracts_flattened_and_is_idempotent(self):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("streamline/sl.interposer.dll", b"interposer bytes")
            z.writestr("streamline/sl.dlss_nr.dll", b"nr plugin bytes")
        real_manifest, real_bytes = pcc._rhi_dlss_manifest, pcc._gh_bytes
        pcc._rhi_dlss_manifest = lambda: {"streamline": [
            {"version": "2.13.0.0", "url": "http://x/sl.zip"}]}
        pcc._gh_bytes = lambda url, task=None: buf.getvalue()
        try:
            pcc.download_streamline_sdk("t3")
        finally:
            pcc._rhi_dlss_manifest, pcc._gh_bytes = real_manifest, real_bytes
        self.assertEqual(pcc.TASKS["t3"]["status"], "done")
        version_dir = pcc.STREAMLINE_DATA_DIR / "2.13.0.0"
        self.assertEqual((version_dir / "sl.interposer.dll").read_bytes(), b"interposer bytes")
        self.assertEqual((version_dir / "sl.dlss_nr.dll").read_bytes(), b"nr plugin bytes")
        lib = pcc.streamline_sdk_library()
        self.assertEqual(len(lib), 1)
        self.assertEqual(lib[0]["version"], "2.13.0.0")
        self.assertCountEqual(lib[0]["files"], ["sl.interposer.dll", "sl.dlss_nr.dll"])

        # second call is a no-op - no network needed
        pcc._rhi_dlss_manifest = lambda: {"streamline": [
            {"version": "2.13.0.0", "url": "http://x/sl.zip"}]}
        pcc._gh_bytes = lambda url, task=None: (_ for _ in ()).throw(RuntimeError("should not fetch again"))
        try:
            pcc.download_streamline_sdk("t4")
        finally:
            pcc._rhi_dlss_manifest, pcc._gh_bytes = real_manifest, real_bytes
        self.assertEqual(pcc.TASKS["t4"]["status"], "done")
        self.assertIn("already cached", pcc.TASKS["t4"]["detail"])

    def _seed_streamline_version(self, version, files):
        vdir = pcc.STREAMLINE_DATA_DIR / version
        vdir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (vdir / name).write_bytes(content)

    def test_deploy_streamline_to_game_no_cache_raises(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(RuntimeError):
            pcc.deploy_streamline_to_game(str(d))

    def test_deploy_streamline_to_game_copies_dlls_and_scan_reports_it(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        self._seed_streamline_version("2.13.0.0", {
            "sl.interposer.dll": b"interposer", "sl.dlss_g.dll": b"fg plugin",
            "readme.txt": b"not a dll, must be skipped",
        })
        r = pcc.deploy_streamline_to_game(str(d))
        self.assertEqual(r["deployed"], 2)
        self.assertEqual(r["version"], "2.13.0.0")
        dest = d / "OptiScaler" / "Streamline"
        self.assertEqual((dest / "sl.interposer.dll").read_bytes(), b"interposer")
        self.assertEqual((dest / "sl.dlss_g.dll").read_bytes(), b"fg plugin")
        self.assertFalse((dest / "readme.txt").exists())

        status = pcc.scan_streamline_for_game(str(d))
        self.assertTrue(status["deployed"])
        self.assertCountEqual(status["files"], ["sl.interposer.dll", "sl.dlss_g.dll"])

        rm = pcc.remove_streamline_from_game(str(d))
        self.assertTrue(rm["removed"])
        self.assertFalse(dest.exists())
        status2 = pcc.scan_streamline_for_game(str(d))
        self.assertFalse(status2["deployed"])

    def test_deploy_streamline_to_game_picks_requested_version(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        self._seed_streamline_version("2.13.0.0", {"sl.interposer.dll": b"new"})
        self._seed_streamline_version("2.10.0.0", {"sl.interposer.dll": b"old"})
        r = pcc.deploy_streamline_to_game(str(d), version="2.10.0.0")
        self.assertEqual(r["version"], "2.10.0.0")
        self.assertEqual((d / "OptiScaler" / "Streamline" / "sl.interposer.dll").read_bytes(), b"old")

    def test_remove_streamline_from_game_noop_when_not_deployed(self):
        d = self.root / "steamapps/common/TestGame"
        d.mkdir(parents=True, exist_ok=True)
        rm = pcc.remove_streamline_from_game(str(d))
        self.assertFalse(rm["removed"])

    def test_remove_optiscaler_takes_streamline_folder_with_it(self):
        d, exe = self._fake_game_exe()
        self._fake_optiscaler_staging()
        pcc.install_optiscaler("12345", str(d), exe_override=str(exe), gpu_type="NVIDIA")
        self._seed_streamline_version("2.13.0.0", {"sl.interposer.dll": b"x"})
        pcc.deploy_streamline_to_game(str(d))
        self.assertTrue((d / "OptiScaler" / "Streamline" / "sl.interposer.dll").is_file())
        pcc.remove_optiscaler("12345")
        self.assertFalse((d / "OptiScaler").exists())

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

    def test_dlss_scan_climb_refuses_a_directory_home_to_another_game(self):
        """Regression: a game with no DLSS of its own (e.g. an older title)
        would climb straight into a Steam library's shared common/ folder -
        or a custom multi-game root - and misattribute a sibling game's DLLs
        as its own. other_roots must block that climb once it would step
        into a directory another known game's install_path lives under."""
        import struct as _s
        def mk(a, b, c, d):
            data = b"MZ" + b"\x00" * 64 + _s.pack("<I", 0xFEEF04BD)
            data += _s.pack("<I", 0x00010000)
            data += _s.pack("<II", (a << 16) | b, (c << 16) | d) + b"\x00" * 32
            return data
        common = Path(self.tmp.name) / "steamapps" / "common"
        game_a = common / "Alien Isolation"   # has no DLSS of its own
        game_a.mkdir(parents=True)
        game_b = common / "The Witcher 3"     # sibling game that DOES
        game_b.mkdir(parents=True)
        (game_b / "nvngx_dlss.dll").write_bytes(mk(310, 7, 0, 0))

        # without other_roots, the old unguarded climb wanders in
        self.assertEqual(len(pcc.scan_game_dlss(str(game_a))), 1)
        # with other_roots naming the sibling, the climb is refused
        found = pcc.scan_game_dlss(str(game_a), other_roots=[str(game_b)])
        self.assertEqual(found, [])
        # the sibling's own scan is unaffected
        found_b = pcc.scan_game_dlss(str(game_b), other_roots=[str(game_a)])
        self.assertEqual(len(found_b), 1)

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

    # -- DLSS render-preset control (DXVK-NVAPI DRS layer) ------------------

    def test_ngx_render_presets_parsed_from_header(self):
        """SR/RR/FG/NR are separate enums in NVIDIA's own header with
        different ceilings - must not collapse to one shared letter range."""
        self.assertEqual(pcc.NGX_RENDER_PRESETS["sr"],
                          list("abcdefghijklmno") + ["latest"])
        self.assertEqual(pcc.NGX_RENDER_PRESETS["rr"],
                          list("abcdefghijklmno") + ["latest"])
        self.assertEqual(pcc.NGX_RENDER_PRESETS["fg"][:5], list("abcde"))
        self.assertEqual(pcc.NGX_RENDER_PRESETS["fg"][-2:], ["default", "latest"])
        self.assertEqual(len(pcc.NGX_RENDER_PRESETS["fg"]), 28)  # A-Z + default + latest
        self.assertEqual(pcc.NGX_RENDER_PRESETS["nr"],
                          ["a", "b", "c", "d", "latest"])
        # SR and RR happen to share the same letters today but must be
        # independent lists, not one shared object/enum a future header
        # change could accidentally desync.
        self.assertIsNot(pcc.NGX_RENDER_PRESETS["sr"], pcc.NGX_RENDER_PRESETS["rr"])

    def test_preset_symbol_casing_matches_header(self):
        self.assertEqual(pcc._preset_symbol("a"), "A")
        self.assertEqual(pcc._preset_symbol("o"), "O")
        self.assertEqual(pcc._preset_symbol("latest"), "Latest")
        self.assertEqual(pcc._preset_symbol("default"), "Default")

    def test_nvapi_dll_dlss_support_reads_real_build_strings(self):
        """A build's nvapi64.dll is probed for literal setting-name and
        RENDER_PRESET_<X> byte strings - a build whose dxvk-nvapi predates a
        letter genuinely doesn't have that string compiled in, so this must
        correctly narrow the ceiling rather than always returning it whole."""
        tool_dir = Path(self.tmp.name) / "protonbuild"
        dll_dir = tool_dir / "files/lib/wine/nvapi/x86_64-windows"
        dll_dir.mkdir(parents=True)
        # Fake dxvk-nvapi that only knows SR up through preset C (no D..O,
        # no latest) and has never heard of RR presets or the debug var at
        # all - simulates an old build.
        blob = (b"...NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION..."
                b"RENDER_PRESET_A RENDER_PRESET_B RENDER_PRESET_C ...")
        (dll_dir / "nvapi64.dll").write_bytes(blob)
        support = pcc.nvapi_dll_dlss_support(tool_dir)
        self.assertEqual(support["sr"], ["a", "b", "c"])
        self.assertEqual(support["rr"], [])   # setting name itself absent
        self.assertEqual(support["fg"], [])
        self.assertFalse(support["debug_indicator"])

    def test_nvapi_dll_dlss_support_missing_build_fails_empty_not_open(self):
        """No nvapi64.dll at all (build not installed, or too old to ship
        one) - must return the all-empty shape, not raise or fabricate
        support the DLL can't actually prove."""
        support = pcc.nvapi_dll_dlss_support(Path(self.tmp.name) / "nope")
        self.assertEqual(support,
                          {"sr": [], "rr": [], "fg": [], "nr": [],
                           "debug_indicator": False})

    def test_proton_capabilities_includes_dlss_presets_and_ceiling(self):
        root = Path(self.tmp.name) / "capsroot"
        build = root / "compatibilitytools.d/GE-Proton11-1"
        dll_dir = build / "files/lib/wine/nvapi/x86_64-windows"
        dll_dir.mkdir(parents=True)
        build.joinpath("proton").write_text("PROTON_ENABLE_WAYLAND")
        (dll_dir / "nvapi64.dll").write_bytes(
            b"NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION RENDER_PRESET_A "
            b"RENDER_PRESET_Latest DXVK_NVAPI_SET_NGX_DEBUG_OPTIONS")
        real = pcc._compat_dirs
        pcc._compat_dirs = lambda r: [Path(r) / "compatibilitytools.d"]
        try:
            cap = pcc.proton_capabilities(root)
        finally:
            pcc._compat_dirs = real
        self.assertIn("dlss_presets", cap)
        self.assertEqual(cap["dlss_presets"]["GE-Proton11-1"]["sr"],
                          ["a", "latest"])
        self.assertTrue(cap["dlss_presets"]["GE-Proton11-1"]["debug_indicator"])
        self.assertEqual(cap["dlss_preset_ceiling"], pcc.NGX_RENDER_PRESETS)

    def test_dlss_preset_defaults_round_trip(self):
        """The 'global default' is a saved template only - get/set just
        persists it, no game's launch options are touched by these calls."""
        self.assertEqual(pcc.get_dlss_preset_defaults(), {})
        settings = {"srMode": "quality", "srPreset": "render_preset_latest",
                    "rrPreset": "render_preset_f", "drsCompact": True}
        pcc.set_dlss_preset_defaults(settings)
        self.assertEqual(pcc.get_dlss_preset_defaults(), settings)
        # overwrite, not merge
        pcc.set_dlss_preset_defaults({"srMode": "balanced"})
        self.assertEqual(pcc.get_dlss_preset_defaults(), {"srMode": "balanced"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
