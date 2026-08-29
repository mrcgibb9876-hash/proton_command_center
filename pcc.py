#!/usr/bin/env python3
"""
Proton Command Center (PCC)
Per-game launch options and DLSS DLL management for Steam on Linux.
Stdlib only. Run: python3 pcc.py  ->  http://localhost:8686
"""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

STARTED_AT = int(time.time())
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.27.0"
PORT = int(os.environ.get("PCC_PORT", "8686"))
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".local/share/proton-command-center"
DLL_LIBRARY = DATA_DIR / "dlls"        # dlls/<kind>/<version>/<name>.dll
BACKUP_DIR = DATA_DIR / "backups"      # backups/<appid>/<relpath>.pccbak
DATA_DIR.mkdir(parents=True, exist_ok=True)
DLL_LIBRARY.mkdir(parents=True, exist_ok=True)
_DEDUPE_ON_IMPORT = True  # dedupe runs lazily via dll_library()
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

DLSS_KINDS = {
    "nvngx_dlss.dll":   {"kind": "sr",  "label": "DLSS Super Resolution"},
    "nvngx_dlssg.dll":  {"kind": "fg",  "label": "DLSS Frame Generation"},
    "nvngx_dlssd.dll":  {"kind": "rr",  "label": "DLSS Ray Reconstruction"},
    "nvngx_dlssnr.dll": {"kind": "nr",  "label": "DLSS Neural Rendering"},
}
KIND_TO_NAME = {v["kind"]: k for k, v in DLSS_KINDS.items()}

# NVIDIA's official DLSS SR repo ships the DLL in-tree.
NVIDIA_DLSS_REPO_API = "https://api.github.com/repos/NVIDIA/DLSS/contents/lib/Windows_x86_64/rel"

TASKS = {}  # task_id -> {status, progress, detail}
STATE_FILE = DATA_DIR / "state.json"
STATE_LOCK = threading.Lock()
CONFIG_FILE = DATA_DIR / "config.json"
ART_DIR = DATA_DIR / "art"
ART_DIR.mkdir(parents=True, exist_ok=True)
SGDB_API = "https://www.steamgriddb.com/api/v2"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def save_config(cfg) -> None:
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=1))
    tmp.replace(CONFIG_FILE)
    try:
        CONFIG_FILE.chmod(0o600)  # API key lives here
    except OSError:
        pass


def _sgdb_get(path, key):
    req = urllib.request.Request(f"{SGDB_API}{path}", headers={
        "Authorization": f"Bearer {key}", "User-Agent": "proton-command-center"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _valid_image(data) -> bool:
    return bool(data) and (data[:2] == b"\xff\xd8"            # JPEG
                           or data[:8] == b"\x89PNG\r\n\x1a\n"  # PNG
                           or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"))


def _fetch_image(url):
    req = urllib.request.Request(url, headers={"User-Agent": "proton-command-center"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        ct = r.headers.get("Content-Type", "image/png").split(";")[0]
    if not _valid_image(data):
        return None
    if not ct.startswith("image/"):
        ct = "image/png"
    return data, ct


def _clean_game_name(name):
    """'Mortal Shell II - Open Beta™' -> 'Mortal Shell II' for name search."""
    name = re.sub(r"[™®©]", "", name or "")
    name = re.sub(r"\s*[-–—:(\[]?\s*(open beta|closed beta|beta|demo|"
                  r"playtest|early access|technical test)\s*[)\]]?\s*$",
                  "", name, flags=re.I)
    return name.strip()


ART_MISSES = {}  # appid -> timestamp of last failed lookup (avoid re-hammering)


def _sgdb_grids(path_base, key, t):
    """Grids at preferred dimensions first, then any static grid at all - many entries (esp. new/beta games) only have portrait or odd sizes."""
    for suffix in ("?dimensions=460x215,920x430&types=static", "?types=static"):
        try:
            data = _sgdb_get(path_base + suffix, key)
            grids = data.get("data") or []
            if grids:
                res = _fetch_image(grids[0]["url"])
                if res:
                    return res
                t.append(f"{path_base}: grid fetch invalid ({suffix})")
            else:
                t.append(f"{path_base}: no grids ({suffix})")
        except Exception as e:
            t.append(f"{path_base}: {e}")
    return None


def sgdb_art(appid: str, name=None, trace=None):
    """Resolve 460x215 art through a cascade covering beta/demo appids.
    Cached files are validated by magic bytes on every serve - corrupt
    entries from failed fetches self-delete and re-fetch. Misses are
    negative-cached for 10 minutes only."""
    t = trace if trace is not None else []
    for ext, ct in (("jpg", "image/jpeg"), ("png", "image/png"), ("webp", "image/webp")):
        cached = ART_DIR / f"{appid}.{ext}"
        if cached.is_file():
            data = cached.read_bytes()
            if _valid_image(data):
                t.append(f"disk cache hit ({ext})")
                return data, ct
            cached.unlink(missing_ok=True)   # poisoned entry - self-heal
            t.append(f"deleted corrupt cached {ext}")
    if time.time() - ART_MISSES.get(str(appid), 0) < 600:
        t.append("negative-cached (retries in <10 min)")
        return None

    def save(res):
        img, ct = res
        ext = {"image/jpeg": "jpg", "image/png": "png",
               "image/webp": "webp"}.get(ct, "png")
        (ART_DIR / f"{appid}.{ext}").write_bytes(img)
        return img, ct

    # 1. Steam CDN, server-side
    try:
        res = _fetch_image("https://cdn.cloudflare.steamstatic.com"
                           f"/steam/apps/{appid}/header.jpg")
        if res:
            t.append("steam CDN: ok")
            return save(res)
        t.append("steam CDN: invalid image body")
    except Exception as e:
        t.append(f"steam CDN: {e}")

    key = load_config().get("sgdb_api_key", "").strip()
    if not key:
        t.append("no SGDB key set")
    else:
        # 2. SGDB by Steam appid
        res = _sgdb_grids(f"/grids/steam/{appid}", key, t)
        if res:
            t.append("SGDB appid: ok")
            return save(res)
        # 3. SGDB by cleaned name
        clean = _clean_game_name(name)
        if not clean:
            t.append("no name provided for search")
        else:
            try:
                hits = _sgdb_get("/search/autocomplete/"
                                 + urllib.parse.quote(clean), key).get("data") or []
                if not hits:
                    t.append(f"SGDB name search '{clean}': no matches")
                for hit in hits[:3]:
                    res = _sgdb_grids(f"/grids/game/{hit['id']}", key, t)
                    if res:
                        t.append(f"SGDB name search '{clean}' -> "
                                 f"{hit.get('name', hit['id'])}: ok")
                        return save(res)
            except Exception as e:
                t.append(f"SGDB name search: {e}")

    ART_MISSES[str(appid)] = time.time()
    return None


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state) -> None:
    with STATE_LOCK:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(STATE_FILE)


def driver_version():
    try:
        return Path("/sys/module/nvidia/version").read_text().strip()
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0]
    except Exception:
        pass
    return "unknown"


def steam_root() -> Path | None:
    for p in [
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam",
    ]:
        if (p / "steamapps").is_dir():
            return p.resolve()
    return None


def steam_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-x", "steam"], capture_output=True)
        return out.returncode == 0
    except FileNotFoundError:
        return False


def shutdown_steam(timeout=60) -> bool:
    """Ask Steam to exit gracefully and wait until it's gone.
    Graceful matters: Steam flushes localconfig.vdf on clean exit."""
    if not steam_running():
        return True
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH — close Steam manually")
    subprocess.Popen([exe, "-shutdown"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not steam_running():
            time.sleep(2)  # give it a moment to finish flushing files
            return True
        time.sleep(1)
    raise RuntimeError("Steam didn't close within 60s — close it manually and save again")


SESSION_ENV_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY",
                    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                    "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP")


def session_env():
    """Launch env for GUI apps. The backend may have been started without
    DISPLAY/WAYLAND_DISPLAY (systemd, ssh, stale shell) which makes Steam
    fail with display errors - harvest the vars from the user's running
    graphical session processes instead."""
    env = dict(os.environ)
    if env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"):
        return env
    uid = os.getuid()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            p = Path("/proc") / pid
            try:
                if p.stat().st_uid != uid:
                    continue
                raw = (p / "environ").read_bytes()
            except OSError:
                continue
            found = {}
            for chunk in raw.split(b"\x00"):
                try:
                    k, _, v = chunk.decode(errors="ignore").partition("=")
                except Exception:
                    continue
                if k in SESSION_ENV_KEYS and v:
                    found[k] = v
            if found.get("DISPLAY") or found.get("WAYLAND_DISPLAY"):
                env.update(found)
                return env
    except OSError:
        pass
    return env


def _spawn_detached(cmd) -> bool:
    """Launch GUI apps OUTSIDE our service cgroup. Without this, Steam and
    games become children of the backend's systemd unit: the service gets
    charged for their memory, and a service restart kills the game."""
    env = session_env()
    if shutil.which("systemd-run"):
        try:
            subprocess.Popen(["systemd-run", "--user", "--scope", "--collect",
                              "--quiet"] + cmd, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            pass
    subprocess.Popen(cmd, start_new_session=True, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def launch_steam():
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    return _spawn_detached([exe])


def launch_game(appid: str):
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    aid = int(appid)
    # Non-Steam shortcuts (always have bit 31 set - real Steam appids never
    # do, being nowhere near 2**31) need the shifted 64-bit id for rungameid;
    # everywhere else in this app (localconfig/compat-tool keys, grid art)
    # uses the plain unsigned appid unchanged.
    if aid & 0x80000000:
        aid = (aid << 32) | 0x02000000
    return _spawn_detached([exe, f"steam://rungameid/{aid}"])


def library_folders(root: Path):
    """All steamapps dirs across library folders."""
    libs = [root / "steamapps"]
    vdf = root / "steamapps/libraryfolders.vdf"
    if vdf.is_file():
        try:
            data = vdf_parse(vdf.read_text(errors="replace"))
            folders = ci_get(data, "libraryfolders") or {}
            for _, entry in folders.items():
                if isinstance(entry, dict):
                    p = entry.get("path")
                    if p:
                        sp = Path(p) / "steamapps"
                        if sp.is_dir() and sp.resolve() not in [l.resolve() for l in libs]:
                            libs.append(sp)
        except Exception:
            pass
    return libs


SKIP_APPIDS = {
    "228980",   # Steamworks Common Redistributables
    "1070560", "1391110", "1628350", "2180100", "4183110",  # SLR variants
    "1493710", "2348590", "2805730", "3175060", "3658110", "4628710",  # Proton
    "1887720", "961940", "1054830", "1113280", "1245040", "1420170",
    "2456610", "1161040",  # older Proton + EAC runtime
}
SKIP_NAME_RE = re.compile(
    r"^(proton|steam linux runtime|steamworks|pressure vessel|"
    r"steam client|steam sdk|dedicated server)", re.I)


# Steam's StateFlags is a BITFIELD (EAppState), not a single value. Verified
# against real manifests: 4 = fully installed; 1026 = 1024|2 (update started +
# update required); 1062 = 1024|32|4|2 (update started + files missing + fully
# installed + update required, i.e. a repair). Treating it as a plain value
# (flags != 4) made queued/paused/repairing games look "forever downloading".
APP_UPDATE_REQUIRED = 2
APP_FULLY_INSTALLED = 4
APP_FILES_MISSING = 32
APP_UPDATE_RUNNING = 256
APP_UPDATE_PAUSED = 512
APP_UPDATE_STARTED = 1024

_APP_BUSY = (APP_UPDATE_REQUIRED | APP_FILES_MISSING | APP_UPDATE_RUNNING
             | APP_UPDATE_PAUSED | APP_UPDATE_STARTED)


def _is_installing(flags) -> bool:
    """True when Steam has real pending work for this app.

    A game is done only when FullyInstalled is set and no update/repair bit is.
    Flags of 0 (odd/missing manifest) must never mean 'forever downloading'.
    """
    if not flags:
        return False
    if flags & APP_FULLY_INSTALLED and not (flags & _APP_BUSY):
        return False
    return bool(flags & _APP_BUSY)


def list_games(root: Path):
    games = []
    seen = set()
    for lib in library_folders(root):
        for manifest in sorted(lib.glob("appmanifest_*.acf")):
            try:
                data = vdf_parse(manifest.read_text(errors="replace"))
            except Exception:
                continue
            app = ci_get(data, "AppState") or {}
            appid = app.get("appid")
            name = app.get("name")
            installdir = app.get("installdir")
            if not appid or appid in seen:
                continue
            seen.add(appid)
            if appid in SKIP_APPIDS or (name and SKIP_NAME_RE.match(name)):
                continue
            install_path = lib / "common" / (installdir or "")
            flags = int(ci_get(app, "StateFlags") or 0)
            downloaded = int(ci_get(app, "BytesDownloaded") or 0)
            to_download = int(ci_get(app, "BytesToDownload") or 0)
            installing = _is_installing(flags)
            # Steam does NOT reset BytesDownloaded when a download finishes, so
            # the counters go stale. StateFlags is authoritative: if it says
            # done, report done regardless of the bytes (this is what caused
            # "3% forever" while Steam showed the game as installed).
            pct = None
            if installing and to_download > 0:
                pct = round(min(100.0, 100 * downloaded / to_download), 1)
            games.append({
                "appid": appid,
                "name": name or installdir or appid,
                "install_path": str(install_path),
                # A game being downloaded has a manifest before Steam creates
                # the directory, so a pending install must still count as known.
                "installed": install_path.is_dir() or installing,
                "fully_installed": not installing,
                "download_pct": pct,
                "size_bytes": int(ci_get(app, "SizeOnDisk") or 0),
                "library": str(lib),
            })
    games.sort(key=lambda g: g["name"].lower())
    return games


# --------------------------------------------------------------------------
# VDF (text) parse / serialize - round-trip safe for localconfig.vdf
# --------------------------------------------------------------------------

def vdf_parse(text):
    i, n = 0, len(text)

    def skip_ws():
        nonlocal i
        while i < n:
            if text[i] in " \t\r\n":
                i += 1
            elif text.startswith("//", i):
                while i < n and text[i] != "\n":
                    i += 1
            else:
                break

    def read_string():
        nonlocal i
        assert text[i] == '"'
        i += 1
        out = []
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n:
                out.append(text[i:i + 2]); i += 2
            elif c == '"':
                i += 1
                return "".join(out)
            else:
                out.append(c); i += 1
        raise ValueError("unterminated string")

    def read_object():
        nonlocal i
        obj = {}
        while True:
            skip_ws()
            if i >= n:
                return obj
            if text[i] == "}":
                i += 1
                return obj
            if text[i] != '"':
                raise ValueError(f"expected key at byte {i}")
            key = read_string()
            skip_ws()
            if i < n and text[i] == "{":
                i += 1
                obj[key] = read_object()
            elif i < n and text[i] == '"':
                obj[key] = read_string()
            else:
                raise ValueError(f"expected value at byte {i}")

    skip_ws()
    result = {}
    while i < n:
        skip_ws()
        if i >= n:
            break
        key = read_string()
        skip_ws()
        if i < n and text[i] == "{":
            i += 1
            result[key] = read_object()
        else:
            result[key] = read_string()
    return result


def vdf_dump(obj, indent=0):
    pad = "\t" * indent
    out = []
    for k, v in obj.items():
        if isinstance(v, dict):
            out.append(f'{pad}"{k}"\n{pad}{{\n')
            out.append(vdf_dump(v, indent + 1))
            out.append(f"{pad}}}\n")
        else:
            out.append(f'{pad}"{k}"\t\t"{v}"\n')
    return "".join(out)


# --------------------------------------------------------------------------
# Binary VDF (shortcuts.vdf) - a completely different wire format from the
# text VDF above. Verified byte-for-byte against a real shortcuts.vdf on this
# box: parse-then-redump reproduces the original file exactly. That includes
# the trailing run of BIN_END bytes - every object closes with one 0x08,
# *including the implicit top-level object*, so a document with N shortcuts
# ends in a run of BIN_END bytes rather than just closing "shortcuts" once.
# --------------------------------------------------------------------------
BIN_NONE, BIN_STRING, BIN_INT32, BIN_END = 0x00, 0x01, 0x02, 0x08


def binvdf_parse(data: bytes) -> dict:
    """Parse a binary VDF document (shortcuts.vdf) into a dict. Ints round-trip
    as signed 32-bit (shortcuts.vdf's `appid` field is stored signed even
    though it's conceptually an unsigned bitmask - callers mask with
    0xFFFFFFFF when they need the unsigned value Steam uses elsewhere)."""
    def parse_obj(pos):
        d = {}
        while True:
            t = data[pos]; pos += 1
            if t == BIN_END:
                return d, pos
            end = data.index(b"\x00", pos)
            key = data[pos:end].decode("utf-8", "replace")
            pos = end + 1
            if t == BIN_NONE:
                d[key], pos = parse_obj(pos)
            elif t == BIN_STRING:
                end = data.index(b"\x00", pos)
                d[key] = data[pos:end].decode("utf-8", "replace")
                pos = end + 1
            elif t == BIN_INT32:
                d[key] = struct.unpack_from("<i", data, pos)[0]
                pos += 4
            else:
                raise ValueError(f"binvdf: unknown type byte {t:#x} at offset {pos - 1}")
        return d, pos
    root, _ = parse_obj(0)
    return root


def binvdf_dump(d: dict) -> bytes:
    """Inverse of binvdf_parse. Dict insertion order is preserved (Python 3.7+),
    so re-dumping a parsed file without modification reproduces it exactly -
    this is what the round-trip test in tests/test_pcc.py checks."""
    out = bytearray()
    for k, v in d.items():
        if isinstance(v, dict):
            out += bytes([BIN_NONE]) + k.encode("utf-8") + b"\x00" + binvdf_dump(v)
        elif isinstance(v, str):
            out += bytes([BIN_STRING]) + k.encode("utf-8") + b"\x00" + v.encode("utf-8") + b"\x00"
        elif isinstance(v, int):
            out += bytes([BIN_INT32]) + k.encode("utf-8") + b"\x00" + struct.pack("<i", v)
        else:
            raise TypeError(f"binvdf: unsupported value type {type(v)} for key {k!r}")
    out += bytes([BIN_END])
    return bytes(out)


def ci_get(d, key):
    """Case-insensitive dict get."""
    if not isinstance(d, dict):
        return None
    for k in d:
        if k.lower() == key.lower():
            return d[k]
    return None


def ci_ensure(d, key):
    for k in d:
        if k.lower() == key.lower():
            return d[k]
    d[key] = {}
    return d[key]


def find_localconfigs(root: Path):
    """Every user's localconfig.vdf, newest first."""
    userdata = root / "userdata"
    if not userdata.is_dir():
        return []
    configs = list(userdata.glob("*/config/localconfig.vdf"))
    configs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return configs


def get_launch_options(root: Path, appid: str) -> dict:
    data, path, key = _find_shortcut(root, appid)
    if data is not None:
        entry = data["shortcuts"][key]
        return {"value": entry.get("LaunchOptions", ""), "config": str(path)}
    for cfg in find_localconfigs(root):
        try:
            data = vdf_parse(cfg.read_text(errors="replace"))
        except Exception:
            continue
        store = ci_get(data, "UserLocalConfigStore")
        apps = _apps_node(store)
        if apps:
            app = ci_get(apps, str(appid))
            if app:
                lo = ci_get(app, "LaunchOptions")
                if lo is not None:
                    return {"value": vdf_unescape(lo), "config": str(cfg)}
    cfgs = find_localconfigs(root)
    return {"value": "", "config": str(cfgs[0]) if cfgs else None}


def _apps_node(store) -> bool:
    sw = ci_get(store, "Software")
    valve = ci_get(sw, "Valve")
    steam = ci_get(valve, "Steam")
    return ci_get(steam, "apps") or ci_get(steam, "Apps")


def vdf_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def vdf_unescape(s):
    return s.replace('\\"', '"').replace("\\\\", "\\")



def set_game_config(root: Path, appid: str, launch_value=None, compat_tool=None,
                    close_steam=False) -> dict:
    """Single-save: write launch options AND compat tool together, closing
    Steam once for both rather than twice."""
    result = {}
    if close_steam and steam_running():
        shutdown_steam()
        close_steam = False  # already down; downstream calls shouldn't retry
    if launch_value is not None:
        result["launch"] = set_launch_options(root, appid, launch_value,
                                               close_steam=False)
    if compat_tool is not None:
        result["compat"] = set_compat_tool(root, appid, compat_tool,
                                           close_steam=False)
    return {"saved": True, **result}


def set_launch_options(root: Path, appid: str, value, close_steam=False) -> dict:
    if steam_running():
        if close_steam:
            shutdown_steam()
        else:
            raise RuntimeError("Steam is running. Close Steam first — it overwrites its config files on exit.")
    # Non-Steam shortcuts store LaunchOptions on the shortcut entry itself in
    # shortcuts.vdf - Steam never looks at localconfig.vdf's apps.<appid> node
    # for these, so writing there (as below) silently has no effect in-game.
    data, path, key = _find_shortcut(root, appid)
    if data is not None:
        entry = data["shortcuts"][key]
        entry["LaunchOptions"] = value.strip()
        bak = path.with_suffix(f".vdf.pcc-{int(time.time())}.bak")
        shutil.copy2(path, bak)
        tmp = path.with_suffix(".vdf.pcc-tmp")
        tmp.write_bytes(binvdf_dump(data))
        tmp.replace(path)
        return {"saved": True, "backup": str(bak), "config": str(path)}
    configs = find_localconfigs(root)
    if not configs:
        raise RuntimeError("No localconfig.vdf found under userdata/")
    cfg = configs[0]
    data = vdf_parse(cfg.read_text(errors="replace"))
    store = ci_ensure(data, "UserLocalConfigStore")
    sw = ci_ensure(store, "Software")
    valve = ci_ensure(sw, "Valve")
    steam = ci_ensure(valve, "Steam")
    apps = None
    for k in steam:
        if k.lower() == "apps":
            apps = steam[k]
    if apps is None:
        apps = steam.setdefault("apps", {})
    app = ci_ensure(apps, str(appid))
    # remove existing LaunchOptions key regardless of case
    for k in list(app.keys()):
        if k.lower() == "launchoptions":
            del app[k]
    if value.strip():
        app["LaunchOptions"] = vdf_escape(value.strip())
    # timestamped backup, then atomic-ish write
    bak = cfg.with_suffix(f".vdf.pcc-{int(time.time())}.bak")
    shutil.copy2(cfg, bak)
    tmp = cfg.with_suffix(".vdf.pcc-tmp")
    tmp.write_text(vdf_dump(data))
    tmp.replace(cfg)
    return {"saved": True, "backup": str(bak), "config": str(cfg)}


# --------------------------------------------------------------------------
# DLSS DLL handling
# --------------------------------------------------------------------------

def pe_version(path):
    """Read file version from VS_FIXEDFILEINFO without dependencies.

    The 0xFEEF04BD signature can appear coincidentally in a DLL's data before
    the real version resource, yielding garbage like '46863.0.46863.4696'. So
    we scan ALL occurrences and accept only a block whose dwStrucVersion is a
    sane value and whose resulting version looks like a real DLSS version
    (major in a plausible range), preferring the highest valid one."""
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return None
    sig = struct.pack("<I", 0xFEEF04BD)
    best = None
    start = 0
    while True:
        idx = blob.find(sig, start)
        if idx < 0:
            break
        start = idx + 4
        if idx + 16 > len(blob):
            continue
        # dwStrucVersion (right after signature) is normally 0x00010000
        struc = struct.unpack_from("<I", blob, idx + 4)[0]
        if struc not in (0x00010000, 0x00000000, 0x00010001):
            continue
        ms, ls = struct.unpack_from("<II", blob, idx + 8)
        a, b, c, d = ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF
        # DLSS versions: major is small (1,2,3) or the DLSS4 scheme (310+),
        # never five digits. Reject implausible parses.
        if a > 999 or a == 0:
            continue
        cand = (a, b, c, d)
        if best is None or cand > best:
            best = cand
    if best is None:
        return None
    return f"{best[0]}.{best[1]}.{best[2]}.{best[3]}"


def _scan_dlss_tree(base: Path):
    found = []
    if not base.is_dir():
        return found
    # Some games ship a debug copy of the DLSS DLLs in a Development/ or Debug/
    # subfolder. Those are not loaded at runtime, so listing them just creates a
    # confusing duplicate entry. Skip them.
    SKIP_DIRS = {"development", "debug", "profile", "profiling"}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d.lower() not in SKIP_DIRS]
        for fn in filenames:
            if fn.lower() in DLSS_KINDS:
                p = Path(dirpath) / fn
                meta = DLSS_KINDS[fn.lower()]
                ver = pe_version(p)
                found.append({
                    "path": str(p),
                    "name": fn,
                    "kind": meta["kind"],
                    "label": meta["label"],
                    "version": ver,
                    "friendly": friendly_dlss(ver),
                    "size": p.stat().st_size,
                    "backed_up": _backup_path(p).exists(),
                })
    return found


def scan_game_dlss(install_path, other_roots=()):
    """other_roots: every OTHER game's own install_path known to PCC. The
    climb below refuses to step into a directory that's home to one of
    them - without that check it silently wanders into a Steam library's
    shared common/ folder (or a custom multi-game root) after just one
    climb and reports a completely unrelated game's DLLs as this game's
    own. Found the hard way: scanning "Alien: Isolation" (which has no
    DLSS of its own) climbed straight into steamapps/common and reported
    The Witcher 3's nvngx_dlss.dll, and a misconfigured non-Steam shortcut
    climbed into a shared /mnt/data/Games root and reported DLLs from
    three unrelated games."""
    base = Path(install_path)
    found = _scan_dlss_tree(base)
    if found:
        return found
    # Non-Steam shortcuts point install_path at the launch exe's own folder.
    # Steam library games get their real install root, but engine-plugin-based
    # DLSS integrations (Unreal Engine ships DLSS/Streamline under
    # Engine/Plugins/.../ThirdParty/Win64, a sibling of the project's own
    # Binaries/Win64 folder the exe sits in) can live several levels above
    # that. Climb looking for them, stopping at the first hit so this can't
    # wander into an unrelated sibling game's folder higher up the tree.
    other_roots = [Path(p) for p in other_roots]
    for _ in range(4):
        parent = base.parent
        if parent == base or len(parent.parts) <= 2:
            break
        if any(parent == r or parent in r.parents for r in other_roots):
            break
        base = parent
        found = _scan_dlss_tree(base)
        if found:
            return found
    return []


def _backup_path(dll_path):
    p = Path(dll_path)
    h = re.sub(r"[^A-Za-z0-9]", "_", str(p))
    return BACKUP_DIR / f"{h}.pccbak"


# --------------------------------------------------------------------------
# Ultra+ (UE4SS-based mod) detection
# --------------------------------------------------------------------------

def scan_ultraplus(install_path):
    """Ultra+ mods (theultraplace.com) inject via a UE4SS build that hijacks
    dwmapi.dll, sitting next to the game's own ue4ss/ folder. Confirmed
    against theultraplace.com's own mod packages (both the Black Myth: Wukong
    and Stellar Blade "Ultra Plus" zips ship an identical dwmapi.dll straight
    in Binaries/Win64) - Ultra+ Manager places this file itself as part of
    installing the mod, so PCC only ever needs to detect it, never supply it.
    Companion fixes some games require (e.g. NaniteRayTracingFix.asi) are
    separate .asi files loaded from the same directory. None of this fires
    under Proton unless WINEDLLOVERRIDES forces the native dwmapi.dll over
    Proton's own stub, so this only reports what's on disk - the
    launch-option toggle is a separate, explicit step."""
    base = Path(install_path)
    if not base.is_dir():
        return {"installed": False}
    SKIP_DIRS = {"development", "debug", "profile", "profiling"}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d.lower() not in SKIP_DIRS]
        if "ue4ss" not in (d.lower() for d in dirnames):
            continue
        exe_dir = Path(dirpath)
        mods_txt = exe_dir / "ue4ss" / "Mods" / "mods.txt"
        enabled = False
        if mods_txt.is_file():
            for line in mods_txt.read_text(errors="replace").splitlines():
                name, _, flag = line.strip().lstrip("﻿").partition(":")
                if name.strip().lower() == "ultraplusextensions":
                    enabled = flag.strip() == "1"
                    break
        return {
            "installed": True,
            "exe_dir": str(exe_dir),
            "loader_present": (exe_dir / "dwmapi.dll").is_file(),
            "asi_files": sorted(p.name for p in exe_dir.glob("*.asi")),
            "mod_enabled": enabled,
        }
    return {"installed": False}


ULTRAPLUS_GAMEDATA_URL = "https://d25cpafae92g0h.cloudfront.net/gamedata.json"
ULTRAPLUS_MODS_URL = "https://d25cpafae92g0h.cloudfront.net/mods_manifest.json"
ULTRAPLUS_ADDONS_URL = "https://d25cpafae92g0h.cloudfront.net/addons_manifest.json"
# Global (not per-game) polish data for the settings editor: which category
# each setting key displays under, a curated description/name that overrides
# the mod's own parser_friendly_settings.ini where present. Same official
# CloudFront bucket the real Ultra+ Manager reads at startup.
ULTRAPLUS_UI_CATEGORIES_URL = "https://d25cpafae92g0h.cloudfront.net/ui_categories.json"
ULTRAPLUS_DESC_OVERRIDES_URL = "https://d25cpafae92g0h.cloudfront.net/description_overrides.json"
ULTRAPLUS_NAME_OVERRIDES_URL = "https://d25cpafae92g0h.cloudfront.net/name_overrides.json"


def _fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "proton-command-center"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ultraplus_catalog() -> dict:
    """theultraplace.com's live game/mod catalog, reduced to just what PCC
    needs: which games actually have a released Ultra+ mod (gamedata.json
    lists every UE game it *could* support, including ones with no mod yet -
    mods_manifest.json is the ground truth for "released"), and the Steam
    display-name variants to match each one against. Cached 6h, same pattern
    as list_ge_proton()."""
    state = load_state()
    cache = state.get("ultraplus_catalog")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    gamedata = _fetch_json(ULTRAPLUS_GAMEDATA_URL)
    mods = _fetch_json(ULTRAPLUS_MODS_URL)
    supported = gamedata.get("SupportedGames", {})
    search_terms = gamedata.get("GameSearchTerms", {})
    # Mod filenames look like "<key> Ultra Plus vX.Y.Z.zip" but their casing
    # doesn't always match the SupportedGames key (e.g. "Runescape" vs.
    # "RuneScape"), so key everything off the lowercased prefix.
    mod_keys = set()
    for f in mods.get("files", []):
        m = re.match(r"^(.*?)\s+Ultra\s*Plus\s+v", f.get("filename", ""), re.I)
        if m:
            mod_keys.add(m.group(1).strip().lower())
    games = {}
    for key, info in supported.items():
        if key.lower() not in mod_keys:
            continue
        games[key] = {
            "full_name": info.get("full_name") or key,
            "search_terms": [t.lower() for t in search_terms.get(key, [key])],
            "url": info.get("nexus_url") or info.get("wiki_url") or "",
            # Needed to resolve install paths when actually installing a mod
            # (see resolve_executable_path/resolve_unreal_project_path below).
            "exe_path": info.get("exe_path") or "",
            "ue_game_path": info.get("ue_game_path") or "",
            "install_root_only": bool(info.get("install_root_only")),
            "mod_filename_prefixes": info.get("mod_filename_prefixes") or [],
            "addons": info.get("addons") or [],
        }
    data = {"games": games}
    state["ultraplus_catalog"] = {"ts": now, "data": data}
    save_state(state)
    return data


def _norm_game_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def match_ultraplus_catalog(name: str, catalog: dict):
    """Match a Steam game's display name against the Ultra+ catalog. Compares
    alnum-only normalized forms (so spacing/punctuation differences like
    "Stellar Blade" vs. catalog key "StellarBlade" still match) against every
    known name variant for each game - GameSearchTerms, the key itself, and
    full_name. GameSearchTerms is community-curated and occasionally missing
    a variant (confirmed for StellarBlade, which only lists "StellarBlade"
    with no space), so the key/full_name are included as a safety net rather
    than trusting search_terms alone."""
    norm = _norm_game_name(name)
    if not norm:
        return None
    for key, info in catalog.get("games", {}).items():
        candidates = info["search_terms"] + [key, info["full_name"]]
        if norm in (_norm_game_name(c) for c in candidates):
            return key, info
    return None


# --------------------------------------------------------------------------
# Ultra+ mod install (ports of Ultra+ Manager's C# install logic, fixed and
# verified in the linux-parity Avalonia port this same session: zip-slip
# guard, content-aware file routing, UE4SS-signature cleanup, and staged/
# atomic writes so a failed install can never truncate the user's live
# config). PCC installs mods itself now rather than launching the separate
# Ultra+ Manager app - see README's Credits section for attribution.
# --------------------------------------------------------------------------

def _extract_version_from_filename(filename):
    """'<Game> Ultra Plus vX.Y.Z.zip' -> 'X.Y.Z'. Port of
    ModVersionService.ExtractVersionFromFileName."""
    name = filename.rsplit(".", 1)[0] if "." in filename else filename
    idx = name.lower().rfind(" v")
    if idx < 0:
        return "Unknown"
    version = name[idx + 2:]
    version = re.sub(r"[-_]gamepass", "", version, flags=re.I)
    return version.strip()


def list_mod_versions(game_key):
    """Last 5 released Ultra+ mod versions for a game, from the same
    mods_manifest.json used by ultraplus_catalog() but cached far more
    briefly (checked right before an install, not just to render a badge).
    Port of ModVersionService.GetLast5VersionsAsync."""
    state = load_state()
    cache = state.get("ultraplus_mods_manifest")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 300:
        manifest = cache["data"]
    else:
        manifest = _fetch_json(ULTRAPLUS_MODS_URL)
        state["ultraplus_mods_manifest"] = {"ts": now, "data": manifest}
        save_state(state)

    prefix = (game_key + " Ultra").lower()
    versions = []
    for f in manifest.get("files", []):
        filename = f.get("filename", "")
        low = filename.lower()
        if not low.startswith(prefix) or not low.endswith(".zip") or "gamepass" in low:
            continue
        versions.append({
            "filename": filename,
            "url": f.get("url", ""),
            "updated": f.get("updated", ""),
            "size": f.get("size", 0),
            "description": f.get("description", ""),
            "version": _extract_version_from_filename(filename),
        })
    versions.sort(key=lambda v: v["updated"], reverse=True)
    return versions[:5]


def _resolve_case_insensitive_path(root, relative_path):
    """Walks relative_path under root segment-by-segment, falling back to a
    case-insensitive directory listing at each level. Port of
    GamePathResolver.ResolveCaseInsensitivePath."""
    current = Path(root)
    segments = [s for s in relative_path.replace("\\", "/").split("/") if s]
    for i, segment in enumerate(segments):
        is_last = i == len(segments) - 1
        candidate = current / segment
        if candidate.exists():
            current = candidate
            continue
        try:
            entries = list(current.iterdir())
        except OSError:
            return None
        match = next((e for e in entries if e.name.lower() == segment.lower()
                     and (is_last or e.is_dir())), None)
        if match is None:
            return None
        current = match
    return current


def resolve_executable_path(game_key, install_path, catalog):
    """Trusts the catalog's exe_path first (exact, then case-insensitive),
    falls back to the largest-.exe heuristic (_find_game_exe).
    Port of GamePathResolver.ResolveExecutablePath."""
    info = catalog.get("games", {}).get(game_key, {})
    exe_path = info.get("exe_path")
    if exe_path:
        exact = Path(install_path) / exe_path.replace("\\", "/")
        if exact.is_file():
            return exact
        resolved = _resolve_case_insensitive_path(install_path, exe_path)
        if resolved and resolved.is_file():
            return resolved
    return _find_game_exe(install_path)


def resolve_unreal_project_path(game_key, install_path, catalog, executable_path):
    """Port of GamePathResolver.ResolveUnrealProjectPath."""
    info = catalog.get("games", {}).get(game_key, {})
    if info.get("install_root_only"):
        return Path(install_path)
    ue_game_path = info.get("ue_game_path")
    if ue_game_path:
        exact = Path(install_path) / ue_game_path.replace("\\", "/")
        if exact.is_dir():
            return exact
        resolved = _resolve_case_insensitive_path(install_path, ue_game_path)
        if resolved and resolved.is_dir():
            return resolved
    if not executable_path:
        return None
    current = Path(executable_path).parent
    for _ in range(5):
        parent = current.parent
        if current.name.lower().startswith("win") and parent.name.lower() == "binaries":
            return parent.parent
        if current.name.lower() == "binaries":
            return parent
        if parent == current:
            break
        current = parent
    return None


ROOT_INJECTION_FILES = {"dwmapi.dll", "version.dll", "winmm.dll",
                        "xinput1_3.dll", "xinput1_4.dll"}


def _get_safe_segments(archive_path):
    """Rejects rooted/absolute paths and any '..' segment - the zip-slip
    guard. Port of ModArchivePathMapper.GetSafeSegments."""
    if not archive_path:
        return []
    normalized = archive_path.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized:
        raise ValueError(f"Archive entry has an absolute path: {archive_path}")
    normalized = normalized.strip("/")
    segments = [s for s in normalized.split("/") if s and s != "."]
    if any(s == ".." for s in segments):
        raise ValueError(f"Archive entry escapes the installation directory: {archive_path}")
    return segments


def _combine_safely(root_path, segments):
    """Combines root_path with segments and re-verifies the result is still
    under the root - defense in depth against zip-slip even though
    _get_safe_segments already rejects '..'. Port of
    ModArchivePathMapper.CombineSafely."""
    if not root_path:
        raise RuntimeError("Could not resolve destination root.")
    root = Path(root_path).resolve()
    dest = root.joinpath(*segments).resolve() if segments else root
    try:
        dest.relative_to(root)
    except ValueError:
        raise ValueError(f"Archive destination escapes the installation directory: {dest}")
    return dest


def _find_binaries_win_index(segments):
    for i in range(len(segments) - 1):
        if segments[i].lower() != "binaries" or not segments[i + 1].lower().startswith("win"):
            continue
        if i > 0 and segments[i - 1].lower() == "engine":
            continue
        return i
    return -1


def _find_content_paks_index(segments):
    for i in range(len(segments) - 1):
        if segments[i].lower() == "content" and segments[i + 1].lower() == "paks":
            return i
    return -1


def resolve_destination_path(archive_path, install_root, project_path,
                             executable_path, install_root_only):
    """Routes one archive entry to its real on-disk destination:
    Binaries/Win* -> executable directory, Content/Paks -> Unreal project
    directory, root-injection files (ue4ss/ prefix or a known loader DLL
    name) -> executable directory, else the install root. Raises ValueError
    on any path-traversal attempt. Port of
    ModArchivePathMapper.ResolveDestinationPath."""
    segments = _get_safe_segments(archive_path)
    if not segments:
        return None

    if install_root_only:
        return _combine_safely(install_root, segments)

    exe_dir = Path(executable_path).parent if executable_path else None

    idx = _find_binaries_win_index(segments)
    if idx >= 0:
        if not exe_dir:
            raise RuntimeError("Could not resolve the game executable directory.")
        return _combine_safely(exe_dir, segments[idx + 2:])

    idx = _find_content_paks_index(segments)
    if idx >= 0:
        if not project_path:
            raise RuntimeError("Could not resolve the Unreal project directory.")
        return _combine_safely(project_path, segments[idx:])

    if segments[0].lower() == "ue4ss" or (len(segments) == 1 and segments[0].lower() in ROOT_INJECTION_FILES):
        if not exe_dir:
            raise RuntimeError("Could not resolve the game executable directory.")
        return _combine_safely(exe_dir, segments)

    return _combine_safely(install_root, segments)


_ULTRAPLUS_MOD_DIRS = ("ultraplusextensions", "uobjectcachemod")


def should_install_with_user_managed_ue4ss(archive_path):
    """When the user manages their own UE4SS, only Ultra+-owned mod files
    and Content .pak/.ucas/.utoc files still install. Port of
    UltraPlusArchiveFileFilter.ShouldInstallWithUserManagedUE4SS."""
    if not archive_path:
        return False
    low = archive_path.replace("\\", "/").lower()
    for d in _ULTRAPLUS_MOD_DIRS:
        if f"/mods/{d}/" in low or low.endswith(f"/mods/{d}") or low.startswith(f"mods/{d}/"):
            return True
    is_package = low.endswith((".pak", ".ucas", ".utoc"))
    is_content = low.startswith("content/") or "/content/" in low
    return is_package and is_content


def is_signature_path(path):
    """Port of UE4SSSignatureCleanup.IsSignaturePath."""
    if not path:
        return False
    return any(s.lower() == "ue4ss_signatures" for s in re.split(r"[\\/]", str(path)))


def _find_signature_directory(signature_file_path):
    directory = Path(signature_file_path).parent
    while directory != directory.parent:
        if directory.name.lower() == "ue4ss_signatures":
            return directory
        directory = directory.parent
    return None


def synchronize_signature_directories(current_signature_files):
    """Deletes UE4SS_Signatures files left over from a previous mod version
    (anything not in current_signature_files), then removes now-empty
    subdirectories. Port of
    UE4SSSignatureCleanup.SynchronizeSignatureDirectories."""
    if not current_signature_files:
        return 0
    by_directory = {}
    for f in current_signature_files:
        if not is_signature_path(f):
            continue
        full_path = Path(f).resolve()
        sig_dir = _find_signature_directory(full_path)
        if sig_dir is not None:
            by_directory.setdefault(sig_dir, set()).add(full_path)

    deleted = 0
    for sig_dir, expected in by_directory.items():
        if not sig_dir.is_dir():
            continue
        for existing in sig_dir.rglob("*"):
            if existing.is_dir() or existing.resolve() in expected:
                continue
            existing.unlink()
            deleted += 1
        for sub in sorted((p for p in sig_dir.rglob("*") if p.is_dir()),
                          key=lambda p: len(str(p)), reverse=True):
            try:
                next(sub.iterdir())
            except StopIteration:
                sub.rmdir()
    return deleted


_CONFIG_SETTING_PATTERN = re.compile(r"^(?P<indent>[ \t]*)(?P<key>\w+)=(?P<value>.*)$")
_INLINE_COMMENT_MARKER = "; "


def _strip_inline_comment(value):
    idx = value.find(_INLINE_COMMENT_MARKER)
    return value[:idx].rstrip() if idx > 0 else value


def _get_inline_comment(value):
    idx = value.find(_INLINE_COMMENT_MARKER)
    return value[idx:] if idx > 0 else ""


def _read_config_values(content):
    """Reads every assigned value, keyed case-insensitively. Comment lines,
    section headers, and blank lines are skipped (they can't match the
    \\w+ key pattern)."""
    values = {}
    for line in re.split(r"\r\n|\r|\n", content):
        m = _CONFIG_SETTING_PATTERN.match(line.rstrip())
        if m:
            values[m.group("key").lower()] = (m.group("key"), _strip_inline_comment(m.group("value")).strip())
    return values


def _apply_user_config_value(shipped_line, user_values):
    """Rewrites a shipped assignment with the user's value, keeping the
    shipped key text and inline comment. Any non-assignment line, or one
    whose key the user never set, passes through unchanged."""
    m = _CONFIG_SETTING_PATTERN.match(shipped_line.rstrip())
    if not m:
        return shipped_line
    entry = user_values.get(m.group("key").lower())
    if entry is None:
        return shipped_line
    _, user_value = entry
    return f"{m.group('indent')}{m.group('key')}={user_value}{_get_inline_comment(m.group('value'))}"


def merge_config_contents(backup_content, new_config_content):
    """Merges the user's previous config values into the newly-shipped
    config. The shipped file is the structural base (its keys, comments,
    and ordering win); only the values the user actually assigned are
    carried across, matched case-insensitively. Port of the fixed
    ModService.MergeConfigContents (Linux Ultra+ Manager, this session)."""
    if not backup_content:
        return new_config_content or ""
    if not new_config_content:
        return ""
    user_values = _read_config_values(backup_content)
    if not user_values:
        return new_config_content
    lines = re.split(r"\r\n|\r|\n", new_config_content)
    return "\n".join(_apply_user_config_value(line, user_values) for line in lines)


def mod_config_dir(game_key, install_path, catalog):
    """Where UltraPlusConfig.ini/keybinds.ini/preset_*.ini live for an
    installed mod: <executable dir>/ue4ss/Mods/UltraPlusExtensions/scripts/config,
    the same layout install_mod's root-injection routing branch writes them
    to. Returns None if the executable can't be resolved."""
    exe = resolve_executable_path(game_key, install_path, catalog)
    if not exe:
        return None
    return Path(exe).parent / "ue4ss" / "Mods" / "UltraPlusExtensions" / "scripts" / "config"


def list_presets(config_dir):
    """preset_<name>.ini files shipped alongside UltraPlusConfig.ini inside
    the mod's own config dir. Port of PresetService.GetAvailablePresets."""
    d = Path(config_dir)
    if not d.is_dir():
        return []
    return sorted(p.stem[len("preset_"):] for p in d.glob("preset_*.ini"))


def apply_preset(config_dir, preset_name):
    """Rewrites UltraPlusConfig.ini's matching keys from preset_<name>.ini,
    keeping the config file's own structure/comments (same line-rewrite as
    merge_config_contents). Port of PresetService.ApplyPreset, adapted to
    operate directly on the file since PCC has no separate settings model."""
    config_path = Path(config_dir) / "UltraPlusConfig.ini"
    preset_path = Path(config_dir) / f"preset_{preset_name}.ini"
    if not preset_path.is_file():
        raise RuntimeError(f"Preset not found: {preset_name}")
    if not config_path.is_file():
        raise RuntimeError("UltraPlusConfig.ini not found - install the mod first.")
    preset_values = _read_config_values(preset_path.read_text(errors="replace"))
    lines = config_path.read_text(errors="replace").splitlines()
    lines = [_apply_user_config_value(line, preset_values) for line in lines]
    tmp = config_path.with_suffix(".ini.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(config_path)
    return {"applied": preset_name}


def ultraplus_overrides() -> dict:
    """ui_categories.json (display grouping) + description_overrides.json +
    name_overrides.json - global (not per-game) polish data the settings
    editor uses. Cached 6h, same pattern as ultraplus_catalog()."""
    state = load_state()
    cache = state.get("ultraplus_overrides")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    data = {
        "categories": _fetch_json(ULTRAPLUS_UI_CATEGORIES_URL),
        "descriptions": _fetch_json(ULTRAPLUS_DESC_OVERRIDES_URL),
        "names": _fetch_json(ULTRAPLUS_NAME_OVERRIDES_URL),
    }
    state["ultraplus_overrides"] = {"ts": now, "data": data}
    save_state(state)
    return data


def _parse_parser_friendly_settings(content):
    """Parses 'Key.Property=Value' blocks (Comment/Type/Default/Category/
    Values|UserSettings/ValueType/Min/Max/Step/Shortcut) into {key: {prop:
    val}}, lowercasing property names, stripping wrapping quotes off Comment.
    Port of ConfigService.LoadParserFriendlySettings (minus WinUI wiring)."""
    settings = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or "=" not in line or "." not in line.split("=", 1)[0]:
            continue
        left, _, value = line.partition("=")
        key, _, prop = left.partition(".")
        key, prop = key.strip(), prop.strip().lower()
        if not key or not prop:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        settings.setdefault(key, {})[prop] = value
    return settings


def _synthesize_numeric_options(min_v, max_v, step, value_type):
    """Discrete dropdown values by walking Min->Max in Step increments - port
    of NumericRangeConverter (the real app renders numeric settings as a
    dropdown of computed values, not a slider/free-entry box). Capped at
    1000 steps as a sanity bound against a malformed/huge range."""
    try:
        lo, hi, st = float(min_v), float(max_v), float(step)
    except (TypeError, ValueError):
        return []
    if st <= 0 or hi < lo:
        return []
    n = min(int((hi - lo) / st) + 1, 1000)
    out = []
    for i in range(n):
        v = lo + i * st
        if (value_type or "").lower() == "int":
            out.append(str(int(round(v))))
        else:
            out.append(f"{v:.6f}".rstrip("0").rstrip(".") or "0")
    return out


def list_mod_settings(config_dir, overrides) -> list:
    """Combines parser_friendly_settings.ini's schema with UltraPlusConfig
    .ini's current values and the global override JSONs into a flat,
    key-sorted list of editable settings. Port of ConfigService.LoadConfig's
    setting-assembly (minus WinUI control wiring - the real app only ever
    renders enum/numeric dropdowns, no slider/checkbox/textbox)."""
    config_dir = Path(config_dir)
    ini_path = config_dir / "UltraPlusConfig.ini"
    if not ini_path.is_file():
        return []
    schema_path = config_dir / "parser_friendly_settings.ini"
    current = _read_config_values(ini_path.read_text(errors="replace"))
    schema = (_parse_parser_friendly_settings(schema_path.read_text(errors="replace"))
              if schema_path.is_file() else {})
    desc_overrides = overrides.get("descriptions", {})
    name_overrides = overrides.get("names", {})
    key_category = {}
    for cat, keys in overrides.get("categories", {}).items():
        for k in keys:
            key_category.setdefault(k, cat)

    out = []
    for real_key, value in current.values():
        sc = schema.get(real_key, {})
        comment = sc.get("comment", "")
        use_mod_desc = comment.startswith("!")
        if use_mod_desc:
            comment = comment[1:].strip()
        description = comment if use_mod_desc else (desc_overrides.get(real_key) or comment)
        setting_type = "numeric" if sc.get("type", "").lower() == "numeric" else "enum"
        if setting_type == "numeric":
            options = _synthesize_numeric_options(sc.get("min"), sc.get("max"),
                                                   sc.get("step"), sc.get("valuetype"))
        else:
            raw = sc.get("values") or sc.get("usersettings") or ""
            options = [v.strip() for v in raw.split(",") if v.strip()]
        out.append({
            "key": real_key,
            "name": name_overrides.get(real_key) or real_key,
            "description": description,
            "category": key_category.get(real_key, "Other"),
            "type": setting_type,
            "options": options,
            "value": value,
            "default": sc.get("default", ""),
            "advanced": sc.get("category", "").lower() == "advanced",
        })
    out.sort(key=lambda s: s["key"].lower())
    return out


def set_mod_setting(config_dir, key, value) -> dict:
    """Rewrites one key in UltraPlusConfig.ini, same line-rewrite-preserving
    -comments approach as _apply_user_config_value/merge_config_contents;
    appends a new line if the key isn't already present. Touches an empty
    'config_modified' sentinel file after saving, matching the real app."""
    config_path = Path(config_dir) / "UltraPlusConfig.ini"
    if not config_path.is_file():
        raise RuntimeError("UltraPlusConfig.ini not found - install the mod first.")
    lines = config_path.read_text(errors="replace").splitlines()
    user_values = {key.lower(): (key, value)}
    new_lines = [_apply_user_config_value(line, user_values) for line in lines]
    found = any(m and m.group("key").lower() == key.lower()
                for m in (_CONFIG_SETTING_PATTERN.match(l.rstrip()) for l in lines))
    if not found:
        new_lines.append(f"{key}={value}")
    tmp = config_path.with_suffix(".ini.tmp")
    tmp.write_text("\n".join(new_lines) + "\n")
    tmp.replace(config_path)
    (Path(config_dir) / "config_modified").touch()
    return {"key": key, "value": value}


def restore_mod_defaults(config_dir) -> dict:
    config_path = Path(config_dir) / "UltraPlusConfig.ini"
    default_path = config_path.with_suffix(".default")
    if not default_path.is_file():
        raise RuntimeError("No default snapshot found for this mod.")
    shutil.copy2(default_path, config_path)
    return {"restored": True}


_USER_OWNED_FILE_NAMES = {"ultraplusconfig.ini", "keybinds.ini"}
_STAGED_SUFFIX = ".incoming"


def _redirect_user_owned_file_to_staging(destination_path):
    """Sends a user-owned file to a staging name so extraction never writes
    over the copy the user has been editing."""
    if destination_path is not None and destination_path.name.lower() in _USER_OWNED_FILE_NAMES:
        return destination_path.with_name(destination_path.name + _STAGED_SUFFIX)
    return destination_path


def _apply_staged_user_owned_file(staged_path):
    """Merges one staged file over the user's copy, or adopts it wholesale
    on a first install, writing through a temp file so an interrupted write
    can never truncate the user's config."""
    target_path = staged_path.with_name(staged_path.name[:-len(_STAGED_SUFFIX)])
    applied = [target_path]
    if target_path.name.lower() == "ultraplusconfig.ini":
        default_path = target_path.with_suffix(".default")
        shutil.copy2(staged_path, default_path)
        applied.append(default_path)
    if target_path.is_file():
        merged = merge_config_contents(target_path.read_text(errors="replace"),
                                       staged_path.read_text(errors="replace"))
        tmp = target_path.with_suffix(target_path.suffix + ".tmp")
        tmp.write_text(merged)
        tmp.replace(target_path)
        staged_path.unlink()
    else:
        staged_path.replace(target_path)
    return applied


def _encode_url_path(url):
    """The Ultra+ mods manifest ships download URLs with literal spaces (and
    potentially other unsafe characters) in the path - e.g. '.../Robocop
    Unfinished Business Ultra Plus v0.1.1.zip'. urllib/http.client reject
    those outright ('URL can't contain control characters'), so percent-
    encode just the path component before making the request."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%")
    return urllib.parse.urlunsplit(parts._replace(path=path))


def install_mod(appid, install_path, game_key, catalog, download_url, filename,
                skip_ue4ss, task_id=None) -> dict:
    """Downloads and installs one Ultra+ mod version: zip-slip guard,
    content-aware routing, UE4SS-signature cleanup, and staged/atomic
    writes for the user-owned config files so a failed or interrupted
    install can never truncate the user's live config. Port of
    ModService.InstallModFromDataAsync (Linux Ultra+ Manager, fixed and
    live-verified this session)."""
    import zipfile, io

    if task_id:
        TASKS[task_id] = {"status": "running", "progress": 5, "detail": "Downloading mod"}
    data = _gh_bytes(_encode_url_path(download_url), task_id)
    if task_id:
        TASKS[task_id]["detail"] = "Extracting"

    install_root_only = bool(catalog.get("games", {}).get(game_key, {}).get("install_root_only"))
    executable_path = None if install_root_only else resolve_executable_path(game_key, install_path, catalog)
    project_path = None if install_root_only else resolve_unreal_project_path(
        game_key, install_path, catalog, executable_path)

    if not install_root_only and not executable_path:
        raise RuntimeError("Could not locate the game executable - mod was not installed "
                           "to avoid deploying files to the wrong folder.")
    if not install_root_only and not project_path:
        raise RuntimeError("Could not locate the Unreal project directory for this game.")

    installed_files = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for entry in zf.infolist():
                if entry.is_dir():
                    continue
                if skip_ue4ss and not should_install_with_user_managed_ue4ss(entry.filename):
                    continue
                dest = resolve_destination_path(
                    entry.filename, install_path, project_path,
                    str(executable_path) if executable_path else None, install_root_only)
                if dest is None:
                    continue
                dest = _redirect_user_owned_file_to_staging(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                installed_files.append(dest)

        if not installed_files:
            raise RuntimeError("No files were installed from the mod archive.")

        signature_files = [f for f in installed_files if is_signature_path(f)]
        if signature_files:
            synchronize_signature_directories(signature_files)

        final_files = []
        for f in installed_files:
            if str(f).endswith(_STAGED_SUFFIX):
                final_files.extend(_apply_staged_user_owned_file(f))
            else:
                final_files.append(f)

        version = _extract_version_from_filename(filename)
        final_paths = {str(p) for p in final_files}
        state = load_state()
        installs = state.setdefault("mod_installs", {})
        previous = installs.get(appid)
        if previous:
            # A version can ship a smaller file set than the one it's
            # replacing (e.g. downgrading v1.0.0 -> v0.1.1, which drops
            # keybinds.ini/changelog.txt/preset_uplus_defaults.ini) - delete
            # whatever the old install tracked that the new one didn't
            # rewrite, so Remove later isn't left with orphans.
            for f in previous.get("installed_files", []):
                if f not in final_paths:
                    Path(f).unlink(missing_ok=True)
        installs[appid] = {
            "game_key": game_key,
            "version": version,
            "filename": filename,
            "installed_files": sorted(final_paths),
            "installed_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        save_state(state)

        if task_id:
            TASKS[task_id] = {"status": "done", "progress": 100,
                              "detail": f"Installed v{version}",
                              "result": {"version": version}}
        return {"installed": True, "version": version, "files": len(final_files)}
    except Exception:
        for f in installed_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _install_mod_task(task_id, appid, install_path, game_key, catalog,
                      download_url, filename, skip_ue4ss):
    try:
        install_mod(appid, install_path, game_key, catalog, download_url,
                   filename, skip_ue4ss, task_id=task_id)
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


def remove_mod(appid) -> dict:
    """Deletes every file this install tracked and forgets it."""
    state = load_state()
    installs = state.get("mod_installs", {})
    rec = installs.pop(appid, None)
    if not rec:
        raise RuntimeError("No mod install tracked for this game.")
    for f in rec.get("installed_files", []):
        Path(f).unlink(missing_ok=True)
    save_state(state)
    return {"removed": True}


def _addons_manifest() -> dict:
    """addons_manifest.json - a separate CloudFront manifest from
    mods_manifest.json, one entry per addon-version zip. Cached 5min like
    list_mod_versions' manifest cache."""
    state = load_state()
    cache = state.get("ultraplus_addons_manifest")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 300:
        return cache["data"]
    manifest = _fetch_json(ULTRAPLUS_ADDONS_URL)
    state["ultraplus_addons_manifest"] = {"ts": now, "data": manifest}
    save_state(state)
    return manifest


def list_addons(game_key, catalog, appid) -> list:
    """Per-game optional extra fixes (e.g. Avowed's DisableVRS/EnhancedRT/
    FixPuddles), declared in gamedata.json's addons[] and downloaded from
    addons_manifest.json under filenames '{GameKey} {AddonName} Ultra Plus
    v{version}.zip' - a separate archive per addon, not bundled in the main
    mod's own zip."""
    addons = catalog.get("games", {}).get(game_key, {}).get("addons") or []
    if not addons:
        return []
    manifest = _addons_manifest()
    installed = load_state().get("addon_installs", {}).get(appid, {})
    out = []
    for a in addons:
        file_name = a.get("FileName") or a.get("Name")
        if not file_name:
            continue
        prefix = f"{game_key} {file_name}".lower()
        versions = sorted(
            ({"filename": f["filename"], "url": f.get("url", ""),
              "updated": f.get("updated", ""),
              "version": _extract_version_from_filename(f["filename"])}
             for f in manifest.get("files", [])
             if f.get("filename", "").lower().startswith(prefix)
             and f.get("filename", "").lower().endswith(".zip")),
            key=lambda v: v["updated"], reverse=True)
        out.append({
            "name": a.get("Name") or file_name, "file_name": file_name,
            "description": a.get("Description") or "",
            "installed": installed.get(file_name),
            "versions": versions[:5],
        })
    return out


def install_addon(appid, install_path, game_key, catalog, file_name,
                  download_url, filename, task_id=None) -> dict:
    """Downloads one addon zip; routes .pak/.ucas/.sig/.utoc to the Paks
    ~mods dir (flat - just the filename, matching the real app's
    AddonFilePlacement.ResolveDestinationPath, NOT the nested-path routing
    install_mod's resolve_destination_path uses for the main mod) and .asi
    beside the exe. Tracks installed files per-addon so a later remove_addon
    can undo exactly this install, and a re-download of an updated version
    cleans up the previous version's files first (same reconciliation
    install_mod already does)."""
    import zipfile, io
    if task_id:
        TASKS[task_id] = {"status": "running", "progress": 5, "detail": "Downloading addon"}
    data = _gh_bytes(_encode_url_path(download_url), task_id)
    if task_id:
        TASKS[task_id]["detail"] = "Extracting"

    install_root_only = bool(catalog.get("games", {}).get(game_key, {}).get("install_root_only"))
    executable_path = None if install_root_only else resolve_executable_path(game_key, install_path, catalog)
    project_path = None if install_root_only else resolve_unreal_project_path(
        game_key, install_path, catalog, executable_path)
    exe_dir = Path(executable_path).parent if executable_path else None

    installed_files = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for entry in zf.infolist():
                if entry.is_dir():
                    continue
                segments = _get_safe_segments(entry.filename)
                if not segments:
                    continue
                fname = segments[-1]
                low = fname.lower()
                if low.endswith((".pak", ".ucas", ".sig", ".utoc")):
                    if not project_path:
                        raise RuntimeError("Could not resolve the Unreal project directory for this game.")
                    dest = _combine_safely(project_path, ["Content", "Paks", "~mods", fname])
                elif low.endswith(".asi"):
                    if not exe_dir:
                        raise RuntimeError("Could not resolve the game executable directory.")
                    dest = _combine_safely(exe_dir, [fname])
                else:
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                installed_files.append(dest)

        if not installed_files:
            raise RuntimeError("No .pak/.asi files found in the addon archive.")

        version = _extract_version_from_filename(filename)
        state = load_state()
        addon_installs = state.setdefault("addon_installs", {}).setdefault(appid, {})
        previous = addon_installs.get(file_name)
        new_paths = {str(p) for p in installed_files}
        if previous:
            for f in previous.get("installed_files", []):
                if f not in new_paths:
                    Path(f).unlink(missing_ok=True)
        addon_installs[file_name] = {
            "version": version, "filename": filename,
            "installed_files": sorted(new_paths),
            "installed_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        save_state(state)
        if task_id:
            TASKS[task_id] = {"status": "done", "progress": 100,
                              "detail": f"Installed v{version}", "result": {"version": version}}
        return {"installed": True, "version": version, "files": len(installed_files)}
    except Exception:
        for f in installed_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _install_addon_task(task_id, appid, install_path, game_key, catalog,
                        file_name, download_url, filename):
    try:
        install_addon(appid, install_path, game_key, catalog, file_name,
                     download_url, filename, task_id=task_id)
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


def remove_addon(appid, file_name) -> dict:
    state = load_state()
    addon_installs = state.get("addon_installs", {}).get(appid, {})
    rec = addon_installs.pop(file_name, None)
    if not rec:
        raise RuntimeError("No addon install tracked for this game/addon.")
    for f in rec.get("installed_files", []):
        Path(f).unlink(missing_ok=True)
    if not addon_installs:
        state.get("addon_installs", {}).pop(appid, None)
    save_state(state)
    return {"removed": True}


def dedupe_dll_library() -> None:
    """One-time housekeeping: if two directories under a kind hold the same real
    DLL version (e.g. a garbage-named dir from the old parser plus a correctly
    named one), keep the correctly-named one and remove the rest. Safe to run on
    every startup."""
    if not DLL_LIBRARY.is_dir():
        return
    for kind_dir in DLL_LIBRARY.iterdir():
        if not kind_dir.is_dir():
            continue
        by_version = {}
        for vdir in kind_dir.iterdir():
            if not vdir.is_dir():
                continue
            dll = next(vdir.glob("*.dll"), None)
            if not dll:
                shutil.rmtree(vdir, ignore_errors=True)
                continue
            real = pe_version(dll) or vdir.name
            by_version.setdefault(real, []).append(vdir)
        for real, dirs in by_version.items():
            if len(dirs) < 2:
                continue
            # keep the dir whose name matches the real version, else the first
            keep = next((d for d in dirs if d.name == real), dirs[0])
            for d in dirs:
                if d != keep:
                    shutil.rmtree(d, ignore_errors=True)


def dll_library():
    dedupe_dll_library()
    out = []
    seen = set()
    for kind_dir in sorted(DLL_LIBRARY.iterdir()) if DLL_LIBRARY.is_dir() else []:
        if not kind_dir.is_dir():
            continue
        for ver_dir in sorted(kind_dir.iterdir()):
            dll = next(ver_dir.glob("*.dll"), None)
            if dll:
                # Prefer the version read from the DLL itself; the directory
                # name may be stale garbage from the old parser. Fall back to
                # the dir name only if the DLL can't be read.
                real = pe_version(dll) or ver_dir.name
                # Dedupe by (kind, real version): an old garbage-named dir and a
                # freshly-named dir can hold the same actual DLL version.
                key = (kind_dir.name, real)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "kind": kind_dir.name,
                    "version": real,
                    "friendly": friendly_dlss(real),
                    "path": str(dll),
                    "name": dll.name,
                })
    return out


def import_dll(src_path) -> dict:
    p = Path(src_path).expanduser()
    if not p.is_file():
        raise RuntimeError(f"File not found: {p}")
    if p.name.lower() not in DLSS_KINDS:
        raise RuntimeError(f"Not a recognised DLSS DLL name: {p.name}")
    ver = pe_version(p) or "unknown"
    kind = DLSS_KINDS[p.name.lower()]["kind"]
    kind_root = DLL_LIBRARY / kind
    dest = kind_root / ver
    dest.mkdir(parents=True, exist_ok=True)
    # clear any stale file already in this version dir, then copy the new one
    for old in dest.glob("*.dll"):
        old.unlink()
    shutil.copy2(p, dest / p.name.lower())
    # Remove any OTHER directory for this kind that actually holds the SAME
    # version (e.g. a garbage-named dir from the old parser). Different real
    # versions are kept - downgrading stays possible.
    if kind_root.is_dir():
        for vdir in kind_root.iterdir():
            if not vdir.is_dir() or vdir.name == ver:
                continue
            other = next(vdir.glob("*.dll"), None)
            if other and (pe_version(other) or vdir.name) == ver:
                shutil.rmtree(vdir, ignore_errors=True)
    return {"kind": kind, "version": ver}


# NVIDIA publishes all three DLLs officially on GitHub:
#   SR  -> NVIDIA/DLSS            FG + RR -> NVIDIAGameWorks/Streamline
# We search each repo's file tree by name instead of hardcoding paths, so
# repo reorganisations don't break downloads.
DLL_SOURCES = {
    "sr": [("NVIDIA/DLSS", "nvngx_dlss.dll"),
           ("NVIDIAGameWorks/Streamline", "nvngx_dlss.dll")],
    "fg": [("NVIDIAGameWorks/Streamline", "nvngx_dlssg.dll")],
    "rr": [("NVIDIAGameWorks/Streamline", "nvngx_dlssd.dll")],
}


def version_tuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except (ValueError, AttributeError):
        return (0,)


def friendly_dlss(version) -> dict:
    """310.2.1.0 -> {'gen': 'DLSS 4', 'short': '310.2.1'};
    3.7.10.0 -> {'gen': 'DLSS 3', 'short': '3.7.10'}"""
    if not version:
        return {"gen": "DLSS", "short": "?"}
    parts = str(version).split(".")
    while len(parts) > 2 and parts[-1] == "0":
        parts.pop()
    short = ".".join(parts)
    major = version_tuple(version)[0]
    if major >= 310:
        gen = "DLSS 4"
    elif major == 3:
        gen = "DLSS 3"
    elif major == 2:
        gen = "DLSS 2"
    else:
        gen = "DLSS"
    return {"gen": gen, "short": short}


# --------------------------------------------------------------------------
# DLSS render-preset control (DXVK-NVAPI DRS layer)
# --------------------------------------------------------------------------
# Verbatim excerpt of the four render-preset-selection enums from NVIDIA's own
# NvApiDriverSettings.h (github.com/NVIDIA/nvapi, commit d08488f - the exact
# revision dxvk-nvapi's own README points at for deducing DXVK_NVAPI_DRS_*
# values). SR and RR only go up to preset O, FG goes all the way to Z plus a
# separate Default sentinel, and NR only has A-D - these are NVIDIA's own
# per-feature limits, not something to guess or share across one enum.
_NGX_RENDER_PRESET_ENUMS = """
enum EValues_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION {
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_OFF     = 0,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_A = 1,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_B = 2,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_C = 3,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_D = 4,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_E = 5,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_F = 6,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_G = 7,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_H = 8,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_I = 9,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_J = 10,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_K = 11,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_L = 12,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_M = 13,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_N = 14,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_O = 15,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_Latest = 0x00ffffff,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_NUM_VALUES = 17,
    NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_DEFAULT = NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION_OFF
};

enum EValues_NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION {
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_OFF     = 0,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_A = 1,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_B = 2,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_C = 3,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_D = 4,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_E = 5,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_F = 6,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_G = 7,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_H = 8,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_I = 9,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_J = 10,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_K = 11,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_L = 12,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_M = 13,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_N = 14,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_O = 15,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_Latest = 0x00ffffff,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_NUM_VALUES = 17,
    NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_DEFAULT = NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION_OFF
};

enum EValues_NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION {
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_OFF     = 0,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_A = 1,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_B = 2,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_C = 3,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_D = 4,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_E = 5,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_F = 6,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_G = 7,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_H = 8,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_I = 9,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_J = 10,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_K = 11,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_L = 12,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_M = 13,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_N = 14,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_O = 15,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_P = 16,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_Q = 17,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_R = 18,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_S = 19,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_T = 20,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_U = 21,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_V = 22,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_W = 23,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_X = 24,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_Y = 25,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_Z = 26,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_Default = 0x00fffffe,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_Latest = 0x00ffffff,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_NUM_VALUES = 29,
    NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_DEFAULT = NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION_OFF
};

enum EValues_NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION {
    NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION_OFF     = 0,
    NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_A = 1,
    NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_B = 2,
    NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_C = 3,
    NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_D = 4,
    NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION_RENDER_PRESET_Latest = 0x00ffffff,
    NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION_NUM_VALUES = 6,
    NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION_DEFAULT = NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION_OFF
};
"""


def _parse_ngx_preset_enum(header_text: str, enum_name: str) -> list:
    """Pulls the RENDER_PRESET_<X> members out of one C enum block, lowercased
    (e.g. 'a', 'o', 'latest', 'default'). Skips the OFF/NUM_VALUES/DEFAULT
    bookkeeping members - those aren't selectable presets."""
    m = re.search(re.escape("enum " + enum_name + " {") + r"(.*?)\};",
                  header_text, re.S)
    if not m:
        return []
    values = []
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(",")
        if "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if "RENDER_PRESET_" not in name:
            continue
        token = name.rsplit("RENDER_PRESET_", 1)[1]
        if re.fullmatch(r"[A-Z]|Latest|Default", token):
            values.append(token.lower())
    return values


NGX_RENDER_PRESETS = {
    "sr": _parse_ngx_preset_enum(_NGX_RENDER_PRESET_ENUMS,
                                 "EValues_NGX_DLSS_SR_OVERRIDE_RENDER_PRESET_SELECTION"),
    "rr": _parse_ngx_preset_enum(_NGX_RENDER_PRESET_ENUMS,
                                 "EValues_NGX_DLSS_RR_OVERRIDE_RENDER_PRESET_SELECTION"),
    "fg": _parse_ngx_preset_enum(_NGX_RENDER_PRESET_ENUMS,
                                 "EValues_NGX_DLSS_FG_OVERRIDE_RENDER_PRESET_SELECTION"),
    "nr": _parse_ngx_preset_enum(_NGX_RENDER_PRESET_ENUMS,
                                 "EValues_NGX_DLSS_NR_OVERRIDE_RENDER_PRESET_SELECTION"),
}


def _preset_symbol(token: str) -> str:
    """'a' -> 'A', 'latest' -> 'Latest' - matches the header's own casing for
    the RENDER_PRESET_<Symbol> members, so it can be looked for verbatim in a
    build's compiled nvapi64.dll."""
    return token.upper() if len(token) == 1 else token.capitalize()


def _nvapi_dll_path(tool_dir: Path):
    p = Path(tool_dir) / "files/lib/wine/nvapi/x86_64-windows/nvapi64.dll"
    return p if p.is_file() else None


def nvapi_dll_dlss_support(tool_dir: Path) -> dict:
    """Which NGX DLSS render presets this Proton build's bundled dxvk-nvapi
    actually recognizes, per feature, plus whether it knows the debug
    on-screen-indicator setting at all.

    Every Proton/Proton-GE build ships its own compiled dxvk-nvapi as
    nvapi64.dll. An older build's dxvk-nvapi simply doesn't have a preset
    letter compiled in as a string constant if that preset didn't exist in
    NVIDIA's header yet when it was built - so a literal byte search against
    the real installed binary is a genuine per-build capability probe, not a
    hardcoded Proton-version table. NGX_RENDER_PRESETS (parsed from NVIDIA's
    own header above) is the ceiling; this narrows it to what THIS build can
    actually parse.
    """
    empty = {"sr": [], "rr": [], "fg": [], "nr": [], "debug_indicator": False}
    path = _nvapi_dll_path(tool_dir)
    if not path:
        return empty
    try:
        blob = path.read_bytes()
    except OSError:
        return empty

    def supported(feature_key, values):
        setting = f"NGX_DLSS_{feature_key}_OVERRIDE_RENDER_PRESET_SELECTION".encode()
        if setting not in blob:
            return []
        out = []
        for v in values:
            # plain substring containment would let e.g. "RENDER_PRESET_L"
            # false-positive-match inside "RENDER_PRESET_Latest" - the
            # lookahead requires the token not continue into more identifier
            # characters, so single letters can't accidentally match a
            # longer name that happens to start with them.
            pattern = re.compile(rb"RENDER_PRESET_" +
                                 re.escape(_preset_symbol(v).encode()) +
                                 rb"(?![A-Za-z0-9_])")
            if pattern.search(blob):
                out.append(v)
        return out

    return {
        "sr": supported("SR", NGX_RENDER_PRESETS["sr"]),
        "rr": supported("RR", NGX_RENDER_PRESETS["rr"]),
        "fg": supported("FG", NGX_RENDER_PRESETS["fg"]),
        "nr": supported("NR", NGX_RENDER_PRESETS["nr"]),
        "debug_indicator": b"DXVK_NVAPI_SET_NGX_DEBUG_OPTIONS" in blob,
    }


def get_dlss_preset_defaults() -> dict:
    """The saved 'global default' DLSS preset template - a starting point a
    user can apply to whichever game they're looking at, not something
    auto-pushed into every game's own launch options."""
    return load_state().get("dlss_preset_defaults", {})


def set_dlss_preset_defaults(settings: dict) -> dict:
    state = load_state()
    state["dlss_preset_defaults"] = settings
    save_state(state)
    return settings


def _gh_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pcc",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


CANDIDATE_DLL_DIRS = [
    "bin/x64", "bin/x64/rel", "bin/x64/release", "bin/x64/development",
    "lib/Windows_x86_64/rel", "lib/Windows_x86_64",
    "sdk/bin/x64", "runtime/bin/x64",
]


def _gh_bytes(url, task=None):
    req = urllib.request.Request(url, headers={"User-Agent": "pcc"})
    with urllib.request.urlopen(req, timeout=300) as r:
        total = int(r.headers.get("Content-Length") or 0)
        got, chunks = 0, []
        while True:
            c = r.read(262144)
            if not c:
                break
            chunks.append(c)
            got += len(c)
            if task and total:
                TASKS[task]["progress"] = int(got / total * 100)
    return b"".join(chunks)


def _resolve_lfs(repo, branch, path, data, task=None):
    """Large NVIDIA binaries are stored via Git LFS: the raw URL returns a
    small pointer file. media.githubusercontent serves the real content."""
    if data.startswith(b"version https://git-lfs"):
        return _gh_bytes(f"https://media.githubusercontent.com/media/"
                         f"{repo}/{branch}/{path}", task)
    return data


def _find_in_tree(repo, branch, fname):
    """Returns (paths, truncated). GitHub truncates trees for big repos like
    Streamline, so a miss with truncated=True is inconclusive."""
    tree = _gh_json(f"https://api.github.com/repos/{repo}"
                    f"/git/trees/{branch}?recursive=1")
    hits = [e["path"] for e in tree.get("tree", [])
            if e.get("type") == "blob"
            and (e["path"].lower().endswith("/" + fname)
                 or e.get("path", "").lower() == fname)]
    hits.sort(key=lambda p: ("rel" not in p.lower() and "bin" not in p.lower(),
                             "dev" in p.lower(), len(p)))
    return hits, bool(tree.get("truncated"))


def _probe_dirs(repo, branch, fname):
    """Contents-API probe of known DLL directories - works even when the
    tree listing is truncated."""
    for d in CANDIDATE_DLL_DIRS:
        try:
            entries = _gh_json(f"https://api.github.com/repos/{repo}"
                               f"/contents/{d}?ref={branch}")
        except Exception:
            continue
        if isinstance(entries, list):
            for e in entries:
                if e.get("name", "").lower() == fname:
                    return e["path"]
    return None


def _try_release_zip(repo, fname, task_id):
    """Last resort: pull the newest release asset zip and extract the DLL."""
    import zipfile
    import io
    rel = _gh_json(f"https://api.github.com/repos/{repo}/releases/latest")
    assets = rel.get("assets") or []
    assets.sort(key=lambda a: a.get("size", 0))     # smallest plausible first
    for a in assets:
        if not a.get("name", "").lower().endswith(".zip"):
            continue
        if a.get("size", 0) > 800 * 1024 * 1024:
            continue
        TASKS[task_id]["detail"] = f"Downloading release {a['name']}"
        data = _gh_bytes(a["browser_download_url"], task_id)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                members = [m for m in z.namelist()
                           if m.lower().endswith(fname)]
                members.sort(key=lambda m: ("rel" not in m.lower(),
                                            "dev" in m.lower(), len(m)))
                if members:
                    return z.read(members[0])
        except zipfile.BadZipFile:
            continue
    return None



DLSS_MANIFEST_URL = ("https://raw.githubusercontent.com/beeradmoore/"
                     "dlss-swapper-manifest-builder/refs/heads/main/manifest.json")

# Section names inside the manifest, verified from DLSS Swapper's wiki/source:
#   dlss = Super Resolution, dlss_g = Frame Generation, dlss_d = Ray Reconstruction
DLSS_MANIFEST_SECTION = {"sr": "dlss", "fg": "dlss_g", "rr": "dlss_d"}


def _manifest_latest(kind, task_id):
    """Fetch DLSS Swapper's manifest (the same one that tool refreshes every
    launch) and return (version, dll_bytes) for the newest STABLE entry of the
    requested kind. Covers SR/FG/RR - this is how the latest DLSS 4.x DLLs are
    found. Returns None on any failure so callers fall back to NVIDIA repos."""
    import zipfile, io
    section = DLSS_MANIFEST_SECTION.get(kind)
    if not section:
        return None
    try:
        manifest = _gh_json(DLSS_MANIFEST_URL)
    except Exception:
        return None
    entries = manifest.get(section) if isinstance(manifest, dict) else None
    if not entries:
        return None
    # entries carry a version_number (packed 64-bit) or a dotted version string
    def _key(e):
        vn = e.get("version_number")
        if isinstance(vn, int):
            return vn
        return version_tuple(e.get("version", "0"))
    best = max(entries, key=_key)
    dl = best.get("download_url")
    if not dl:
        return None
    TASKS[task_id]["detail"] = f"Manifest has {best.get('version')}, downloading"
    data = _gh_bytes(dl, task_id)
    fname = KIND_TO_NAME.get(kind)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = [m for m in z.namelist() if m.lower().endswith(fname)]
            if members:
                return best.get("version"), z.read(members[0])
    except zipfile.BadZipFile:
        pass
    return None


def download_dlss(task_id, kind) -> None:
    """Fetch the requested DLL kind from NVIDIA's official repos. Strategy per
    repo: tree search -> directory probe (tree may be truncated) -> release
    zip. Raw downloads resolve Git LFS pointers automatically."""
    dll_name = KIND_TO_NAME.get(kind)
    label = {"sr": "Super Resolution", "fg": "Frame Generation",
             "rr": "Ray Reconstruction"}.get(kind, kind)
    TASKS[task_id] = {"status": "running", "progress": 0,
                      "detail": f"Looking for {label} DLL"}
    errors = []

    # PRIMARY: DLSS Swapper's manifest, checked alongside RHI's own manifest -
    # both are refreshed constantly and each has been the first to carry a
    # brand-new SR/FG/RR build at different times, so take whichever reports
    # the higher version (this is the fix for "not fetching the latest").
    got = None
    try:
        TASKS[task_id]["detail"] = "Checking DLSS Swapper manifest"
        got = _manifest_latest(kind, task_id)
    except Exception as e:
        errors.append(f"manifest: {e}")
    try:
        TASKS[task_id]["detail"] = "Checking RHI manifest"
        rhi_got = _rhi_manifest_latest(kind, task_id)
        if rhi_got and (not got or version_tuple(rhi_got[0]) > version_tuple(got[0])):
            got = rhi_got
    except Exception as e:
        errors.append(f"rhi manifest: {e}")
    if got:
        version, data = got
        tmp_final = DATA_DIR / dll_name
        try:
            tmp_final.write_bytes(data)
            if data[:2] == b"MZ" and pe_version(tmp_final):
                info = import_dll(tmp_final)
                fr = friendly_dlss(info["version"])
                TASKS[task_id] = {"status": "done", "progress": 100,
                                  "detail": f"Added {fr['gen']} {label} "
                                            f"{fr['short']}"}
                return
        finally:
            tmp_final.unlink(missing_ok=True)
    for repo, fname in DLL_SOURCES.get(kind, []):
        try:
            TASKS[task_id]["detail"] = f"Searching {repo}"
            branch = _gh_json(f"https://api.github.com/repos/{repo}")\
                .get("default_branch", "main")
            data = None
            hits, truncated = [], False
            try:
                hits, truncated = _find_in_tree(repo, branch, fname)
            except Exception:
                truncated = True
            path = hits[0] if hits else None
            if not path and truncated:
                TASKS[task_id]["detail"] = f"{repo}: large repo, probing folders"
                path = _probe_dirs(repo, branch, fname)
            if path:
                TASKS[task_id]["detail"] = f"Downloading {fname} from {repo}"
                data = _gh_bytes(f"https://raw.githubusercontent.com/"
                                 f"{repo}/{branch}/{path}", task_id)
                data = _resolve_lfs(repo, branch, path, data, task_id)
            if data is None:
                TASKS[task_id]["detail"] = f"{repo}: checking release assets"
                data = _try_release_zip(repo, fname, task_id)
            if data is None:
                errors.append(f"{repo}: {fname} not found")
                continue
            tmp_final = DATA_DIR / fname
            info = None
            try:
                tmp_final.write_bytes(data)
                if data[:2] != b"MZ" or not pe_version(tmp_final):
                    raise RuntimeError("downloaded file isn't a valid DLL")
                info = import_dll(tmp_final)
            finally:
                tmp_final.unlink(missing_ok=True)
            fr = friendly_dlss(info["version"])
            TASKS[task_id] = {"status": "done", "progress": 100,
                              "detail": f"Added {fr['gen']} {label} "
                                        f"{fr['short']} to library"}
            return
        except Exception as e:
            errors.append(f"{repo}: {e}")
    TASKS[task_id] = {
        "status": "error", "progress": 0,
        "detail": ("Couldn't fetch the DLL ("
                   + "; ".join(errors[:2]) + "). You can still download it "
                   "manually (e.g. TechPowerUp) and import it below.")}


def download_latest_sr(task_id):  # kept for compatibility
    download_dlss(task_id, "sr")


def swap_dll(game_dll_path, library_dll_path) -> dict:
    game_dll = Path(game_dll_path)
    lib_dll = Path(library_dll_path)
    if not game_dll.is_file():
        raise RuntimeError(f"Game DLL missing: {game_dll}")
    if not lib_dll.is_file():
        raise RuntimeError(f"Library DLL missing: {lib_dll}")
    if game_dll.name.lower() != lib_dll.name.lower():
        raise RuntimeError("DLL type mismatch — refusing to swap different DLSS components")
    bak = _backup_path(game_dll)
    if not bak.exists():
        shutil.copy2(game_dll, bak)
    shutil.copy2(lib_dll, game_dll)
    return {"swapped": True, "new_version": pe_version(game_dll), "backup": str(bak)}


def restore_dll(game_dll_path) -> dict:
    game_dll = Path(game_dll_path)
    bak = _backup_path(game_dll)
    if not bak.exists():
        raise RuntimeError("No backup exists for this DLL")
    shutil.copy2(bak, game_dll)
    return {"restored": True, "version": pe_version(game_dll)}


_EXE_SKIP_DIRS = {"_commonredist", "redist", "directx", "vcredist", "crashreporter", "crashpad"}
_EXE_SKIP_NAME_HINTS = ("unins", "redist", "vcredist", "directx", "dxsetup",
                        "crashreporter", "crashpad", "easyanticheat",
                        "battleye", "vc_redist", "7za", "7z.exe")


def _walk_exe_candidates(base):
    """Every plausible game .exe under an install root, biggest first,
    skipping obvious installers/redistributables/anti-cheat launchers by
    name. Shared by `_find_game_exe` (single best guess) and
    `find_game_exe_candidates` (full list, for dual-build games)."""
    candidates = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d.lower() not in _EXE_SKIP_DIRS]
        for fn in filenames:
            low = fn.lower()
            if not low.endswith(".exe") or any(h in low for h in _EXE_SKIP_NAME_HINTS):
                continue
            p = Path(dirpath) / fn
            try:
                size = p.stat().st_size
            except OSError:
                continue
            candidates.append((size, p))
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates


def _find_game_exe(install_path):
    """Best-effort: prefers the largest .exe that actually imports a known
    graphics API DLL (d3d9/d3d11/d3d12/dxgi/opengl32/vulkan-1) over one that
    doesn't, skipping obvious installers/redistributables/anti-cheat
    launchers by name first. A pure launcher/installer stub - however large
    - has no reason to link against a graphics API at all, while the real
    game binary always does; this is a real, checkable signal (PE import
    table), not another name-based guess. Confirmed against a real case
    live: The Witcher 3 ships a 642MB `setup_redlauncher.exe` at its
    install root - far bigger than the real `bin/x64/witcher3.exe` (86MB)
    - which the old largest-file-wins heuristic (also RHI's own approach,
    per PeHeaderService.FindGameExe - not solved better upstream either)
    always picked instead. Only the top candidates by size are PE-scanned
    (bounded cost regardless of how many .exe files a game ships), and
    falls back to the plain largest-file heuristic if none of them import
    a graphics DLL - the API-override field in the UI exists for when even
    this guesses wrong."""
    base = Path(install_path)
    if not base.is_dir():
        return None
    candidates = _walk_exe_candidates(base)
    if not candidates:
        return None

    best_with_api, best_with_api_size = None, -1
    for size, p in candidates[:10]:
        try:
            _, regular, delay = pe_imports(p)
            api = detect_graphics_api(regular, delay)
        except Exception:
            continue
        if api and size > best_with_api_size:
            best_with_api, best_with_api_size = p, size
    if best_with_api:
        return best_with_api
    return candidates[0][1]


_PATH_API_HINTS = [
    ("dx12", "d3d12"), ("d3d12", "d3d12"), ("directx12", "d3d12"),
    ("dx11", "d3d11"), ("d3d11", "d3d11"), ("directx11", "d3d11"),
    ("dx10", "d3d10"), ("d3d10", "d3d10"),
    ("dx9", "d3d9"), ("d3d9", "d3d9"), ("directx9", "d3d9"),
    ("dx8", "d3d8"), ("d3d8", "d3d8"),
    ("vulkan", "vulkan"),
    ("opengl", "opengl"),
]


def _infer_api_from_path(exe_path) -> str | None:
    """Fallback label for exes a static PE import scan can't read anything
    from - e.g. The Witcher 3's `bin/x64_dx12/witcher3.exe`, which loads
    dxgi.dll/d3d12.dll dynamically at runtime via LoadLibrary rather than
    importing them, so the import table is genuinely empty of any graphics
    DLL. Folder/file names like `x64_dx12` are a real (if weaker) signal a
    developer chose deliberately, worth surfacing as "looks like DX12
    (from folder name)" instead of a flat "couldn't tell" - never used for
    any install-time behavioral decision (Vulkan refusal etc still relies
    only on the real PE scan), display purposes only."""
    parts = [p.lower() for p in Path(exe_path).parts]
    haystack = "/".join(parts)
    for token, api in _PATH_API_HINTS:
        if token in haystack:
            return api
    return None


_API_DISPLAY_NAMES = {
    "d3d12": "DirectX 12", "d3d11": "DirectX 11", "d3d10": "DirectX 10",
    "d3d9": "DirectX 9", "d3d8": "DirectX 8", "vulkan": "Vulkan",
    "opengl": "OpenGL",
}


def describe_graphics_api(api, exe_path=None) -> dict:
    """Friendly display label for a detected (or undetected) graphics API,
    falling back to a folder-name-based guess so the UI can say "looks like
    DirectX 12 (from folder name)" instead of just "couldn't tell" - this
    is the dual DX11/DX12-build case (games that ship separate exes per
    renderer in separate folders, e.g. bin/x64/ vs bin/x64_dx12/) that a
    static import scan alone can't always resolve."""
    if api:
        return {"label": _API_DISPLAY_NAMES.get(api, api.upper()), "inferred": False}
    if exe_path:
        guess = _infer_api_from_path(exe_path)
        if guess:
            return {"label": f"{_API_DISPLAY_NAMES.get(guess, guess.upper())} (from folder name)",
                    "inferred": True}
    return {"label": None, "inferred": False}


def find_game_exe_candidates(install_path, limit=8) -> list[dict]:
    """Every distinct game build under an install root, not just the single
    best guess `_find_game_exe` returns - built for games that ship more
    than one renderer as separate exes in separate folders (confirmed live:
    The Witcher 3 ships both `bin/x64/witcher3.exe` [DX11, statically
    detectable] and `bin/x64_dx12/witcher3.exe` [DX12, loads its API
    dynamically - undetectable via import-table scanning]). One candidate
    per unique parent directory (the largest .exe in each folder - a
    launcher stub and the real game binary rarely share a directory),
    ranked real-API-detected first, then folder-name-inferred, then
    unknown; by size within each tier. Powers the RHI tab's "Detected
    builds" picker so the user can see every build PCC found and explicitly
    choose which one ReShade/OptiScaler/DXVK/shader packs should target,
    instead of silently guessing wrong the way the shader-pack path-
    resolution bug did."""
    base = Path(install_path)
    if not base.is_dir():
        return []
    candidates = _walk_exe_candidates(base)
    by_dir = {}
    for size, p in candidates:
        d = str(p.parent)
        if d not in by_dir or size > by_dir[d][0]:
            by_dir[d] = (size, p)

    out = []
    for size, p in by_dir.values():
        try:
            _, regular, delay = pe_imports(p)
            api = detect_graphics_api(regular, delay)
        except Exception:
            api = None
        display = describe_graphics_api(api, p)
        rank = 0 if api else (1 if display["inferred"] else 2)
        out.append({"path": str(p), "size": size, "api": api,
                    "label": display["label"] or "Unknown engine",
                    "inferred": display["inferred"], "_rank": rank})
    out.sort(key=lambda c: (c["_rank"], -c["size"]))
    for c in out:
        del c["_rank"]
    return out[:limit]


def detect_game_builds(install_path) -> dict:
    """Groups `find_game_exe_candidates` output for the RHI tab: the ranked
    candidate list, plus whether this game actually has more than one
    distinct build (different detected/inferred APIs in different folders)
    - the case that caused ReShade to silently install into the wrong
    folder for a dual-renderer game until this was added."""
    candidates = find_game_exe_candidates(install_path)
    apis = {c["api"] for c in candidates if c["api"]} | \
           {c["label"] for c in candidates if c["api"] is None and c["inferred"]}
    return {"candidates": candidates, "has_multiple_builds": len(apis) > 1}


# --------------------------------------------------------------------------
# RHI port: graphics API detection
# --------------------------------------------------------------------------
# (dll import name, api id, priority) - higher priority wins when a game
# imports more than one. Port of RHI's GraphicsApiDetector.
_GRAPHICS_DLL_PRIORITY = [
    ("d3d12.dll", "d3d12", 7),
    ("vulkan-1.dll", "vulkan", 6),
    ("d3d11.dll", "d3d11", 5),
    ("d3d10.dll", "d3d10", 4),
    ("d3d10_1.dll", "d3d10", 4),
    ("opengl32.dll", "opengl", 3),
    ("d3d9.dll", "d3d9", 2),
    ("d3d8.dll", "d3d8", 1),
]
_UNITY_GFX_DEVICE_MAP = {2: "d3d9", 17: "d3d11", 18: "d3d12", 21: "vulkan", 4: "opengl"}


def pe_imports(path):
    """Read a PE exe's machine type (32/64-bit) and imported DLL names, both
    regular and delay-loaded, with no dependency beyond stdlib: parse the
    DOS/PE/section headers by hand and walk both import directory tables.
    Returns (bitness, {regular names}, {delay-load names}), or
    (None, set(), set()) if it doesn't look like a PE file."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None, set(), set()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None, set(), set()
    pe_off = struct.unpack_from("<i", data, 0x3C)[0]
    if pe_off < 0 or pe_off + 24 > len(data) or data[pe_off:pe_off + 4] != b"PE\x00\x00":
        return None, set(), set()
    coff = pe_off + 4
    machine = struct.unpack_from("<H", data, coff)[0]
    bitness = {0x8664: 64, 0x14c: 32}.get(machine)
    n_sections = struct.unpack_from("<H", data, coff + 2)[0]
    size_opt = struct.unpack_from("<H", data, coff + 16)[0]
    opt_off = coff + 20
    if size_opt < 2 or opt_off + size_opt > len(data):
        return bitness, set(), set()
    magic = struct.unpack_from("<H", data, opt_off)[0]
    if magic == 0x10B:      # PE32
        dir_array_off = opt_off + 96
    elif magic == 0x20B:    # PE32+
        dir_array_off = opt_off + 112
    else:
        return bitness, set(), set()
    imp_dir_off = dir_array_off + 1 * 8          # IMAGE_DIRECTORY_ENTRY_IMPORT
    delay_dir_off = dir_array_off + 13 * 8       # IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT
    if delay_dir_off + 8 > len(data):
        return bitness, set(), set()
    sections = []
    for i in range(n_sections):
        off = opt_off + size_opt + i * 40
        if off + 40 > len(data):
            break
        vsize, va = struct.unpack_from("<II", data, off + 8)
        raw_ptr = struct.unpack_from("<I", data, off + 20)[0]
        sections.append((va, vsize, raw_ptr))

    def rva2off(rva):
        for va, vsize, raw_ptr in sections:
            if va <= rva < va + vsize:
                return raw_ptr + (rva - va)
        return None

    def read_cstr(off, cap=256):
        end = data.find(b"\x00", off, off + cap)
        if end < 0:
            end = off + cap
        return data[off:end].decode("ascii", "ignore").lower()

    def walk_import_table(dir_off, name_rva_offset, entry_size):
        import_rva = struct.unpack_from("<I", data, dir_off)[0]
        if not import_rva:
            return set()
        imp_off = rva2off(import_rva)
        if imp_off is None:
            return set()
        names = set()
        i = 0
        while True:
            entry_off = imp_off + i * entry_size
            if entry_off + entry_size > len(data):
                break
            name_rva = struct.unpack_from("<I", data, entry_off + name_rva_offset)[0]
            if not name_rva:
                break
            noff = rva2off(name_rva)
            if noff is not None:
                names.add(read_cstr(noff))
            i += 1
        return names

    regular = walk_import_table(imp_dir_off, 12, 20)
    delay = walk_import_table(delay_dir_off, 4, 32)
    return bitness, regular, delay


def detect_graphics_api(dll_names, delay_names=None) -> str | None:
    """Highest-priority graphics API among a PE's imported DLLs. Checks the
    regular import table first. If dxgi.dll was regular-imported and nothing
    >= DX11 priority was found there, returns DX12 immediately WITHOUT
    consulting delay-loads - a DX12 game that creates its device through
    dxgi.dll alone (no d3d12.dll import, common in modern engines) is
    unambiguous. Only when dxgi.dll wasn't regular-imported (or a >=DX11
    match already was) does it fall back to the delay-load import table
    (data-directory index 13), since engines like UE4/5 often delay-load
    d3d12.dll as an optional path while explicitly importing their real
    default API (typically d3d11.dll) in the regular table - promoting on
    delay-load alone would misdetect those as DX12. Port of RHI's
    GraphicsApiDetector.Detect - this ordering (dxgi short-circuit BEFORE
    delay-load scan) matches it exactly; scanning delay-loads first, as an
    earlier version of this function did, could misclassify a game that
    regular-imports only dxgi.dll but delay-loads d3d11.dll as DX11 instead
    of DX12."""
    best, best_pri = None, 0
    for dll, api, pri in _GRAPHICS_DLL_PRIORITY:
        if dll in dll_names and pri > best_pri:
            best, best_pri = api, pri
    if "dxgi.dll" in dll_names and best_pri < 5:
        return "d3d12"
    if best_pri < 5 and delay_names:
        for dll, api, pri in _GRAPHICS_DLL_PRIORITY:
            if dll in delay_names and pri > best_pri:
                best, best_pri = api, pri
    return best


_EXPLICIT_DX_APIS = {"d3d8", "d3d9", "d3d10", "d3d11", "d3d12"}


def _detect_all_graphics_apis(dll_names, delay_names=None) -> set:
    """ALL graphics APIs present among a PE's imports (regular + delay-load,
    unconditionally - unlike detect_graphics_api's single best-match, which
    only consults delay-loads when nothing >=DX11 was found in the regular
    table). Includes the dxgi-only DX12 inference. Port of RHI's
    GraphicsApiDetector.DetectAllApis - used to pick ReShade's default
    install filename, where (unlike generic API detection) a legacy d3d9.dll
    import must be seen even on a game whose PRIMARY api is DX11/12."""
    apis = set()
    has_explicit_dx = False
    for dll, api, _pri in _GRAPHICS_DLL_PRIORITY:
        if dll in dll_names or (delay_names and dll in delay_names):
            apis.add(api)
            if api in _EXPLICIT_DX_APIS:
                has_explicit_dx = True
    if "dxgi.dll" in dll_names and not has_explicit_dx:
        apis.add("d3d12")
    return apis


def resolve_auto_reshade_filename(apis) -> str:
    """ReShade's default install filename for a game's detected graphics
    APIs (as returned by _detect_all_graphics_apis). DX11/DX12 take
    precedence over everything else - many games import d3d9.dll for legacy
    reasons even though they primarily render DX11/12. OpenGL only applies
    when it's the ONLY api detected (some engines statically link
    opengl32.dll as an unused fallback while actually rendering DirectX).
    Port of RHI's ResolveAutoReShadeFilename (MainViewModel.Install.Luma.cs)."""
    if "d3d11" in apis or "d3d12" in apis:
        return "dxgi.dll"
    if "d3d9" in apis:
        return "d3d9.dll"
    if "d3d8" in apis:
        return "d3d8.dll"
    if apis == {"opengl"}:
        return "opengl32.dll"
    return "dxgi.dll"


def _detect_unity_api(exe_path):
    """Unity-specific fallback for when import-table scanning finds nothing:
    Unity's player binary imports very little directly and picks its API at
    runtime, so read boot.config's gfx-device-type override instead. Returns
    None if this doesn't look like a Unity game at all (no <name>_Data dir);
    "d3d11" (Unity's Windows default) if it is one but boot.config is
    missing or has no override (pre-boot.config Unity 5-and-earlier builds,
    or a build that never set this key)."""
    exe_path = Path(exe_path)
    data_dir = exe_path.parent / (exe_path.stem + "_Data")
    if not data_dir.is_dir():
        return None
    boot_cfg = data_dir / "boot.config"
    if boot_cfg.is_file():
        try:
            for line in boot_cfg.read_text(errors="replace").splitlines():
                line = line.strip()
                if line.startswith("gfx-device-type="):
                    val = int(line.split("=", 1)[1].strip())
                    return _UNITY_GFX_DEVICE_MAP.get(val, "d3d11")
        except (OSError, ValueError):
            pass
    return "d3d11"


def detect_game_graphics_api(exe_path) -> dict:
    """Full detection pipeline for one exe: PE import scan (regular + delay-
    load), falling back to the Unity boot.config heuristic if the PE scan
    found nothing. Cached in state.json keyed by (path, mtime) so repeated
    panel opens don't re-parse the exe every time."""
    exe_path = str(exe_path)
    try:
        mtime = Path(exe_path).stat().st_mtime
    except OSError:
        return {"bitness": None, "api": None}
    state = load_state()
    cache = state.setdefault("rhi_api_cache", {})
    entry = cache.get(exe_path)
    if entry and entry.get("mtime") == mtime:
        return {"bitness": entry.get("bitness"), "api": entry.get("api")}

    bitness, regular, delay = pe_imports(exe_path)
    api = detect_graphics_api(regular, delay)
    if api is None:
        api = _detect_unity_api(exe_path)
    cache[exe_path] = {"mtime": mtime, "bitness": bitness, "api": api}
    save_state(state)
    return {"bitness": bitness, "api": api}


# --------------------------------------------------------------------------
# RHI port: ReShade install (Stable channel) + RE Framework companion
# --------------------------------------------------------------------------
RHI_DATA_DIR = DATA_DIR / "rhi"
# RHI's own manifest.json - overrides/warnings/blacklists layered on top of
# the mostly-static catalogs the rest of this file uses (shader packs,
# addons, dlssPresets, etc.), fetched live so this port doesn't silently
# drift as upstream's manifest changes. Not every field here is consumed
# yet - dxvkBlacklist (anti-cheat titles where DXVK risks a ban) is the
# safety-relevant one wired in so far; see rhi_manifest()/is_dxvk_blacklisted.
RHI_MANIFEST_URL = "https://raw.githubusercontent.com/RankFTW/RHI/main/manifest.json"


def rhi_manifest() -> dict:
    """Fetches+caches RHI's manifest.json, 6h cached like every other
    RHI-port catalog here. Returns {} (not raising) on fetch failure so
    every consumer degrades to "no override data" rather than breaking -
    this manifest is a layer of polish/safety data on top of catalogs that
    otherwise work fine without it."""
    state = load_state()
    cache = state.get("rhi_manifest")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    try:
        data = _fetch_json(RHI_MANIFEST_URL)
    except Exception:
        return cache["data"] if cache else {}
    state["rhi_manifest"] = {"ts": now, "data": data}
    save_state(state)
    return data


def is_dxvk_blacklisted(game_name) -> bool:
    """True if this game's exact display name (case-insensitive) is on
    RHI manifest.json's dxvkBlacklist - titles where DXVK risks an
    anti-cheat ban. Port of GameInitializationService's blacklistSet.Contains
    check. Never raises - a manifest fetch failure means "unknown", not
    "blacklisted", so it doesn't itself block installs it can't verify."""
    if not game_name:
        return False
    try:
        blacklist = rhi_manifest().get("dxvkBlacklist") or []
    except Exception:
        return False
    return game_name.strip().lower() in {b.strip().lower() for b in blacklist}
RESHADE_STAGING_DIR = RHI_DATA_DIR / "reshade"
RESHADE_NORMAL_STAGING_DIR = RHI_DATA_DIR / "reshade-normal"     # No Addons channel
RESHADE_NIGHTLY_STAGING_DIR = RHI_DATA_DIR / "reshade-nightly"
RESHADE_LEGACY_STAGING_DIR = RHI_DATA_DIR / "reshade-legacy"
RESHADE_CUSTOM_DIR = RHI_DATA_DIR / "reshade-custom"             # user drops DLLs here
RESHADE_DOWNLOADS_PAGE = "https://reshade.me/"
RESHADE_NIGHTLY_URLS = {
    64: "https://nightly.link/crosire/reshade/workflows/build/main/ReShade%20(64-bit).zip",
    32: "https://nightly.link/crosire/reshade/workflows/build/main/ReShade%20(32-bit).zip",
}
RE_FRAMEWORK_ZIP_URL = ("https://github.com/praydog/REFramework-nightly/"
                        "releases/latest/download/REFramework.zip")
RE_FRAMEWORK_RELEASES_API = "https://api.github.com/repos/praydog/REFramework-nightly/releases"
_RESHADE_MIN_SIZE = 1_000_000   # below this, treat a staged/installed DLL as corrupt
RESHADE_CHANNELS = ("stable", "no_addons", "nightly", "legacy", "custom")


def reshade_latest() -> dict:
    """Scrapes reshade.me for the current Stable (Addon-capable) build.
    Cached 6h, same pattern as list_ge_proton(). Port of RHI's
    ReShadeUpdateService."""
    state = load_state()
    cache = state.get("reshade_latest")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    req = urllib.request.Request(RESHADE_DOWNLOADS_PAGE,
                                 headers={"User-Agent": "Mozilla/5.0 pcc"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(r"downloads/ReShade_Setup_([\d.]+)_Addon\.exe", html)
    if not m:
        raise RuntimeError("Couldn't find a current ReShade build on reshade.me")
    version = m.group(1)
    data = {"version": version,
            "url": f"https://reshade.me/downloads/ReShade_Setup_{version}_Addon.exe"}
    state["reshade_latest"] = {"ts": now, "data": data}
    save_state(state)
    return data


def _reshade_engine_cached(engine_dir) -> bool:
    dll64, dll32 = engine_dir / "ReShade64.dll", engine_dir / "ReShade32.dll"
    return (dll64.is_file() and dll64.stat().st_size >= _RESHADE_MIN_SIZE
            and dll32.is_file() and dll32.stat().st_size >= _RESHADE_MIN_SIZE)


def _download_and_extract_reshade_exe(url, engine_dir, task_id=None, label="ReShade") -> Path:
    """Shared by Stable/No-Addons/Legacy: downloads a reshade.me setup .exe
    and pulls ReShade32.dll/ReShade64.dll straight out of it. The installer
    is a plain zip with a stub exe prepended - stdlib zipfile finds the
    end-of-central-directory record by scanning back from EOF, no extra
    tooling needed (matches RHI's own approach - it uses SharpCompress/7z
    for the same trick, but Python's zipfile already does this natively)."""
    import zipfile, io
    if task_id:
        TASKS[task_id] = {"status": "running", "progress": 10,
                          "detail": f"Downloading {label}"}
    data = _gh_bytes(url, task_id)
    if len(data) < 500_000 or data[:2] != b"MZ":
        raise RuntimeError("Download from reshade.me didn't look like a real "
                           "installer (got an error page?) - try again.")
    if task_id:
        TASKS[task_id]["detail"] = "Extracting"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        for dll in ("ReShade32.dll", "ReShade64.dll"):
            if dll not in names:
                raise RuntimeError(f"ReShade installer didn't contain {dll} "
                                   "(its format may have changed)")
        engine_dir.mkdir(parents=True, exist_ok=True)
        for dll in ("ReShade32.dll", "ReShade64.dll"):
            (engine_dir / dll).write_bytes(zf.read(dll))
    return engine_dir


def ensure_reshade_engine(version, url=None, task_id=None) -> Path:
    """Stable (Addon-capable) channel. Cached per version so repeat installs
    across games don't re-download."""
    engine_dir = RESHADE_STAGING_DIR / version
    if _reshade_engine_cached(engine_dir):
        return engine_dir
    if not url:
        url = reshade_latest()["url"]
    return _download_and_extract_reshade_exe(url, engine_dir, task_id,
                                             label=f"ReShade {version}")


def reshade_no_addons_latest() -> dict:
    """Scrapes reshade.me for the current No-Addons (plain) build - same
    page as the Stable scrape, but the filename pattern naturally excludes
    the Addon build: 'ReShade_Setup_X.Y.Z.exe' can't match inside
    'ReShade_Setup_X.Y.Z_Addon.exe' since '.exe' isn't what immediately
    follows the version there. Port of RHI's NormalReShadeUpdateService."""
    state = load_state()
    cache = state.get("reshade_no_addons_latest")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    req = urllib.request.Request(RESHADE_DOWNLOADS_PAGE,
                                 headers={"User-Agent": "Mozilla/5.0 pcc"})
    with urllib.request.urlopen(req, timeout=30) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(r"downloads/ReShade_Setup_([\d.]+)\.exe", html)
    if not m:
        raise RuntimeError("Couldn't find a current No-Addons ReShade build on reshade.me")
    version = m.group(1)
    data = {"version": version,
            "url": f"https://reshade.me/downloads/ReShade_Setup_{version}.exe"}
    state["reshade_no_addons_latest"] = {"ts": now, "data": data}
    save_state(state)
    return data


def ensure_reshade_no_addons_engine(version, url=None, task_id=None) -> Path:
    engine_dir = RESHADE_NORMAL_STAGING_DIR / version
    if _reshade_engine_cached(engine_dir):
        return engine_dir
    if not url:
        url = reshade_no_addons_latest()["url"]
    return _download_and_extract_reshade_exe(url, engine_dir, task_id,
                                             label=f"ReShade {version} (No Addons)")


def ensure_reshade_legacy_engine(version, task_id=None) -> Path:
    """Pin to a specific older Stable version (>=6.0.0, the first
    addon-DLL-capable release) via the same reshade.me URL pattern with an
    explicit version - no scrape needed, the URL is deterministic. Excluded
    from update checks by design elsewhere (that's the point of pinning)."""
    engine_dir = RESHADE_LEGACY_STAGING_DIR / version
    if _reshade_engine_cached(engine_dir):
        return engine_dir
    url = f"https://reshade.me/downloads/ReShade_Setup_{version}_Addon.exe"
    return _download_and_extract_reshade_exe(url, engine_dir, task_id,
                                             label=f"ReShade {version} (Legacy)")


def ensure_reshade_nightly_engine(task_id=None) -> dict:
    """Latest main-branch CI build from nightly.link (a real zip, no stub
    exe to work around). No reliable version number exists here - track by
    comparing the downloaded zip's byte size against the last-known size,
    matching RHI's own approach, rather than inventing a fake version
    scheme. Returns {"dir": Path, "changed": bool} - "changed" tells the
    caller whether this was actually a new build vs. an already-cached one."""
    import zipfile, io
    state = load_state()
    last_sizes = state.get("reshade_nightly_sizes", {})
    changed = False
    for bitness, url in RESHADE_NIGHTLY_URLS.items():
        if task_id:
            TASKS[task_id] = {"status": "running", "progress": 10,
                              "detail": f"Downloading Nightly ReShade ({bitness}-bit)"}
        data = _gh_bytes(url, task_id)
        if len(data) < 10_000 or data[:2] != b"PK":
            raise RuntimeError("Download from nightly.link didn't look like a "
                               "real zip - the CI build may be temporarily unavailable.")
        if last_sizes.get(str(bitness)) != len(data):
            changed = True
        last_sizes[str(bitness)] = len(data)
        dll_name = f"ReShade{bitness}.dll"
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            match = next((n for n in zf.namelist() if n.endswith(dll_name)), None)
            if not match:
                raise RuntimeError(f"Nightly build didn't contain {dll_name} "
                                   "(its format may have changed)")
            RESHADE_NIGHTLY_STAGING_DIR.mkdir(parents=True, exist_ok=True)
            (RESHADE_NIGHTLY_STAGING_DIR / dll_name).write_bytes(zf.read(match))
    state["reshade_nightly_sizes"] = last_sizes
    save_state(state)
    return {"dir": RESHADE_NIGHTLY_STAGING_DIR, "changed": changed}


def list_custom_reshade_files() -> list:
    """Files the user has manually dropped into the Custom-channel folder,
    for the per-game 'which one' picker."""
    if not RESHADE_CUSTOM_DIR.is_dir():
        return []
    return sorted(p.name for p in RESHADE_CUSTOM_DIR.glob("*.dll"))


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_custom_reshade_updates() -> dict:
    """Detects when a file in the Custom ReShade folder has changed since
    it was last hashed (the user dropped in a newer build over the same
    filename) and redeploys it to every game currently installed from that
    exact file - matched by rhi_reshade_installs' 'version' field, which
    for channel=='custom' IS the source filename (see install_reshade /
    get_staged_reshade_path). No per-game action needed, unlike every other
    channel here. Port of CustomReShadeHashService.CheckAndRedeploy, minus
    its Vulkan-layer branch (Vulkan ReShade isn't supported on this port at
    all - see install_reshade's Vulkan refusal)."""
    if not RESHADE_CUSTOM_DIR.is_dir():
        return {"changed": [], "redeployed": 0}
    current = {f.name: _sha256_file(f) for f in RESHADE_CUSTOM_DIR.glob("*.dll")}
    state = load_state()
    first_run = "rhi_custom_reshade_hashes" not in state
    stored = state.get("rhi_custom_reshade_hashes", {})
    # The very first check ever just establishes the baseline (matches
    # upstream's separate EnsureInitialized step) - otherwise every file
    # that predates this feature would look "changed" the first time this
    # runs and trigger a pointless (if harmless) redeploy of identical
    # content to every custom-channel game.
    changed = set() if first_run else {name for name, h in current.items() if stored.get(name) != h}
    redeployed = 0
    if changed:
        for rec in state.get("rhi_reshade_installs", {}).values():
            if rec.get("channel") != "custom":
                continue
            fname = rec.get("version")
            if fname not in changed:
                continue
            src = RESHADE_CUSTOM_DIR / fname
            dest = Path(rec["path"])
            if not src.is_file() or not dest.parent.is_dir():
                continue
            try:
                shutil.copy2(src, dest)
                redeployed += 1
            except OSError:
                pass
    # Always saved, even on a partial redeploy failure - the files on disk
    # in the Custom folder ARE the new version regardless of whether every
    # game got updated, so re-comparing against the old hash next time
    # would be wrong.
    state["rhi_custom_reshade_hashes"] = current
    save_state(state)
    return {"changed": sorted(changed), "redeployed": redeployed}


def _identify_dxgi_file(path) -> str:
    """Is an existing dxgi.dll ours (ReShade/OptiScaler/DXVK), or something
    foreign? Positive evidence only, never guessed from size alone - port
    of RHI's IdentifyDxgiFile. Signature scans run regardless of file size
    (OptiScaler's real release is ~25MB - a size gate ahead of the scan
    would wrongly call it "unknown" and let a later ReShade/DXVK install
    clobber it as foreign); the 15MB cutoff only applies to the ReShade-
    specific staged-size-comparison fallback below, which really is
    ReShade-only and safe to skip for anything that large."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return "unknown"
    try:
        data = path.read_bytes()
    except OSError:
        return "unknown"
    if b"ReShade" in data and (b"reshade.me" in data or b"crosire" in data):
        return "reshade"
    if b"OptiScaler" in data:
        return "optiscaler"
    if b"dxvk" in data or b"DXVK_" in data:
        return "dxvk"
    if size > 15_000_000:
        return "unknown"          # far too big to be a staged ReShade build
    search_dirs = ([RESHADE_NIGHTLY_STAGING_DIR, RESHADE_CUSTOM_DIR]
                   + list(RESHADE_STAGING_DIR.glob("*"))
                   + list(RESHADE_NORMAL_STAGING_DIR.glob("*"))
                   + list(RESHADE_LEGACY_STAGING_DIR.glob("*")))
    for d in search_dirs:
        if not d.is_dir():
            continue
        for staged in d.glob("*.dll"):
            if staged.stat().st_size == size:
                return "reshade"
    return "unknown"


def _backup_foreign_dll(path) -> None:
    """If an existing DLL at this target isn't ours - not ReShade, and (for
    a DXVK-managed filename like d3d9.dll/d3d11.dll, which ReShade can also
    be installed as on DX9/DX8-only games) not DXVK either, since the two
    coexist there rather than one backing up the other - rename it aside as
    '.original' instead of overwriting it. Refreshes an existing backup with
    the current file rather than discarding it: the foreign DLL may have
    been updated (e.g. a game patch) since the last time this ran, and the
    old backup would otherwise silently go stale. Port of RHI's
    BackupForeignDll."""
    path = Path(path)
    if not path.is_file():
        return
    if _identify_dxgi_file(path) in ("reshade", "dxvk"):
        return
    backup = path.with_name(path.name + ".original")
    backup.unlink(missing_ok=True)
    path.rename(backup)


def _restore_foreign_dll(path) -> None:
    """Reverse of _backup_foreign_dll, called on removal."""
    path = Path(path)
    backup = path.with_name(path.name + ".original")
    if backup.is_file() and not path.exists():
        backup.rename(path)


def get_staged_reshade_path(channel, bitness, legacy_version=None,
                            custom_filename=None, task_id=None) -> tuple:
    """Resolves (source_path, version_label) for the requested channel/
    bitness, downloading/extracting as needed. version_label is what gets
    recorded in state and shown in the UI - a real version for Stable/
    No Addons/Legacy, a date-stamped tag for Nightly (no reliable version
    number exists there), or the filename itself for Custom."""
    dll_name = f"ReShade{bitness}.dll"
    if channel == "stable":
        info = reshade_latest()
        engine_dir = ensure_reshade_engine(info["version"], info["url"], task_id=task_id)
        return engine_dir / dll_name, info["version"]
    if channel == "no_addons":
        info = reshade_no_addons_latest()
        engine_dir = ensure_reshade_no_addons_engine(info["version"], info["url"], task_id=task_id)
        return engine_dir / dll_name, info["version"]
    if channel == "legacy":
        if not legacy_version:
            raise RuntimeError("Pick a Legacy version first.")
        engine_dir = ensure_reshade_legacy_engine(legacy_version, task_id=task_id)
        return engine_dir / dll_name, legacy_version
    if channel == "nightly":
        result = ensure_reshade_nightly_engine(task_id=task_id)
        return result["dir"] / dll_name, time.strftime("nightly-%Y-%m-%d", time.gmtime())
    if channel == "custom":
        if not custom_filename:
            files = list_custom_reshade_files()
            if not files:
                raise RuntimeError(f"No files in the Custom ReShade folder "
                                   f"({RESHADE_CUSTOM_DIR}) - drop a .dll there first.")
            custom_filename = files[0]
        p = RESHADE_CUSTOM_DIR / custom_filename
        if not p.is_file():
            raise RuntimeError(f"Custom ReShade file not found: {custom_filename}")
        return p, custom_filename
    raise RuntimeError(f"Unknown ReShade channel: {channel}")


def scan_game_reshade(appid, install_path, exe_path=None) -> dict:
    """ReShade status for one game: detected graphics API/bitness (from the
    best-guess exe, or the exe an existing install actually used - so the
    status display doesn't keep pointing at the wrong exe forever after a
    manual exe-override install corrected it), whatever PCC has on record,
    and whether an update is available (installed file's size no longer
    matches what's staged - Stable/No-Addons only; Legacy/Custom are
    pinned/user-managed by design, and Nightly's check would require a
    network fetch on every status poll, too expensive to do here)."""
    state = load_state()
    rec = state.get("rhi_reshade_installs", {}).get(str(appid))
    if not exe_path and rec and rec.get("exe"):
        exe_path = rec["exe"]
    exe = Path(exe_path) if exe_path else _find_game_exe(install_path)
    detected = detect_game_graphics_api(exe) if exe else {"bitness": None, "api": None}
    display = describe_graphics_api(detected["api"], exe) if exe else {"label": None, "inferred": False}
    result = {"exe": str(exe) if exe else None, "detected_api": detected["api"],
             "detected_api_display": display["label"], "detected_api_inferred": display["inferred"],
             "detected_bitness": detected["bitness"], "installed": False,
             "update_available": False,
             "builds": detect_game_builds(install_path),
             "custom_files": list_custom_reshade_files()}
    if rec:
        p = Path(rec["path"])
        channel = rec.get("channel", "stable")
        result.update({"installed": p.is_file(), "path": rec["path"],
                       "channel": channel, "version": rec.get("version")})
        if p.is_file() and channel in ("stable", "no_addons"):
            try:
                latest = (reshade_latest() if channel == "stable"
                         else reshade_no_addons_latest())
                staging = RESHADE_STAGING_DIR if channel == "stable" else RESHADE_NORMAL_STAGING_DIR
                engine_dir = staging / latest["version"]
                staged64 = engine_dir / "ReShade64.dll"
                staged32 = engine_dir / "ReShade32.dll"
                sz = p.stat().st_size
                known_sizes = {f.stat().st_size for f in (staged64, staged32) if f.is_file()}
                result["update_available"] = bool(known_sizes) and sz not in known_sizes
            except Exception:
                pass
    return result


def install_reshade(appid, install_path, exe_override=None, channel="stable",
                    legacy_version=None, custom_filename=None, task_id=None) -> dict:
    """Installs ReShade for one game: detects the exe/graphics API/bitness,
    and installs under the API-correct filename - dxgi.dll for DX11/12 (the
    common case), but d3d9.dll/d3d8.dll/opengl32.dll for DX9/DX8/OpenGL-only
    games, where ReShade never gets a chance to hook via dxgi.dll at all
    since those games don't load it. Refuses to overwrite a foreign DLL at
    that target (backs it up as .original instead). Port of RHI's
    ResolveAutoReShadeFilename + AuxInstallService install flow."""
    if channel not in RESHADE_CHANNELS:
        raise RuntimeError(f"Unknown ReShade channel: {channel}")
    exe = Path(exe_override).expanduser() if exe_override else _find_game_exe(install_path)
    if not exe or not exe.is_file():
        raise RuntimeError("Couldn't find the game's .exe under its install folder — "
                           "point Command Center at it manually.")
    detected = detect_game_graphics_api(exe)
    if detected["api"] in ("vulkan",):
        raise RuntimeError("Vulkan ReShade install isn't supported yet on Linux - "
                           "this needs Proton-prefix-specific work, not just a "
                           "file copy. DX9-12 games work normally.")
    bitness = detected["bitness"] or 64
    _, regular, delay = pe_imports(exe)
    rs_filename = resolve_auto_reshade_filename(_detect_all_graphics_apis(regular, delay))
    target = exe.parent / rs_filename

    src_dll, version = get_staged_reshade_path(
        channel, bitness, legacy_version=legacy_version,
        custom_filename=custom_filename, task_id=task_id)

    _backup_foreign_dll(target)
    shutil.copy2(src_dll, target)

    state = load_state()
    installs = state.setdefault("rhi_reshade_installs", {})
    installs[str(appid)] = {"path": str(target), "channel": channel,
                            "version": version, "bitness": bitness,
                            "exe": str(exe),
                            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    save_state(state)
    if task_id:
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Installed ReShade {version}",
                          "result": {"version": version}}
    return {"installed": True, "path": str(target), "version": version,
            "api": detected["api"], "bitness": bitness}


def _install_reshade_task(task_id, appid, install_path, exe, channel="stable",
                          legacy_version=None, custom_filename=None) -> None:
    try:
        install_reshade(appid, install_path, exe_override=exe, channel=channel,
                        legacy_version=legacy_version, custom_filename=custom_filename,
                        task_id=task_id)
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


def remove_reshade(appid) -> dict:
    """Deletes the ReShade install tracked for this game (whichever
    API-correct filename it was installed under) and restores whatever
    foreign DLL it backed up at that target, if any."""
    state = load_state()
    installs = state.get("rhi_reshade_installs", {})
    rec = installs.pop(str(appid), None)
    if not rec:
        raise RuntimeError("No ReShade install tracked for this game.")
    target = Path(rec["path"])
    target.unlink(missing_ok=True)
    save_state(state)
    _restore_foreign_dll(target)
    return {"removed": True}


def is_re_engine_game(install_path) -> bool:
    """RE Engine's signature file - present in every RE Engine game's root
    (Resident Evil, Monster Hunter Wilds, DMC5, SF6, and similar). Shallow
    scan (top 2 levels) since it's always near the game's install root."""
    base = Path(install_path)
    if not base.is_dir():
        return False
    for depth, (dirpath, dirnames, filenames) in enumerate(os.walk(base)):
        if "re_chunk_000.pak" in filenames:
            return True
        if depth >= 1:
            dirnames[:] = []   # don't recurse past depth 2
    return False


def re_framework_latest() -> dict:
    """Latest REFramework-nightly release tag, 6h cached."""
    state = load_state()
    cache = state.get("re_framework_latest")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    releases = _gh_json(RE_FRAMEWORK_RELEASES_API)
    if not releases:
        raise RuntimeError("Couldn't reach REFramework-nightly's releases")
    tag = releases[0]["tag_name"]
    data = {"version": tag, "url": RE_FRAMEWORK_ZIP_URL}
    state["re_framework_latest"] = {"ts": now, "data": data}
    save_state(state)
    return data


def install_re_framework(appid, install_path, task_id=None) -> dict:
    """Downloads REFramework.zip and drops dinput8.dll directly in the game's
    install root - a different hook slot than ReShade's dxgi.dll, so the two
    coexist without wrapping each other."""
    import zipfile, io
    install_path = Path(install_path)
    if not install_path.is_dir():
        raise RuntimeError("Install path not found.")
    info = re_framework_latest()
    if task_id:
        TASKS[task_id] = {"status": "running", "progress": 10,
                          "detail": f"Downloading REFramework {info['version']}"}
    data = _gh_bytes(info["url"], task_id)
    if task_id:
        TASKS[task_id]["detail"] = "Extracting"
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith("dinput8.dll")]
        if not names:
            raise RuntimeError("REFramework.zip didn't contain dinput8.dll "
                               "(its format may have changed)")
        target = install_path / "dinput8.dll"
        target.write_bytes(zf.read(names[0]))

    state = load_state()
    installs = state.setdefault("rhi_reframework_installs", {})
    installs[str(appid)] = {"path": str(target), "version": info["version"],
                            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    save_state(state)
    if task_id:
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Installed REFramework {info['version']}",
                          "result": {"version": info["version"]}}
    return {"installed": True, "path": str(target), "version": info["version"]}


def _install_re_framework_task(task_id, appid, install_path) -> None:
    try:
        install_re_framework(appid, install_path, task_id=task_id)
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


def remove_re_framework(appid) -> dict:
    state = load_state()
    installs = state.get("rhi_reframework_installs", {})
    rec = installs.pop(str(appid), None)
    if not rec:
        raise RuntimeError("No RE Framework install tracked for this game.")
    dll_path = Path(rec["path"])
    backup_path = dll_path.with_name(dll_path.name + PD_UPSCALER_BACKUP_SUFFIX)
    if rec.get("version") == "PD-Upscaler" and backup_path.is_file():
        # Removing a PD-Upscaler build directly (not via OptiScaler removal)
        # must still restore the standard build it replaced, not just
        # delete and orphan the backup.
        dll_path.unlink(missing_ok=True)
        backup_path.rename(dll_path)
    else:
        dll_path.unlink(missing_ok=True)
    save_state(state)
    return {"removed": True}


PD_UPSCALER_DOWNLOAD_BASE = ("https://nightly.link/praydog/REFramework/"
                             "workflows/dev-release/pd-upscaler/")
PD_UPSCALER_BACKUP_SUFFIX = ".rhi_standard_backup"


def pd_upscaler_artifact_for_game(game_name) -> str | None:
    """The PD-Upscaler REFramework build name for this game (e.g. "RE2"),
    for the small set of RE Engine titles that have a dedicated
    OptiScaler-compatible REFramework build - from RHI's manifest.json's
    pdUpscalerGames map. None for every other game."""
    if not game_name:
        return None
    try:
        games = rhi_manifest().get("pdUpscalerGames") or {}
    except Exception:
        return None
    return games.get(game_name)


def install_pd_upscaler_re_framework(appid, install_path, artifact_name, task_id=None) -> dict:
    """Swaps in the PD-Upscaler build of RE Framework: a special
    OptiScaler-compatible dinput8.dll build for a small set of RE Engine
    games (RE2/RE3/RE4/RE7/RE8, per manifest.json's pdUpscalerGames).
    Backs up the standard dinput8.dll first (a no-op if a backup already
    exists - matches upstream's overwrite:false first-backup-wins
    semantics), then installs the pd-upscaler build in its place. The real
    download is a nested zip: an outer nightly.link wrapper containing an
    inner {artifact_name}.zip containing dinput8.dll. Port of
    REFrameworkService.InstallPdUpscalerAsync."""
    import zipfile, io
    install_path = Path(install_path)
    dest_dll = install_path / "dinput8.dll"
    backup_path = dest_dll.with_name(dest_dll.name + PD_UPSCALER_BACKUP_SUFFIX)
    url = f"{PD_UPSCALER_DOWNLOAD_BASE}{artifact_name}.zip"
    outer = _gh_bytes(url, task_id)
    with zipfile.ZipFile(io.BytesIO(outer)) as outer_zf:
        inner_name = next((n for n in outer_zf.namelist()
                           if n.lower() == f"{artifact_name.lower()}.zip"), None)
        inner_name = inner_name or next((n for n in outer_zf.namelist()
                                         if n.lower().endswith(".zip")), None)
        if not inner_name:
            raise RuntimeError(f"PD-Upscaler download for {artifact_name} had no inner zip "
                               "(its format may have changed)")
        inner_bytes = outer_zf.read(inner_name)
    with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner_zf:
        member = next((n for n in inner_zf.namelist()
                       if n.lower().endswith("dinput8.dll")), None)
        if not member:
            raise RuntimeError(f"PD-Upscaler build for {artifact_name} didn't contain "
                               "dinput8.dll (its format may have changed)")
        dll_bytes = inner_zf.read(member)

    if dest_dll.is_file() and not backup_path.is_file():
        shutil.copy2(dest_dll, backup_path)
    dest_dll.write_bytes(dll_bytes)

    state = load_state()
    rec = state.setdefault("rhi_reframework_installs", {}).setdefault(str(appid), {})
    rec.update({"path": str(dest_dll), "version": "PD-Upscaler",
               "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    save_state(state)
    return {"installed": True, "version": "PD-Upscaler"}


def restore_standard_re_framework(appid, install_path) -> dict:
    """Reverses install_pd_upscaler_re_framework: restores the backed-up
    standard dinput8.dll, if one exists, and un-marks the tracked install's
    version as PD-Upscaler. Port of
    REFrameworkService.RestoreStandardREFramework."""
    install_path = Path(install_path)
    dest_dll = install_path / "dinput8.dll"
    backup_path = dest_dll.with_name(dest_dll.name + PD_UPSCALER_BACKUP_SUFFIX)
    if not backup_path.is_file():
        return {"restored": False}
    dest_dll.unlink(missing_ok=True)
    backup_path.rename(dest_dll)
    state = load_state()
    rec = state.get("rhi_reframework_installs", {}).get(str(appid))
    if rec:
        try:
            rec["version"] = re_framework_latest().get("version", "unknown")
        except Exception:
            rec["version"] = "unknown"
        save_state(state)
    return {"restored": True}


def scan_re_framework(appid, install_path) -> dict:
    is_re_engine = is_re_engine_game(install_path)
    state = load_state()
    rec = state.get("rhi_reframework_installs", {}).get(str(appid))
    result = {"is_re_engine": is_re_engine, "installed": False, "update_available": False}
    if rec:
        p = Path(rec["path"])
        result.update({"installed": p.is_file(), "path": rec["path"],
                       "version": rec.get("version"),
                       "pd_upscaler": rec.get("version") == "PD-Upscaler"})
        # A PD-Upscaler build isn't on the nightly version scheme at all -
        # comparing it against re_framework_latest() would always show
        # "update available" (a false positive), and reinstalling standard
        # RE Framework over it would silently discard the OptiScaler-
        # compatible build without restoring it properly. No update check
        # for this case.
        if p.is_file() and rec.get("version") != "PD-Upscaler":
            try:
                result["update_available"] = re_framework_latest()["version"] != rec.get("version")
            except Exception:
                pass
    return result


# --------------------------------------------------------------------------
# RHI port: shader pack management
# --------------------------------------------------------------------------
# Ported verbatim from RHI's ShaderPackService.cs DefaultPacks array - this
# is hardcoded data in RHI too, not fetched from a remote catalog at
# runtime. kind is "gh_release" (GitHub Releases API, picks the first
# release asset matching asset_ext) or "direct_url" (a static branch-zip
# URL). requires lists pack ids that get pulled in automatically (BFS
# dependency expansion) when this pack is selected.
RESHADE_SHADER_PACKS = [
    {"id": "Lilium", "name": "Lilium HDR Shaders", "kind": "gh_release",
     "url": "https://api.github.com/repos/EndlesslyFlowering/ReShade_HDR_shaders/releases/latest",
     "asset_ext": ".7z", "category": "essential",
     "description": "HDR tone mapping and inverse tone mapping shaders"},
    {"id": "CrosireMaster", "name": "crosire reshade-shaders (master)", "kind": "direct_url",
     "url": "https://github.com/crosire/reshade-shaders/archive/refs/heads/master.zip",
     "category": "recommended",
     "description": "Official ReShade standard effects - full master branch"},
    {"id": "CrosireLegacy", "name": "crosire reshade-shaders (legacy)", "kind": "direct_url",
     "url": "https://github.com/crosire/reshade-shaders/archive/refs/heads/legacy.zip",
     "category": "extra", "requires": ["CrosireMaster"],
     "description": "Legacy ReShade effects (older versions removed from master)"},
    {"id": "PumboAutoHDR", "name": "PumboAutoHDR", "kind": "gh_release",
     "url": "https://api.github.com/repos/Filoppi/PumboAutoHDR/releases/latest",
     "asset_ext": ".zip", "category": "recommended",
     "description": "Automatic HDR conversion for SDR games"},
    {"id": "SmolbbsoopShaders", "name": "smolbbsoop shaders", "kind": "direct_url",
     "url": "https://github.com/smolbbsoop/smolbbsoopshaders/archive/refs/heads/main.zip",
     "category": "extra", "description": "HDR utility shaders and effects"},
    {"id": "MaxG2DSimpleHDR", "name": "MaxG2D Simple HDR Shaders", "kind": "direct_url",
     "url": "https://github.com/MaxG2D/ReshadeSimpleHDRShaders/archive/refs/heads/main.zip",
     "category": "recommended", "description": "Simple HDR bloom, lens flare, and tone mapping"},
    {"id": "ClshortfuseShaders", "name": "clshortfuse ReShade shaders", "kind": "direct_url",
     "url": "https://github.com/clshortfuse/reshade-shaders/archive/refs/heads/main.zip",
     "category": "recommended", "description": "HDR and color correction shaders for RenoDX"},
    {"id": "PotatoFX", "name": "potatoFX (CreepySasquatch)", "kind": "direct_url",
     "url": "https://github.com/CreepySasquatch/potatoFX/archive/refs/heads/main.zip",
     "category": "extra", "description": "Lightweight post-processing effects for low-end hardware"},
    {"id": "Azen", "name": "Azen by Zenteon", "kind": "direct_url",
     "url": "https://github.com/Zenteon/Azen/archive/refs/heads/main.zip",
     "category": "extra", "requires": ["SmolbbsoopShaders"],
     "description": "Zenteon's casual shader collection - experimental effects"},
    {"id": "SweetFX", "name": "SweetFX by CeeJay.dk", "kind": "direct_url",
     "url": "https://github.com/CeeJayDK/SweetFX/archive/refs/heads/master.zip",
     "category": "extra", "description": "Classic color grading, sharpening, and bloom effects"},
    {"id": "OtisFX", "name": "OtisFX by Otis_Inf", "kind": "direct_url",
     "url": "https://github.com/FransBouma/OtisFX/archive/refs/heads/master.zip",
     "category": "extra", "description": "Cinematic depth of field, light rays, and camera effects"},
    {"id": "Depth3D", "name": "Depth3D by BlueSkyDefender", "kind": "direct_url",
     "url": "https://github.com/BlueSkyDefender/Depth3D/archive/refs/heads/master.zip",
     "category": "extra", "description": "Stereoscopic 3D and depth-based visual effects"},
    {"id": "DaodanShaders", "name": "reshade-shaders by Daodan", "kind": "direct_url",
     "url": "https://github.com/Daodan317081/reshade-shaders/archive/refs/heads/master.zip",
     "category": "extra", "description": "Comic, crosshatch, and artistic style effects"},
    {"id": "BrussellShaders", "name": "Shaders by brussell", "kind": "direct_url",
     "url": "https://github.com/brussell1/Shaders/archive/refs/heads/master.zip",
     "category": "extra", "description": "Halftone, sketch, and stylized rendering effects"},
    {"id": "FubaxShaders", "name": "fubax-shaders by Fubaxiusz", "kind": "direct_url",
     "url": "https://github.com/Fubaxiusz/fubax-shaders/archive/refs/heads/master.zip",
     "category": "extra", "description": "VR-friendly lens distortion and chromatic aberration"},
    {"id": "qUINT", "name": "qUINT by Marty McFly", "kind": "direct_url",
     "url": "https://github.com/martymcmodding/qUINT/archive/refs/heads/master.zip",
     "category": "extra", "description": "MXAO, ADOF, lightroom, and screen-space reflections"},
    {"id": "AlucardDH", "name": "dh-reshade-shaders by AlucardDH", "kind": "direct_url",
     "url": "https://github.com/AlucardDH/dh-reshade-shaders/archive/refs/heads/master.zip",
     "category": "extra", "description": "Ambient occlusion, undither, and color enhancement"},
    {"id": "WarpFX", "name": "Warp-FX by Radegast", "kind": "direct_url",
     "url": "https://github.com/Radegast-FFXIV/Warp-FX/archive/refs/heads/master.zip",
     "category": "extra", "description": "Screen warp, swirl, and distortion effects"},
    {"id": "Prod80", "name": "Color effects by prod80", "kind": "direct_url",
     "url": "https://github.com/prod80/prod80-ReShade-Repository/archive/refs/heads/master.zip",
     "category": "extra", "description": "Professional color grading, curves, and tone tools"},
    {"id": "CorgiFX", "name": "CorgiFX by originalnicodr", "kind": "direct_url",
     "url": "https://github.com/originalnicodr/CorgiFX/archive/refs/heads/master.zip",
     "category": "extra", "description": "Screenshot and virtual photography tools"},
    {"id": "InsaneShaders", "name": "Insane-Shaders by Lord of Lunacy", "kind": "direct_url",
     "url": "https://github.com/LordOfLunacy/Insane-Shaders/archive/refs/heads/master.zip",
     "category": "extra", "description": "Advanced dithering, fog removal, and edge detection"},
    {"id": "CobraFX", "name": "CobraFX by SirCobra", "kind": "direct_url",
     "url": "https://github.com/LordKobra/CobraFX/archive/refs/heads/master.zip",
     "category": "extra", "description": "Gravity, auto-focus, and real-time ray tracing effects"},
    {"id": "AstrayFX", "name": "AstrayFX by BlueSkyDefender", "kind": "direct_url",
     "url": "https://github.com/BlueSkyDefender/AstrayFX/archive/refs/heads/master.zip",
     "category": "extra", "description": "Depth-based fog, haze, and atmospheric effects"},
    {"id": "CRTRoyale", "name": "CRT-Royale-ReShade by akgunter", "kind": "direct_url",
     "url": "https://github.com/akgunter/crt-royale-reshade/archive/refs/heads/master.zip",
     "category": "extra", "description": "CRT monitor simulation with phosphor and scanline emulation"},
    {"id": "RSRetroArch", "name": "RSRetroArch by Matsilagi", "kind": "direct_url",
     "url": "https://github.com/Matsilagi/RSRetroArch/archive/refs/heads/main.zip",
     "category": "extra", "description": "RetroArch shader ports - CRT, LCD, and retro filters"},
    {"id": "VRToolkit", "name": "VRToolkit by retroluxfilm", "kind": "direct_url",
     "url": "https://github.com/retroluxfilm/reshade-vrtoolkit/archive/refs/heads/main.zip",
     "category": "extra", "description": "Sharpening and clarity tools optimized for VR headsets"},
    {"id": "FGFX", "name": "FGFX by AlexTuduran", "kind": "direct_url",
     "url": "https://github.com/AlexTuduran/FGFX/archive/refs/heads/main.zip",
     "category": "extra", "description": "Film grain, multi-LUT, and cinematic post-processing"},
    {"id": "CShade", "name": "CShade by papadanku", "kind": "direct_url",
     "url": "https://github.com/papadanku/CShade/archive/refs/heads/main.zip",
     "category": "extra", "description": "Optical flow, motion blur, and convolution effects"},
    {"id": "iMMERSE", "name": "iMMERSE by Marty McFly", "kind": "direct_url",
     "url": "https://github.com/martymcmodding/iMMERSE/archive/refs/heads/main.zip",
     "category": "extra", "description": "Next-gen RTGI, MXAO, and anti-aliasing suite"},
    {"id": "VortShaders", "name": "vort_Shaders by vortigern11", "kind": "direct_url",
     "url": "https://github.com/vortigern11/vort_Shaders/archive/refs/heads/main.zip",
     "category": "extra", "description": "Sharpening, color correction, and depth effects"},
    {"id": "BXShade", "name": "BX-Shade by BarricadeMKXX", "kind": "direct_url",
     "url": "https://github.com/liuxd17thu/BX-Shade/archive/refs/heads/main.zip",
     "category": "extra", "description": "Bloom, exposure, and color enhancement effects"},
    {"id": "SHADERDECK", "name": "SHADERDECK by TreyM", "kind": "direct_url",
     "url": "https://github.com/IAmTreyM/SHADERDECK/archive/refs/heads/main.zip",
     "category": "extra", "description": "Curated collection of color and lighting effects"},
    {"id": "METEOR", "name": "METEOR by Marty McFly", "kind": "direct_url",
     "url": "https://github.com/martymcmodding/METEOR/archive/refs/heads/main.zip",
     "category": "extra", "description": "Advanced denoiser and image reconstruction"},
    {"id": "AnnReShade", "name": "Ann-ReShade by Anastasia Bouwsma", "kind": "direct_url",
     "url": "https://github.com/AnastasiaGals/Ann-ReShade/archive/refs/heads/main.zip",
     "category": "extra", "description": "Soft bloom, color grading, and ambient light presets"},
    {"id": "ZenteonFX", "name": "ZenteonFX Shaders by Zenteon", "kind": "direct_url",
     "url": "https://github.com/Zenteon/ZenteonFX/archive/refs/heads/main.zip",
     "category": "extra", "description": "Global illumination, SSR, and path tracing effects"},
    {"id": "GShadeShaders", "name": "GShade-Shaders by Marot", "kind": "direct_url",
     "url": "https://github.com/Mortalitas/GShade-Shaders/archive/refs/heads/master.zip",
     "category": "extra", "description": "Large collection of community shaders from GShade"},
    {"id": "PthoFX", "name": "Ptho-FX by PthoEastCoast", "kind": "direct_url",
     "url": "https://github.com/PthoEastCoast/Ptho-FX/archive/refs/heads/main.zip",
     "category": "extra", "description": "Cinematic color grading and film emulation"},
    {"id": "Anagrama", "name": "The Anagrama Collection by nullfractal", "kind": "direct_url",
     "url": "https://github.com/nullfrctl/reshade-shaders/archive/refs/heads/main.zip",
     "category": "extra", "description": "Artistic and experimental visual effects"},
    {"id": "BarbatosShaders", "name": "reshade-shaders by Barbatos", "kind": "direct_url",
     "url": "https://github.com/BarbatosBachiko/Reshade-Shaders/archive/refs/heads/main.zip",
     "category": "extra", "description": "Ambient occlusion, bloom, and color effects"},
    {"id": "BFBFX", "name": "BFBFX by yaboi BFB", "kind": "direct_url",
     "url": "https://github.com/yplebedev/BFBFX/archive/refs/heads/main.zip",
     "category": "extra", "description": "Stylized and artistic post-processing effects"},
    {"id": "Rendepth", "name": "Rendepth by cybereality", "kind": "direct_url",
     "url": "https://github.com/outmode/rendepth-reshade/archive/refs/heads/main.zip",
     "category": "extra", "description": "Depth-based 3D rendering and stereo effects"},
    {"id": "CropAndResize", "name": "Crop and Resize by P0NYSLAYSTATION", "kind": "direct_url",
     "url": "https://github.com/P0NYSLAYSTATION/Scaling-Shaders/archive/refs/heads/main.zip",
     "category": "extra", "description": "Screen cropping, scaling, and aspect ratio tools"},
    {"id": "FXShaders", "name": "FXShaders by luluco250", "kind": "direct_url",
     "url": "https://github.com/luluco250/FXShaders/archive/refs/heads/master.zip",
     "category": "extra", "description": "Bloom, grain, dithering, and utility shader library"},
    {"id": "LumeniteFX", "name": "LumeniteFX by Kaido", "kind": "direct_url",
     "url": "https://github.com/umar-afzaal/LumeniteFX/archive/refs/heads/mainline.zip",
     "category": "extra", "description": "Lighting, bloom, and atmospheric glow effects"},
    {"id": "NNShaders", "name": "NN-Shaders by Sarenya", "kind": "direct_url",
     "url": "https://github.com/Sarenya/NN-Shaders/archive/refs/heads/master.zip",
     "category": "extra", "description": "Neural network-based image processing shaders"},
    {"id": "QdOledAplFixer", "name": "QD-OLED APL Fixer by mspeedo", "kind": "direct_url",
     "url": "https://github.com/mspeedo/QD-OLED-APL-FIXER/archive/refs/heads/main.zip",
     "category": "extra", "description": "HDR brightness boost to compensate for QD-OLED ABL dimming"},
    {"id": "GlamaryeFX", "name": "Glamarye Fast Effects by rj200", "kind": "direct_url",
     "url": "https://github.com/rj200/Glamarye_Fast_Effects_for_ReShade/archive/refs/heads/main.zip",
     "category": "extra",
     "description": "Lightweight all-in-one: sharpening, AO, indirect lighting, color correction"},
    {"id": "LumaBoost", "name": "LumaBoost by Valadore", "kind": "direct_url",
     "url": "https://github.com/Valadore/LumaBoost/archive/refs/heads/main.zip",
     "category": "extra",
     "description": "OLED ABL compensation - dynamically lifts midtones"},
    {"id": "RenoFXHDRToolkit", "name": "RenoFX HDR Toolkit by OopyDoopy", "kind": "direct_url",
     "url": "https://github.com/clshortfuse/renofx/archive/refs/heads/main.zip",
     "category": "recommended",
     "description": "SDR to HDR conversion, tone mapping, and color grading"},
]
RESHADE_SHADER_PACKS_BY_ID = {p["id"]: p for p in RESHADE_SHADER_PACKS}
# Shader files that fail to compile and should never be extracted or
# deployed - matched against the filename (leaf) of each archive entry.
SHADER_EXCLUDED_FILES = {"BX_XIV_ChromakeyPlus.fx", "GrainSpread.fx",
                         "NTSCCustom.fx", "NTSC_XOT.fx"}
RESHADE_SHADERS_STAGE_DIR = RHI_DATA_DIR / "shaders" / "Shaders"
RESHADE_TEXTURES_STAGE_DIR = RHI_DATA_DIR / "shaders" / "Textures"
GAME_RESHADE_SHADERS_DIR = "reshade-shaders"
GAME_RESHADE_SHADERS_ORIGINAL = "reshade-shaders-original"
RESHADE_SHADERS_MANAGED_MARKER = "Managed by Proton Command Center.txt"


def _expand_pack_dependencies(pack_ids) -> list:
    """BFS over each pack's `requires` list so selecting a pack automatically
    pulls in whatever it declares as required. Port of RHI's
    ExpandPackDependencies."""
    seen, queue = [], list(pack_ids)
    visited = set()
    while queue:
        pid = queue.pop(0)
        if pid in visited or pid not in RESHADE_SHADER_PACKS_BY_ID:
            continue
        visited.add(pid)
        seen.append(pid)
        queue.extend(RESHADE_SHADER_PACKS_BY_ID[pid].get("requires") or [])
    return seen


_SHADER_PACK_BRANCH_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/archive/refs/heads/(.+)\.zip$")


def _shader_pack_latest_signal(pack, release=None) -> str | None:
    """A cheap 'has this pack changed upstream' signal: the release tag for
    a gh_release pack (pass the already-fetched release dict to avoid a
    second API call when ensure_shader_pack just fetched it), or the latest
    commit SHA of the tracked branch for a direct_url github
    archive/refs/heads/<branch>.zip pack (GitHub's commits API returns one
    small JSON object, not the whole archive - cheap to poll). Returns None
    when no such signal can be determined (e.g. a non-GitHub direct_url) -
    meaning "can't check", not "no update"."""
    if pack["kind"] == "gh_release":
        try:
            release = release if release is not None else _gh_json(pack["url"])
        except Exception:
            return None
        return release.get("tag_name") or release.get("published_at")
    if pack["kind"] == "direct_url":
        m = _SHADER_PACK_BRANCH_URL_RE.match(pack["url"])
        if not m:
            return None
        owner, repo, branch = m.groups()
        try:
            commit = _gh_json(f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}")
        except Exception:
            return None
        return commit.get("sha")
    return None


def check_shader_pack_update(pack_id, force=False) -> bool:
    """Whether pack_id has a newer version available than what's cached, 6h
    cached like every other update check in this file - checking on every
    catalog fetch would burn through GitHub's API rate limit fast across a
    40+ pack catalog. False (not just "unknown") for a pack that isn't
    cached yet - nothing to compare against.

    The signal isn't fetched at download time (ensure_shader_pack's normal
    path stays purely local-disk-check, no extra network call on every
    install/deploy) - the first time a downloaded pack is checked here, the
    live signal becomes its baseline and this reports no update yet, since
    there's nothing to compare that first read against. Later checks then
    compare against that stored baseline; the baseline only moves forward
    when the pack is actually re-fetched (ensure_shader_pack(force=True))."""
    pack = RESHADE_SHADER_PACKS_BY_ID.get(pack_id)
    if not pack:
        return False
    state = load_state()
    cache = state.get("rhi_shader_packs", {}).get(pack_id)
    if not cache:
        return False
    check_cache = state.setdefault("rhi_shader_pack_update_checks", {})
    entry = check_cache.get(pack_id)
    now = time.time()
    if not force and entry and now - entry.get("ts", 0) < 21600:
        return entry.get("update_available", False)
    latest = _shader_pack_latest_signal(pack)
    baseline = cache.get("signal")
    if baseline is None:
        cache["signal"] = latest
        update_available = False
    else:
        update_available = bool(latest and latest != baseline)
    check_cache[pack_id] = {"ts": now, "update_available": update_available}
    save_state(state)
    return update_available


def ensure_shader_pack(pack_id, task_id=None, force=False) -> list:
    """Downloads/extracts one shader pack into its own ID-named subfolder of
    the shared staging tree, recording every extracted file's staging-
    relative path in state (source of truth for later pruning) plus a
    version "signal" (see _shader_pack_latest_signal) used by
    check_shader_pack_update. Skips the download if the recorded files are
    all still present on disk and force isn't set - force=True (an explicit
    user-triggered update, not the normal deploy path) always re-fetches
    and prunes any previously-extracted file the new archive no longer
    contains, in case files were renamed/removed upstream. Returns the list
    of staging-relative paths (e.g. "Shaders/Lilium/HDR.fx")."""
    import zipfile, io
    pack = RESHADE_SHADER_PACKS_BY_ID.get(pack_id)
    if not pack:
        raise RuntimeError(f"Unknown shader pack: {pack_id}")
    state = load_state()
    cache = state.setdefault("rhi_shader_packs", {})
    entry = cache.get(pack_id)
    if not force and entry and entry.get("files"):
        if all((RHI_DATA_DIR / "shaders" / f).is_file() for f in entry["files"]):
            return entry["files"]
    previous_files = set(entry["files"]) if entry and entry.get("files") else set()

    if task_id:
        TASKS[task_id] = {"status": "running", "progress": 10,
                          "detail": f"Downloading {pack['name']}"}
    release = None
    if pack["kind"] == "gh_release":
        release = _gh_json(pack["url"])
        asset = next((a for a in release.get("assets", [])
                     if a["name"].lower().endswith(pack.get("asset_ext", ""))), None)
        url = asset["browser_download_url"] if asset else release.get("zipball_url")
        if not url:
            raise RuntimeError(f"No downloadable asset found for {pack['name']}")
    else:
        url = pack["url"]
    data = _gh_bytes(url, task_id)
    if task_id:
        TASKS[task_id]["detail"] = f"Extracting {pack['name']}"

    def classify(rel) -> str | None:
        """Maps one archive-relative path to its staging destination (or
        None to skip it) - shared between the zip and 7z extraction
        branches below so both follow the exact same layout rules."""
        fn = rel.rsplit("/", 1)[-1]
        if fn in SHADER_EXCLUDED_FILES:
            return None
        low = rel.lower()
        if "/shaders/" in f"/{low}":
            idx = low.find("shaders/")
            return f"Shaders/{pack_id}/{rel[idx + len('shaders/'):]}"
        elif "/textures/" in f"/{low}":
            idx = low.find("textures/")
            return f"Textures/{pack_id}/{rel[idx + len('textures/'):]}"
        elif fn.endswith((".fx", ".fxh")) and "/" not in rel:
            return f"Shaders/{pack_id}/{fn}"
        return None

    files = []
    is_7z = data[:6] == b"7z\xbc\xaf\x27\x1c"
    if is_7z:
        # Some packs (e.g. Lilium HDR's real GitHub release) ship as .7z,
        # not .zip, despite most of this catalog being plain zips - sniff
        # the real magic bytes rather than trusting the catalog's asset_ext
        # metadata alone, so a wrong/missing extension hint can't silently
        # feed a .7z into zipfile and blow up with "File is not a zip file"
        # (the exact bug this replaced, confirmed live against Lilium).
        seven_zip = _find_7z_binary()
        if not seven_zip:
            raise RuntimeError(
                f"{pack['name']} ships as a .7z archive - install the 7-Zip CLI first "
                "(Arch: `sudo pacman -S 7zip`, other distros: the `p7zip` package) "
                "then try again.")
        with tempfile.TemporaryDirectory(prefix="pcc_shaderpack_") as tmp:
            tmp = Path(tmp)
            archive = tmp / "pack.7z"
            archive.write_bytes(data)
            extract_dir = tmp / "extracted"
            extract_dir.mkdir()
            proc = subprocess.run([seven_zip, "x", str(archive), f"-o{extract_dir}", "-y"],
                                  capture_output=True, text=True, timeout=180)
            if proc.returncode != 0:
                raise RuntimeError(f"7z extraction failed: {proc.stderr.strip()[:300]}")
            for p in extract_dir.rglob("*"):
                if not p.is_file():
                    continue
                rel = str(p.relative_to(extract_dir))
                out_rel = classify(rel)
                if not out_rel:
                    continue
                dest = RHI_DATA_DIR / "shaders" / out_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(p.read_bytes())
                files.append(out_rel)
                if p.name in ("ReShade.fxh", "ReShadeUI.fxh"):
                    (RESHADE_SHADERS_STAGE_DIR / p.name).write_bytes(p.read_bytes())
    else:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            roots = {n.split("/", 1)[0] for n in names if "/" in n}
            prefix = f"{next(iter(roots))}/" if len(roots) == 1 else ""
            for n in names:
                if n.endswith("/"):
                    continue
                rel = n[len(prefix):] if prefix and n.startswith(prefix) else n
                out_rel = classify(rel)
                if not out_rel:
                    continue
                dest = RHI_DATA_DIR / "shaders" / out_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(n))
                files.append(out_rel)
                fn = rel.rsplit("/", 1)[-1]
                if fn in ("ReShade.fxh", "ReShadeUI.fxh"):
                    (RESHADE_SHADERS_STAGE_DIR / fn).write_bytes(zf.read(n))

    if not files:
        raise RuntimeError(f"{pack['name']}'s archive didn't contain any usable "
                           "shader files (its layout may have changed)")
    # Prune anything the PREVIOUS extraction staged that this one didn't -
    # only reachable on a forced update (previous_files is empty otherwise),
    # since a renamed/removed upstream file would otherwise linger in the
    # shared staging tree (and in turn stay deployed to every game using it)
    # forever.
    for stale in previous_files - set(files):
        (RHI_DATA_DIR / "shaders" / stale).unlink(missing_ok=True)
    # gh_release packs get their signal for free (release was already
    # fetched above to find the download asset) either way. direct_url
    # packs only pay for the extra commits-API call on a forced update -
    # the normal download path leaves signal unset and lets
    # check_shader_pack_update establish it lazily on first check, so
    # installing/deploying a pack never makes an extra network call beyond
    # what downloading it already needed.
    signal = (_shader_pack_latest_signal(pack, release=release)
             if force or pack["kind"] == "gh_release" else None)
    cache[pack_id] = {"files": files, "fetched_at": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "signal": signal}
    if signal is not None:
        # A real baseline was just established (or re-established, on a
        # forced update) - record "no update pending" so
        # check_shader_pack_update doesn't immediately re-fetch it and
        # treat this fresh baseline as itself being "newer than itself".
        # When signal is None (the common direct_url non-force case), leave
        # no entry here at all - check_shader_pack_update's own lazy-
        # baseline path handles that case on first real check.
        state.setdefault("rhi_shader_pack_update_checks", {})[pack_id] = {
            "ts": time.time(), "update_available": False}
    save_state(state)
    if task_id:
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"{pack['name']}: {len(files)} files"}
    return files


def get_shader_pack_catalog() -> list:
    state = load_state()
    cache = state.get("rhi_shader_packs", {})
    return [{"id": p["id"], "name": p["name"], "category": p["category"],
            "description": p["description"], "requires": p.get("requires") or [],
            "cached": p["id"] in cache,
            "update_available": check_shader_pack_update(p["id"]) if p["id"] in cache else False}
           for p in RESHADE_SHADER_PACKS]


def resolve_rhi_target_dir(appid, install_path) -> Path:
    """Resolves the directory RHI shader packs should land in for one
    game: whichever RHI mod is already installed there is authoritative
    (ReShade, then OptiScaler, then DXVK - checked in that order, matching
    exactly what that mod itself resolved, including any manual exe
    override the user gave it), otherwise the same graphics-API-aware exe
    detection ReShade itself would use to auto-install. Never the raw
    Steam install root on its own - that's wrong for any game whose real
    exe lives in a subdirectory (bin/x64/, bin/x64_dx12/, etc), which
    live-testing confirmed is common. Addons already resolved this
    correctly (Part 1f) by reading straight from the ReShade record; this
    generalizes that same fix for shader packs, which had been deploying
    to the Steam root the whole time. Checking OptiScaler/DXVK too (not
    just ReShade) matters for a game that has one of those installed but
    not ReShade - it would otherwise fall through to a fresh exe-detection
    guess instead of reusing the build the user already confirmed by
    installing something else onto it."""
    state = load_state()
    rec = state.get("rhi_reshade_installs", {}).get(str(appid))
    if rec:
        return Path(rec["path"]).parent
    rec = state.get("rhi_optiscaler_installs", {}).get(str(appid))
    if rec:
        return Path(rec["install_path"])
    rec = state.get("rhi_dxvk_installs", {}).get(str(appid))
    if rec:
        return Path(rec["install_path"])
    exe = _find_game_exe(install_path)
    return exe.parent if exe else Path(install_path)


def deploy_shader_packs(install_path, pack_ids, task_id=None) -> dict:
    """Deploys the given packs (dependency-expanded) into
    <install_path>/reshade-shaders/{Shaders,Textures}/ - ReShade's own
    default relative search path, so no reshade.ini rewriting is needed at
    all (unlike the old removed PCC shader-cache feature, which needed a
    shared dir + EffectSearchPaths/TextureSearchPaths). Prunes files from
    packs that were previously deployed here but are no longer selected,
    without touching anything not in that tracked list (user-placed files
    survive). Non-destructive: a pre-existing, non-PCC-managed
    reshade-shaders/ folder is renamed aside once and restored on removal."""
    install_path = Path(install_path)
    game_dir = install_path / GAME_RESHADE_SHADERS_DIR
    original_dir = install_path / GAME_RESHADE_SHADERS_ORIGINAL
    marker = game_dir / RESHADE_SHADERS_MANAGED_MARKER

    if game_dir.is_dir() and not marker.is_file() and not original_dir.exists():
        game_dir.rename(original_dir)

    pack_ids = _expand_pack_dependencies(pack_ids)
    all_files = []
    for pid in pack_ids:
        all_files += ensure_shader_pack(pid, task_id=task_id)

    state = load_state()
    prev_deployed = set(state.get("rhi_shader_deployments", {}).get(str(install_path), []))
    new_deployed = set(all_files)

    game_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text("This folder is managed by Proton Command Center's RHI "
                      "port. Deleting this file will make PCC treat the "
                      "folder as user-managed and stop touching it.\n")
    for rel in new_deployed:
        src = RHI_DATA_DIR / "shaders" / rel
        dest = game_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file() or dest.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dest)
    for rel in prev_deployed - new_deployed:
        stale = game_dir / rel
        stale.unlink(missing_ok=True)

    deployments = state.setdefault("rhi_shader_deployments", {})
    deployments[str(install_path)] = sorted(new_deployed)
    save_state(state)
    return {"deployed": len(new_deployed), "packs": pack_ids}


def remove_reshade_shaders(install_path) -> dict:
    """Deletes PCC's managed reshade-shaders/ folder and restores whatever
    non-managed folder it renamed aside on first deploy, if any."""
    install_path = Path(install_path)
    game_dir = install_path / GAME_RESHADE_SHADERS_DIR
    original_dir = install_path / GAME_RESHADE_SHADERS_ORIGINAL
    marker = game_dir / RESHADE_SHADERS_MANAGED_MARKER
    if game_dir.is_dir() and marker.is_file():
        shutil.rmtree(game_dir)
    state = load_state()
    state.get("rhi_shader_deployments", {}).pop(str(install_path), None)
    save_state(state)
    if original_dir.is_dir() and not game_dir.exists():
        original_dir.rename(game_dir)
    return {"removed": True}


def get_game_shader_selection(appid) -> list:
    return load_state().get("rhi_shader_selection", {}).get(str(appid), [])


def set_game_shader_selection(appid, pack_ids) -> dict:
    state = load_state()
    sel = state.setdefault("rhi_shader_selection", {})
    if pack_ids:
        sel[str(appid)] = list(pack_ids)
    else:
        sel.pop(str(appid), None)
    save_state(state)
    return {"selection": pack_ids}


def _deploy_shader_packs_task(task_id, install_path, pack_ids) -> None:
    try:
        result = deploy_shader_packs(install_path, pack_ids, task_id=task_id)
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"{result['deployed']} files deployed",
                          "result": result}
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


def _update_shader_pack_task(task_id, pack_id) -> None:
    """Re-fetches one shader pack's staging copy (force=True), updating
    whichever games already have it deployed the next time they're
    re-deployed via the existing 'Deploy selected' flow - deploy_shader_packs
    already re-copies any staged file whose size changed, so this doesn't
    need to push into every game itself."""
    try:
        files = ensure_shader_pack(pack_id, task_id=task_id, force=True)
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Updated: {len(files)} files",
                          "result": {"files": len(files)}}
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


def _extract_fx_files(techniques_value) -> set:
    """Parses one ReShade preset 'Techniques=' value: comma-separated
    entries of the form 'TechniqueName@file.fx', extracting the deduped
    set of .fx filenames after each '@'. Port of
    TechniquesParser.ExtractFxFiles."""
    files = set()
    for entry in (techniques_value or "").split(","):
        entry = entry.strip()
        if "@" not in entry:
            continue
        fx = entry.split("@", 1)[1].strip()
        if fx:
            files.add(fx)
    return files


def _extract_fx_files_from_preset(preset_text) -> set:
    """Scans every 'Techniques=' line in a whole preset .ini's content (a
    preset can carry more than one - PCC doesn't need to care why) and
    unions the required .fx files across all of them."""
    files = set()
    for line in (preset_text or "").splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("techniques=") or low.startswith("techniques ="):
            _, _, value = stripped.partition("=")
            files |= _extract_fx_files(value)
    return files


def resolve_preset_shader_packs(fx_files, task_id=None) -> dict:
    """Ensures every catalog pack is downloaded (needed to know which pack
    owns which .fx file - port of upstream's own "download packs missing a
    file list" step; a pack failing to download just can't be matched
    against, doesn't block resolving against the rest), then matches each
    required .fx filename (by basename, case-insensitive) against every
    pack's recorded file list. Port of ShaderResolver.Resolve."""
    for pack in RESHADE_SHADER_PACKS:
        try:
            ensure_shader_pack(pack["id"], task_id=task_id)
        except Exception:
            pass
    state = load_state()
    pack_files = state.get("rhi_shader_packs", {})
    fx_files_lower = {fx.lower() for fx in fx_files}
    matched = set()
    resolved_lower = set()
    for pack_id, entry in pack_files.items():
        names = {Path(f).name.lower() for f in entry.get("files", [])}
        hit = names & fx_files_lower
        if hit:
            matched.add(pack_id)
            resolved_lower |= hit
    unresolved = {fx for fx in fx_files if fx.lower() not in resolved_lower}
    return {"matched": sorted(matched), "unresolved": sorted(unresolved)}


def apply_preset_shader_packs(appid, install_path, preset_text, task_id=None) -> dict:
    """Full pipeline for one dropped ReShade preset: extract required .fx
    files, resolve+download the packs that provide them, union with this
    game's existing selection (dependency expansion happens inside
    deploy_shader_packs itself, same as manual selection), persist, and
    deploy. Port of MainViewModel.ApplyPresetShadersAsync."""
    fx_files = _extract_fx_files_from_preset(preset_text)
    if not fx_files:
        return {"matched": [], "unresolved": [], "deployed": 0}
    resolved = resolve_preset_shader_packs(fx_files, task_id=task_id)
    if not resolved["matched"]:
        return {**resolved, "deployed": 0}
    existing = set(get_game_shader_selection(appid))
    merged = sorted(existing | set(resolved["matched"]))
    set_game_shader_selection(appid, merged)
    deploy_result = deploy_shader_packs(install_path, merged, task_id=task_id)
    return {"matched": resolved["matched"], "unresolved": resolved["unresolved"],
           "deployed": deploy_result["deployed"]}


def _apply_preset_shader_packs_task(task_id, appid, install_path, preset_text) -> None:
    try:
        result = apply_preset_shader_packs(appid, install_path, preset_text, task_id=task_id)
        detail = (f"{len(result['matched'])} pack(s) applied, {result['deployed']} files deployed"
                  + (f" - {len(result['unresolved'])} shader(s) not found in any pack"
                     if result["unresolved"] else ""))
        TASKS[task_id] = {"status": "done", "progress": 100, "detail": detail, "result": result}
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


# --------------------------------------------------------------------------
# RHI port: ReShade addon management
# --------------------------------------------------------------------------
# ReShade's own community addon directory (crosire/reshade-shaders' `list`
# branch) - the same source RHI fetches from. Deliberately does NOT include
# RHI's "renodx-devkit"/"renodx-dlssfix" hardcoded entries: their exact
# download filenames weren't verified against real source during this
# port's original research. "RenoDX DLSS5 Setup" (below) WAS verified
# directly against RHI's real Renodx5AddonService.cs + a live GitHub
# releases check, so it's included as a real catalog entry rather than
# left out - the same standard, applied once the source was confirmed.
RESHADE_ADDONS_INI_URL = "https://raw.githubusercontent.com/crosire/reshade-shaders/list/Addons.ini"
RESHADE_ADDONS_CACHE_FILE = RHI_DATA_DIR / "addons_cache.ini"

# RenoDX DLSS5 addon (RTX 50-series-only) - hosted on RankFTW/rhi-repo under
# a renodx-dlss5-<version> release tag, confirmed live (v2.5 at research
# time: asset "renodx-dlss5_2.5.zip", tag "renodx-dlss5-2.5"). Port of
# Renodx5AddonService.cs. Ships 64-bit only - no .addon32 exists upstream.
RENODX_DLSS5_RELEASES_API = "https://api.github.com/repos/RankFTW/rhi-repo/releases?per_page=100"
RENODX_DLSS5_TAG_PREFIX = "renodx-dlss5-"
RENODX_DLSS5_ADDON_FILE = "renodx-dlss5.addon64"
# RHI's own curated DLSS manifest (distinct from the beeradmoore/
# dlss-swapper-manifest-builder one Part 2's OptiScaler DLSS swap uses). It
# also carries "dlss"/"dlssg"/"dlssd"/"streamline" sections overlapping
# beeradmoore's, and has repeatedly gone live with a new DLSS build (e.g.
# 310.8.0) before both the DLSS Swapper manifest and NVIDIA's own public
# GitHub repos catch up - see rhi_manifest_dlss_sr/fg/rr() below, checked
# alongside _manifest_latest() in download_dlss(). It's the only source
# with a "dlssnr" entry at all (NVIDIA's DLSS Neural Rendering component,
# required by RenoDX DLSS5, 50-series GPUs only): version 310.8.0 at
# research time, a zip asset on RankFTW/rhi-repo.
RHI_DLSS_MANIFEST_URL = "https://raw.githubusercontent.com/RankFTW/RHI/main/dlss_manifest.json"
RHI_DLSS_MANIFEST_SECTION = {"sr": "dlss", "fg": "dlssg", "rr": "dlssd"}
DLSSNR_DLL_NAME = "nvngx_dlssnr.dll"
DLSSNR_CACHE_DIR = RHI_DATA_DIR / "dlssnr"


def renodx_dlss5_latest():
    """Latest RenoDX DLSS5 addon release, 6h cached. Port of
    Renodx5AddonService.FetchLatestReleaseInfoAsync - scans all releases
    for the renodx-dlss5- tag prefix and picks the highest parsed version,
    since (unlike every other RHI-port catalog here) this repo has no
    single 'latest' release to rely on."""
    state = load_state()
    cache = state.get("renodx_dlss5_latest")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    try:
        releases = _gh_json(RENODX_DLSS5_RELEASES_API)
    except Exception:
        return None
    best = None
    for release in releases:
        tag = release.get("tag_name") or ""
        if not tag.startswith(RENODX_DLSS5_TAG_PREFIX):
            continue
        version = tag[len(RENODX_DLSS5_TAG_PREFIX):]
        asset = next((a for a in release.get("assets", [])
                     if a["name"].lower() == RENODX_DLSS5_ADDON_FILE
                     or (a["name"].lower().endswith(".zip")
                         and a["name"].lower().startswith("renodx-dlss5"))), None)
        if not asset:
            continue
        parsed = tuple(int(x) if x.isdigit() else 0 for x in version.split("."))
        if best is None or parsed > best[0]:
            best = (parsed, {"version": version, "url": asset["browser_download_url"],
                             "asset_name": asset["name"]})
    if not best:
        return None
    data = best[1]
    state["renodx_dlss5_latest"] = {"ts": now, "data": data}
    save_state(state)
    return data


def _rhi_dlss_manifest() -> dict:
    state = load_state()
    cache = state.get("rhi_dlss_manifest")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    req = urllib.request.Request(RHI_DLSS_MANIFEST_URL, headers={"User-Agent": "pcc"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    state["rhi_dlss_manifest"] = {"ts": now, "data": data}
    save_state(state)
    return data


def _rhi_manifest_latest(kind, task_id):
    """Same shape as _manifest_latest() but reading RHI's own dlss_manifest.json
    instead of DLSS Swapper's. Checked alongside it in download_dlss() -
    whichever manifest reports the higher version wins, so a lag on either
    side (this has happened on both) never blocks picking up a new build."""
    import zipfile, io
    section = RHI_DLSS_MANIFEST_SECTION.get(kind)
    if not section:
        return None
    try:
        entries = _rhi_dlss_manifest().get(section) or []
    except Exception:
        return None
    if not entries:
        return None
    best = max(entries, key=lambda e: version_tuple(e.get("version", "0")))
    url = best.get("url")
    if not url:
        return None
    TASKS[task_id]["detail"] = f"RHI manifest has {best.get('version')}, downloading"
    data = _gh_bytes(url, task_id)
    fname = KIND_TO_NAME.get(kind)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = [m for m in z.namelist() if m.lower().endswith(fname)]
            if members:
                return best.get("version"), z.read(members[0])
    except zipfile.BadZipFile:
        pass
    return None


# Streamline SDK: the full sl.*.dll/nvngx_*.dll bundle (interposer + plugins),
# distinct from the single "dlss"/"dlssg"/"dlssd" DLLs download_dlss() swaps
# per-game. There's no single convention for where a game expects the whole
# set placed, so this only wires up the download point - a local version
# cache the user can point a game at manually, same spirit as manual import.
STREAMLINE_DATA_DIR = RHI_DATA_DIR / "streamline"


def streamline_sdk_latest() -> dict | None:
    """Latest Streamline SDK release from RHI's own manifest's "streamline"
    section. 6h cached via _rhi_dlss_manifest()."""
    entries = _rhi_dlss_manifest().get("streamline") or []
    if not entries:
        return None
    best = max(entries, key=lambda e: version_tuple(e.get("version", "0")))
    if not best.get("url"):
        return None
    return {"version": best.get("version"), "url": best["url"]}


def streamline_sdk_library() -> list:
    """Locally cached Streamline SDK bundles, newest first."""
    if not STREAMLINE_DATA_DIR.is_dir():
        return []
    out = []
    for vdir in STREAMLINE_DATA_DIR.iterdir():
        if not vdir.is_dir():
            continue
        files = sorted(f.name for f in vdir.iterdir() if f.is_file())
        if files:
            out.append({"version": vdir.name, "path": str(vdir), "files": files})
    out.sort(key=lambda e: version_tuple(e["version"]), reverse=True)
    return out


def download_streamline_sdk(task_id) -> None:
    """Downloads+extracts the latest Streamline SDK bundle into
    STREAMLINE_DATA_DIR/<version>/, flattening the zip's internal folder.
    A no-op if that version is already cached."""
    import zipfile, io
    TASKS[task_id] = {"status": "running", "progress": 0, "detail": "Checking RHI manifest"}
    try:
        latest = streamline_sdk_latest()
        if not latest:
            TASKS[task_id] = {"status": "error", "progress": 0,
                              "detail": "No Streamline SDK entry in RHI's manifest"}
            return
        version = latest["version"]
        version_dir = STREAMLINE_DATA_DIR / version
        if version_dir.is_dir() and any(version_dir.iterdir()):
            TASKS[task_id] = {"status": "done", "progress": 100,
                              "detail": f"Streamline SDK {version} already cached",
                              "result": {"version": version, "path": str(version_dir)}}
            return
        TASKS[task_id]["detail"] = f"Downloading Streamline SDK {version}"
        data = _gh_bytes(latest["url"], task_id)
        TASKS[task_id]["detail"] = "Extracting"
        version_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                name = Path(member).name
                if not name:
                    continue
                (version_dir / name).write_bytes(zf.read(member))
                count += 1
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Streamline SDK {version} cached ({count} files)",
                          "result": {"version": version, "path": str(version_dir)}}
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


STREAMLINE_GAME_SUBDIR = "OptiScaler/Streamline"


def deploy_streamline_to_game(install_path, version=None) -> dict:
    """Copies every .dll from one cached Streamline SDK version subfolder
    into <install_path>/OptiScaler/Streamline/ - the interposer+plugin
    bundle OptiScaler's FrameGen chainloads from there when FGOutput=dlssg
    is selected. Plain overwrite copy, no .original backups (these are
    RHI-managed files, not game originals - matches upstream exactly).
    Falls back to the newest cached version if the requested one isn't
    available. Port of OptiScalerService.DeployStreamlineToGame."""
    library = streamline_sdk_library()
    if not library:
        raise RuntimeError("No Streamline SDK cached yet - download one from the "
                           "OptiScaler section first.")
    entry = next((e for e in library if e["version"] == version), None) if version else None
    entry = entry or library[0]   # streamline_sdk_library() sorts newest first
    src_dir = Path(entry["path"])
    dest_dir = Path(install_path) / STREAMLINE_GAME_SUBDIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for f in src_dir.glob("*.dll"):
        shutil.copy2(f, dest_dir / f.name)
        copied += 1
    return {"deployed": copied, "version": entry["version"], "path": str(dest_dir)}


def remove_streamline_from_game(install_path) -> dict:
    """Deletes <install_path>/OptiScaler/Streamline/ entirely. Port of
    OptiScalerService.RemoveStreamlineFromGame. (remove_optiscaler() already
    rmtree's the whole OptiScaler/ subfolder on OptiScaler removal, which
    takes this with it - this is for toggling Streamline off on its own,
    independent of removing OptiScaler itself.)"""
    dest_dir = Path(install_path) / STREAMLINE_GAME_SUBDIR
    removed = dest_dir.is_dir()
    if removed:
        shutil.rmtree(dest_dir)
    return {"removed": removed}


def scan_streamline_for_game(install_path) -> dict:
    """Whether a Streamline bundle is currently deployed for this game (any
    .dll present in its OptiScaler/Streamline/ folder), plus the cached SDK
    library so the UI can offer a version picker."""
    dest_dir = Path(install_path) / STREAMLINE_GAME_SUBDIR
    deployed_files = sorted(f.name for f in dest_dir.glob("*.dll")) if dest_dir.is_dir() else []
    return {"deployed": bool(deployed_files), "files": deployed_files,
           "library": streamline_sdk_library()}


def ensure_dlssnr_cached() -> Path | None:
    """Downloads+caches the latest nvngx_dlssnr.dll from RHI's own curated
    manifest - the required companion for the RenoDX DLSS5 addon."""
    import zipfile, io
    entries = _rhi_dlss_manifest().get("dlssnr") or []
    if not entries:
        return None
    entry = entries[0]
    version_dir = DLSSNR_CACHE_DIR / entry["version"]
    cached = version_dir / DLSSNR_DLL_NAME
    if cached.is_file():
        return cached
    data = _gh_bytes(entry["url"])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(DLSSNR_DLL_NAME)]
        if not names:
            return None
        version_dir.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(zf.read(names[0]))
    return cached


def _deploy_dlssnr_if_absent(install_path) -> None:
    """Copies nvngx_dlssnr.dll to the addon's install dir if not already
    present - never overwrites an existing copy (matches
    DeployNrDllIfAbsentAsync's own no-op-if-present behavior)."""
    dest = Path(install_path) / DLSSNR_DLL_NAME
    if dest.is_file():
        return
    src = ensure_dlssnr_cached()
    if src:
        shutil.copy2(src, dest)


def find_dlssnr_target_dir(install_path, other_roots=()) -> Path | None:
    """Where nvngx_dlssnr.dll belongs for a game that doesn't have one yet:
    whatever directory its other DLSS engine DLLs already live in, since
    Streamline loads its nvngx_*.dll plugins from a single shared folder -
    the same reasoning as the RenoDX DLSS5 addon's own DeployNrDllIfAbsentAsync,
    generalised beyond that one addon's own install dir. Games scatter these
    across nested engine-plugin folders (see scan_game_dlss's climb-up
    comment), so this reuses that same scan rather than guessing a path.
    other_roots is forwarded to scan_game_dlss() unchanged - required here
    even more than there, since this function's whole job is picking a
    write target, not just a display path. Prefers Super Resolution's
    folder (present whenever DLSS is used at all), falling back to Frame
    Generation's then Ray Reconstruction's. None if no other DLSS DLL was
    found anywhere - without one there's no basis to guess where NR should
    go."""
    found = scan_game_dlss(install_path, other_roots=other_roots)
    for kind in ("sr", "fg", "rr"):
        hit = next((f for f in found if f["kind"] == kind), None)
        if hit:
            return Path(hit["path"]).parent
    return None


def deploy_dlssnr_to_game(install_path, other_roots=()) -> dict:
    """Copies the cached nvngx_dlssnr.dll into whichever directory this
    game's other DLSS DLLs already live in (find_dlssnr_target_dir). Backs
    up any existing NR file first, matching swap_dll()'s backup convention,
    so restore_dll() works on it afterwards like any other DLSS DLL."""
    target_dir = find_dlssnr_target_dir(install_path, other_roots=other_roots)
    if not target_dir:
        raise RuntimeError("No DLSS Super Resolution/Frame Generation/Ray "
                           "Reconstruction DLL found in this game - can't tell "
                           "where the Neural Rendering DLL should go.")
    src = ensure_dlssnr_cached()
    if not src:
        raise RuntimeError("Couldn't fetch nvngx_dlssnr.dll from RHI's manifest.")
    dest = target_dir / DLSSNR_DLL_NAME
    if dest.is_file():
        bak = _backup_path(dest)
        if not bak.exists():
            shutil.copy2(dest, bak)
    shutil.copy2(src, dest)
    return {"deployed": True, "path": str(dest), "version": pe_version(dest)}


def _slugify_addon_name(name) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


_ADDONS_INI_EXCLUDED_SECTIONS = {"00", "21", "26"}


def _parse_addons_ini(content) -> list:
    """Parses ReShade's Addons.ini format: blank-line-separated blocks, each
    starting with `[NN]` (active) or `# [NN]`/`;[NN]` (disabled - skipped),
    followed by `Key=Value` lines. Sections whose id is in
    _ADDONS_INI_EXCLUDED_SECTIONS are skipped too, regardless of
    commented-out status - they're managed by RHI itself or not applicable
    (e.g. "00" is crosire's swapchain-override addon, which duplicates what
    RHI's own DXVK/OptiScaler coexistence logic already handles). Port of
    RHI's AddonsIniParser.ExcludedSections."""
    addons = []
    current = None
    excluded = False
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            if current and current.get("PackageName") and not excluded:
                addons.append(current)
            current = None
            excluded = False
            continue
        if line.startswith("#") or line.startswith(";"):
            continue   # disabled section or comment
        if line.startswith("[") and line.endswith("]"):
            section_id = line[1:-1].strip()
            excluded = section_id in _ADDONS_INI_EXCLUDED_SECTIONS
            current = {}
            continue
        if current is not None and "=" in line:
            key, _, value = line.partition("=")
            current[key.strip()] = value.strip()
    if current and current.get("PackageName") and not excluded:
        addons.append(current)
    return addons


def reshade_addons_catalog() -> list:
    """Fetches+parses the community Addons.ini, 6h cached in state (like
    every other RHI-port catalog here), with a disk-file fallback if the
    fetch fails and nothing is cached yet."""
    state = load_state()
    cache = state.get("reshade_addons_catalog")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    try:
        req = urllib.request.Request(RESHADE_ADDONS_INI_URL,
                                     headers={"User-Agent": "Mozilla/5.0 pcc"})
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
        RHI_DATA_DIR.mkdir(parents=True, exist_ok=True)
        RESHADE_ADDONS_CACHE_FILE.write_text(text)
    except Exception:
        if RESHADE_ADDONS_CACHE_FILE.is_file():
            text = RESHADE_ADDONS_CACHE_FILE.read_text(errors="replace")
        else:
            raise
    parsed = _parse_addons_ini(text)
    data = [{"id": _slugify_addon_name(a["PackageName"]), "name": a["PackageName"],
            "description": a.get("PackageDescription", ""),
            "download_url32": a.get("DownloadUrl32") or a.get("DownloadUrl"),
            "download_url64": a.get("DownloadUrl64") or a.get("DownloadUrl"),
            "repository_url": a.get("RepositoryUrl", "")}
           for a in parsed]
    try:
        dlss5 = renodx_dlss5_latest()
    except Exception:
        dlss5 = None
    if dlss5:
        data.append({
            "id": "renodx-dlss5", "name": "RenoDX DLSS5 Setup (RTX 50 Series only)",
            "description": "Sets up RenoDX for DLSS5 and deploys the required "
                           "nvngx_dlssnr.dll alongside it. RTX 50-series GPUs only.",
            "download_url32": None, "download_url64": dlss5["url"],
            "zip_member": RENODX_DLSS5_ADDON_FILE,
            "repository_url": "https://github.com/RankFTW/rhi-repo",
        })
    # RenoDX DevKit + DLSS Fix: hardcoded, non-versioned "snapshot" release
    # assets RHI injects into its catalog alongside the Addons.ini entries
    # (AddonPackService.cs's RenoDxDevKitEntry/DlssFixEntry) rather than
    # sourcing them from Addons.ini. DLSS Fix has no 32-bit build upstream.
    data.append({
        "id": "renodx-devkit", "name": "RenoDX DevKit",
        "description": "RenoDX development tools addon for ReShade.",
        "download_url32": "https://github.com/clshortfuse/renodx/releases/"
                          "download/snapshot/renodx-devkit.addon32",
        "download_url64": "https://github.com/clshortfuse/renodx/releases/"
                          "download/snapshot/renodx-devkit.addon64",
        "repository_url": "https://github.com/clshortfuse/renodx",
    })
    data.append({
        "id": "renodx-dlssfix", "name": "DLSS Fix",
        "description": "Makes ReShade draw on native game frames instead of frame gen "
                       "frames. Also hides DLSS upscaling from ReShade.",
        "download_url32": None,
        "download_url64": "https://github.com/clshortfuse/renodx/releases/"
                          "download/snapshot/renodx-dlssfix.addon64",
        "repository_url": "https://github.com/clshortfuse/renodx/wiki/Mods#unreal-engine-",
    })
    state["reshade_addons_catalog"] = {"ts": now, "data": data}
    save_state(state)
    return data


def deploy_reshade_addons(install_path, addon_ids, bitness, task_id=None) -> dict:
    """Downloads the selected addons' .addon32/.addon64 (by bitness) and
    copies them directly into the game's install root, next to the ReShade
    DLL - ReShade only auto-loads addon binaries sitting beside itself, so
    unlike shaders these are never placed in the reshade-shaders/ subfolder.
    Prunes files from addons no longer selected; never touches a file not
    in PCC's own deployment record (same non-destructive pattern as
    everywhere else in this port)."""
    install_path = Path(install_path)
    catalog = {a["id"]: a for a in reshade_addons_catalog()}
    url_key = "download_url64" if bitness == 64 else "download_url32"
    ext = "addon64" if bitness == 64 else "addon32"

    state = load_state()
    deployments = state.setdefault("rhi_addon_deployments", {})
    prev = set(deployments.get(str(install_path), []))
    new_files = set()
    skipped = []

    for aid in addon_ids:
        addon = catalog.get(aid)
        if not addon:
            skipped.append(f"{aid} (not in catalog)")
            continue
        if not addon.get(url_key):
            skipped.append(f"{addon['name']} (no {bitness}-bit build available)")
            continue
        if task_id:
            TASKS[task_id] = {"status": "running", "progress": 10,
                              "detail": f"Downloading {addon['name']}"}
        data = _gh_bytes(addon[url_key], task_id)
        zip_member = addon.get("zip_member")
        if zip_member:
            import zipfile, io
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(zip_member.lower())]
                if not names:
                    continue
                data = zf.read(names[0])
        fname = f"{aid}.{ext}"
        (install_path / fname).write_bytes(data)
        new_files.add(fname)

    for stale in prev - new_files:
        (install_path / stale).unlink(missing_ok=True)

    # RenoDX DLSS5's required companion DLL - deployed once, never
    # overwritten or removed alongside the addon (matches RHI's own
    # DeployNrDllIfAbsentAsync/Uninstall behavior - a persistent asset,
    # not tied 1:1 to the addon's own lifecycle).
    if "renodx-dlss5" in addon_ids and f"renodx-dlss5.{ext}" in new_files:
        try:
            _deploy_dlssnr_if_absent(install_path)
        except Exception:
            pass

    deployments[str(install_path)] = sorted(new_files)
    save_state(state)
    detail = f"{len(new_files)} addon(s) deployed"
    if skipped:
        detail += f" - skipped: {', '.join(skipped)}"
    if task_id:
        TASKS[task_id] = {"status": "done", "progress": 100, "detail": detail,
                          "result": {"deployed": len(new_files), "skipped": skipped}}
    return {"deployed": len(new_files), "skipped": skipped}


def remove_reshade_addons(install_path) -> dict:
    install_path = Path(install_path)
    state = load_state()
    deployments = state.get("rhi_addon_deployments", {})
    files = deployments.pop(str(install_path), [])
    for f in files:
        (install_path / f).unlink(missing_ok=True)
    save_state(state)
    return {"removed": len(files)}


def get_game_addon_selection(appid) -> list:
    return load_state().get("rhi_addon_selection", {}).get(str(appid), [])


def set_game_addon_selection(appid, addon_ids) -> dict:
    state = load_state()
    sel = state.setdefault("rhi_addon_selection", {})
    if addon_ids:
        sel[str(appid)] = list(addon_ids)
    else:
        sel.pop(str(appid), None)
    save_state(state)
    return {"selection": addon_ids}


def _deploy_reshade_addons_task(task_id, install_path, addon_ids, bitness) -> None:
    try:
        deploy_reshade_addons(install_path, addon_ids, bitness, task_id=task_id)
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


# --------------------------------------------------------------------------
# RHI port: OptiScaler (upscaler redirection DLSS<->FSR<->XeSS + frame gen)
# --------------------------------------------------------------------------
# Unlike ReShade's self-extracting .exe, every OptiScaler release (Stable
# and Nightly) ships only as a .7z archive - confirmed live, no zip
# fallback exists. That needs a real system 7-Zip install (`7z` on PATH),
# same as this file already shells out to `steam`/`pkexec`/`systemctl`
# rather than adding a new Python dependency (py7zr pulls in several
# C-extension packages - pyzstd, brotli, pyppmd, pycryptodomex - which
# would violate this file's stdlib-only design far more than shelling out
# to a standard Linux CLI tool does).
OPTISCALER_DATA_DIR = RHI_DATA_DIR / "optiscaler"
OPTISCALER_STAGING_DIR = OPTISCALER_DATA_DIR / "stable"
OPTISCALER_NIGHTLY_DIR = OPTISCALER_DATA_DIR / "nightly"
OPTISCALER_INIS_DIR = OPTISCALER_DATA_DIR / "inis"          # user-editable, seeded once
OPTIPATCHER_STAGING_DIR = OPTISCALER_DATA_DIR / "optipatcher"
OPTISCALER_DLSS_DIR = OPTISCALER_DATA_DIR / "dlss"           # SR/RR/FG dll cache
OPTISCALER_INI_TEMPLATES_DIR = Path(__file__).resolve().parent / "optiscaler_inis"

OPTISCALER_RELEASES_API = "https://api.github.com/repos/optiscaler/OptiScaler/releases/latest"
OPTISCALER_NIGHTLY_RELEASES_API = "https://api.github.com/repos/optiscaler/OptiScaler-nightly/releases"
OPTIPATCHER_RELEASES_API = "https://api.github.com/repos/optiscaler/OptiPatcher/releases/tags/rolling"
OPTISCALER_DLSS_MANIFEST_URL = ("https://raw.githubusercontent.com/beeradmoore/"
                                "dlss-swapper-manifest-builder/main/manifest.json")

# Ported verbatim from OptiScalerService.cs - the DLL names OptiScaler can
# be renamed to (dxgi.dll by default; winmm.dll for Vulkan games), and the
# 3 DLSS DLL kinds it can swap in from the DLSS Swapper manifest. Loose
# companion DLLs (fakenvapi, libxess*, amd_fidelityfx_*, etc) vary by
# release and aren't hardcoded - each install's own actual footprint is
# tracked in its state record instead (deployed_files/deployed_subdirs).
OPTISCALER_SUPPORTED_DLL_NAMES = [
    "dxgi.dll", "winmm.dll", "d3d11.dll", "d3d12.dll", "dbghelp.dll",
    "version.dll", "wininet.dll", "winhttp.dll",
]
OPTISCALER_DLSS_DLL_NAMES = {"dlss": "nvngx_dlss.dll", "dlss_d": "nvngx_dlssd.dll",
                             "dlss_g": "nvngx_dlssg.dll"}

# Friendly key name -> Windows VK hex code, ported verbatim from
# OptiScalerService.cs - purely OptiScaler's own INI value format, resolved
# entirely inside the DLL's own in-process keyboard hook, so it works
# identically under Proton with no Windows-host dependency.
OPTISCALER_HOTKEY_VK_CODES = {
    "Insert": "0x2D", "Delete": "0x2E", "Home": "0x24", "End": "0x23",
    "Page Up": "0x21", "Page Down": "0x22",
    "F1": "0x70", "F2": "0x71", "F3": "0x72", "F4": "0x73", "F5": "0x74",
    "F6": "0x75", "F7": "0x76", "F8": "0x77", "F9": "0x78", "F10": "0x79",
    "F11": "0x7A", "F12": "0x7B",
}
# Real allowed values, sourced from the bundled OptiScaler.ini's own
# [FrameGen] comments (not invented).
OPTISCALER_FG_INPUT_VALUES = ["auto", "nofg", "dlssg", "nukems", "fsrfg", "upscaler", "fsrfg30"]
OPTISCALER_FG_OUTPUT_VALUES = ["auto", "nofg", "fsrfg", "xefg", "nukems"]

# The 4 real bundled INI templates (of RHI's nominal 6 - nvidia.ini and
# amd-dlss.ini are byte-identical, so only the dlss/nodlss split matters).
_OPTISCALER_INI_CONFIGS = (
    ("NVIDIA", True, False), ("AMD", True, False), ("AMD", False, False),
    ("NVIDIA", True, True), ("AMD", True, True), ("AMD", False, True),
)


def _find_7z_binary() -> str | None:
    for name in ("7z", "7za", "7zr"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _optiscaler_staging_dir(nightly=False) -> Path:
    return OPTISCALER_NIGHTLY_DIR if nightly else OPTISCALER_STAGING_DIR


def optiscaler_staging_version(nightly=False):
    p = _optiscaler_staging_dir(nightly) / "version.txt"
    return p.read_text().strip() if p.is_file() else None


def optiscaler_staging_ready(nightly=False) -> bool:
    d = _optiscaler_staging_dir(nightly)
    return (d / "OptiScaler.dll").is_file() and (d / "version.txt").is_file()


def optiscaler_latest(nightly=False) -> dict:
    """Latest OptiScaler release metadata (tag + .7z asset URL), 6h cached.
    Stable has a real 'latest' alias; Nightly's repo doesn't, so the first
    (newest) entry of the plain /releases list is used instead - matches
    RHI's own approach (Staging.cs:800-864)."""
    state = load_state()
    cache_key = "optiscaler_nightly_latest" if nightly else "optiscaler_latest"
    cache = state.get(cache_key)
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    if nightly:
        releases = _gh_json(OPTISCALER_NIGHTLY_RELEASES_API)
        if not releases:
            raise RuntimeError("Couldn't reach OptiScaler-nightly's releases")
        release = releases[0]
        tag = (release["tag_name"] or "").replace("nightly-", "")
    else:
        release = _gh_json(OPTISCALER_RELEASES_API)
        tag = release["tag_name"]
    asset = next((a for a in release.get("assets", [])
                 if a["name"].lower().endswith(".7z")), None)
    if not asset:
        raise RuntimeError("No .7z asset found in the latest OptiScaler release")
    data = {"version": tag, "url": asset["browser_download_url"], "asset_name": asset["name"]}
    state[cache_key] = {"ts": now, "data": data}
    save_state(state)
    return data


def check_optiscaler_update(nightly=False) -> bool:
    try:
        info = optiscaler_latest(nightly=nightly)
    except Exception:
        return False
    return optiscaler_staging_ready(nightly) and optiscaler_staging_version(nightly) != info["version"]


def ensure_optiscaler_staging(nightly=False, task_id=None) -> Path:
    """Downloads+extracts the latest OptiScaler .7z release into the
    staging dir via a system `7z` binary. Port of EnsureStagingAsync /
    EnsureNightlyStagingAsync (Staging.cs:11-247, 800-1024), collapsed into
    one function parameterized by `nightly` rather than RHI's two
    near-duplicate copies."""
    info = optiscaler_latest(nightly=nightly)
    staging_dir = _optiscaler_staging_dir(nightly)
    if optiscaler_staging_ready(nightly) and optiscaler_staging_version(nightly) == info["version"]:
        return staging_dir

    seven_zip = _find_7z_binary()
    if not seven_zip:
        raise RuntimeError(
            "OptiScaler needs the 7-Zip CLI to extract its .7z release - "
            "install it first (Arch: `sudo pacman -S 7zip`, other distros: "
            "the `p7zip` package) then try again.")

    if task_id:
        TASKS[task_id] = {"status": "running", "progress": 10,
                          "detail": f"Downloading OptiScaler {info['version']}"}
    data = _gh_bytes(info["url"], task_id)

    with tempfile.TemporaryDirectory(prefix="pcc_optiscaler_") as tmp:
        tmp = Path(tmp)
        archive = tmp / info["asset_name"]
        archive.write_bytes(data)
        extract_dir = tmp / "extracted"
        extract_dir.mkdir()
        if task_id:
            TASKS[task_id]["detail"] = "Extracting"
            TASKS[task_id]["progress"] = 75
        proc = subprocess.run([seven_zip, "x", str(archive), f"-o{extract_dir}", "-y"],
                              capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"7z extraction failed: {proc.stderr.strip()[:300]}")
        candidates = list(extract_dir.rglob("OptiScaler.dll"))
        if not candidates:
            raise RuntimeError("OptiScaler.dll not found in the extracted archive "
                               "(its release format may have changed)")
        source_dir = candidates[0].parent
        if staging_dir.is_dir():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)
        for item in source_dir.iterdir():
            if item.name.lower() == "licenses":
                continue
            dest = staging_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

    (staging_dir / "version.txt").write_text(info["version"])
    if task_id:
        TASKS[task_id]["detail"] = f"OptiScaler {info['version']} staged"
        TASKS[task_id]["progress"] = 90
    return staging_dir


def _resolve_optiscaler_dll_name(api, user_override=None) -> str:
    """dxgi.dll by default; winmm.dll for Vulkan games (dxgi.dll won't
    load there) - port of Install.cs:74-86."""
    if user_override:
        return user_override
    if api == "vulkan":
        return "winmm.dll"
    return "dxgi.dll"


def _is_optiscaler_file(path) -> bool:
    """Byte-scan for OptiScaler's signature string in the first 8MB - port
    of IsOptiScalerFileStatic (Install.cs:1300-1328)."""
    try:
        with Path(path).open("rb") as f:
            data = f.read(8 * 1024 * 1024)
    except OSError:
        return False
    return b"OptiScaler" in data


def detect_optiscaler_installation(install_path) -> str | None:
    """Scans the known DLL-name slots for OptiScaler's signature, falling
    back to ini-presence + DLL-existence-only if the signature scan misses
    - port of DetectInstallation (Install.cs:1250-1287)."""
    base = Path(install_path)
    if not base.is_dir():
        return None
    for name in OPTISCALER_SUPPORTED_DLL_NAMES:
        p = base / name
        if p.is_file() and _is_optiscaler_file(p):
            return name
    if (base / "OptiScaler.ini").is_file():
        for name in OPTISCALER_SUPPORTED_DLL_NAMES:
            if (base / name).is_file():
                return name
    return None


def _backup_original_if_exists(path) -> None:
    """Renames an existing file aside to <name>.original before OptiScaler
    overwrites it, so it can be restored on uninstall - port of
    BackupOriginalIfExists (Install.cs:1066-1080). Unlike
    _backup_foreign_dll, this never checks ownership first: by the time
    this runs, any ReShade filename conflict has already been resolved by
    the coexistence rename step in install_optiscaler(), so whatever's
    still here really is a game-owned original."""
    path = Path(path)
    if not path.is_file():
        return
    backup = path.with_name(path.name + ".original")
    if backup.exists():
        return
    path.rename(backup)


def _restore_original_if_exists(path) -> None:
    path = Path(path)
    backup = path.with_name(path.name + ".original")
    if backup.is_file() and not path.exists():
        backup.rename(path)


def _optiscaler_ini_template_name(gpu_type, dlss_inputs, nightly) -> str:
    prefix = "nightly" if nightly else "stable"
    suffix = "dlss" if (gpu_type == "NVIDIA" or dlss_inputs) else "nodlss"
    return f"{prefix}-{suffix}.ini"


def get_optiscaler_ini_template_path(gpu_type, dlss_inputs, nightly=False) -> Path:
    return OPTISCALER_INI_TEMPLATES_DIR / _optiscaler_ini_template_name(gpu_type, dlss_inputs, nightly)


def get_optiscaler_user_ini_path(gpu_type, dlss_inputs, nightly=False) -> Path:
    return OPTISCALER_INIS_DIR / _optiscaler_ini_template_name(gpu_type, dlss_inputs, nightly)


def seed_optiscaler_user_inis() -> None:
    """Copies each of the 4 bundled templates into the user-editable INIs
    dir on first run only - never overwrites existing user edits. Port of
    SeedUserInis (Install.cs:989-1018)."""
    OPTISCALER_INIS_DIR.mkdir(parents=True, exist_ok=True)
    for gpu_type, dlss_inputs, nightly in _OPTISCALER_INI_CONFIGS:
        user_path = get_optiscaler_user_ini_path(gpu_type, dlss_inputs, nightly)
        bundled_path = get_optiscaler_ini_template_path(gpu_type, dlss_inputs, nightly)
        if not user_path.is_file() and bundled_path.is_file():
            shutil.copy2(bundled_path, user_path)


def _enforce_ini_flag(ini_path, key, value) -> None:
    """Rewrites (or appends) a single top-level `key=value` line in an INI
    file, removing duplicates - generic port of RHI's EnforceLoadReshade/
    EnforceLoadAsiPlugins (Install.cs:1144-1208), parameterized by key
    rather than duplicated per-flag."""
    ini_path = Path(ini_path)
    lines = ini_path.read_text().splitlines() if ini_path.is_file() else []
    prefix = f"{key}="
    found = False
    out = []
    for line in lines:
        if line.lstrip().startswith(prefix):
            if not found:
                out.append(f"{key}={value}")
                found = True
            continue  # drop duplicates
        out.append(line)
    if not found:
        out.append(f"{key}={value}")
    ini_path.write_text("\n".join(out) + "\n")


def _resolve_hotkey_vk(name) -> str:
    return OPTISCALER_HOTKEY_VK_CODES.get(name, name)


def write_optiscaler_shortcut_key(ini_path, hotkey) -> None:
    _enforce_ini_flag(ini_path, "ShortcutKey", _resolve_hotkey_vk(hotkey))


def set_optiscaler_hotkey(hotkey) -> None:
    """Writes ShortcutKey= to all 4 seeded user-INI templates so future
    installs pick it up, and remembers it as the global default - port of
    SetHotkey (Install.cs:1396-1415)."""
    state = load_state()
    state["rhi_optiscaler_hotkey"] = hotkey
    save_state(state)
    OPTISCALER_INIS_DIR.mkdir(parents=True, exist_ok=True)
    for gpu_type, dlss_inputs, nightly in _OPTISCALER_INI_CONFIGS:
        p = get_optiscaler_user_ini_path(gpu_type, dlss_inputs, nightly)
        if p.is_file():
            write_optiscaler_shortcut_key(p, hotkey)


def apply_optiscaler_hotkey_to_all_games(hotkey) -> int:
    """Writes ShortcutKey= into every installed game's live OptiScaler.ini -
    port of ApplyHotkeyToAllGames (Install.cs:1418-1445)."""
    state = load_state()
    installs = state.get("rhi_optiscaler_installs", {})
    updated = 0
    for rec in installs.values():
        ini_path = Path(rec["install_path"]) / "OptiScaler.ini"
        if ini_path.is_file():
            write_optiscaler_shortcut_key(ini_path, hotkey)
            updated += 1
    return updated


def _parse_ini_sections(ini_path) -> dict:
    """section -> {key: value} - port of ParseIniSections (Install.cs:
    1111-1137), used for the update-time merge-preserve pass."""
    result = {}
    section = ""
    for raw in Path(ini_path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            result.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result.setdefault(section, {})[key.strip()] = value.strip()
    return result


def set_optiscaler_ini_value(install_path, section, key, value) -> None:
    """Writes key=value under [section] in a game's live OptiScaler.ini,
    creating the section if missing - port of SetOptiScalerIniValue
    (Install.cs:1509-1559)."""
    ini_path = Path(install_path) / "OptiScaler.ini"
    if not ini_path.is_file():
        return
    lines = ini_path.read_text().splitlines()
    target = f"[{section}]".lower()
    key_prefix = (key + "=").lower()
    section_start = None
    section_end = len(lines)
    key_line = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section_start is not None:
                section_end = i
                break
            if stripped.lower() == target:
                section_start = i
        elif section_start is not None and stripped.lower().startswith(key_prefix):
            key_line = i
    if key_line is not None:
        lines[key_line] = f"{key}={value}"
    elif section_start is not None:
        lines.insert(section_end, f"{key}={value}")
    else:
        lines.append(f"[{section}]")
        lines.append(f"{key}={value}")
    ini_path.write_text("\n".join(lines) + "\n")


def set_optiscaler_fg(appid, fg_input, fg_output, fg_nvngx_replacement=None) -> dict:
    """Writes the 3 FrameGen keys into one game's live OptiScaler.ini -
    direct port of ApplyFgSettings (Install.cs:1565-1572), including its
    fg_output=='dlssg' gate on FGNvngxReplacement (kept as-is even though
    'dlssg' isn't itself a documented FGOutput value - matching RHI's own
    condition rather than guessing at a fix)."""
    state = load_state()
    rec = state.get("rhi_optiscaler_installs", {}).get(str(appid))
    if not rec:
        raise RuntimeError("No OptiScaler install tracked for this game.")
    install_path = rec["install_path"]
    set_optiscaler_ini_value(install_path, "FrameGen", "FGInput", fg_input)
    set_optiscaler_ini_value(install_path, "FrameGen", "FGOutput", fg_output)
    if str(fg_output).lower() == "dlssg" and fg_nvngx_replacement:
        set_optiscaler_ini_value(install_path, "FrameGen", "FGNvngxReplacement", fg_nvngx_replacement)
    return {"applied": True}


def _dlss_manifest() -> dict:
    state = load_state()
    cache = state.get("optiscaler_dlss_manifest")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]
    req = urllib.request.Request(OPTISCALER_DLSS_MANIFEST_URL, headers={"User-Agent": "pcc"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    state["optiscaler_dlss_manifest"] = {"ts": now, "data": data}
    save_state(state)
    return data


def _dlss_latest_entry(key):
    """Last non-dev-file entry in the manifest's dlss/dlss_d/dlss_g array -
    matches RHI's own 'keep overwriting remoteVersion, last wins' loop
    (Staging.cs:678-691)."""
    entries = _dlss_manifest().get(key) or []
    latest = None
    for entry in entries:
        if entry.get("is_dev_file"):
            continue
        latest = entry
    return latest


def ensure_dlss_dll_cached(key, task_id=None) -> Path | None:
    """Downloads+caches the latest DLSS SR/RR/FG dll from the real DLSS
    Swapper manifest (a plain .zip, no 7z needed here) - one generic
    function covering what RHI splits across 3 near-identical
    DlssStreamlineService methods."""
    import zipfile, io
    entry = _dlss_latest_entry(key)
    if not entry:
        return None
    dll_name = OPTISCALER_DLSS_DLL_NAMES[key]
    version_dir = OPTISCALER_DLSS_DIR / key / entry["version"]
    cached = version_dir / dll_name
    if cached.is_file():
        return cached
    if task_id and task_id in TASKS:
        TASKS[task_id]["detail"] = f"Downloading {dll_name} {entry['version']}"
    data = _gh_bytes(entry["download_url"])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(dll_name)]
        if not names:
            raise RuntimeError(f"DLSS manifest zip didn't contain {dll_name}")
        version_dir.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(zf.read(names[0]))
    return cached


def get_staged_dlss_dll(key) -> Path | None:
    """Newest already-cached DLSS DLL of the given kind, without triggering
    a network fetch - used during install/update so a slow/offline DLSS
    check never blocks getting OptiScaler itself installed."""
    base = OPTISCALER_DLSS_DIR / key
    if not base.is_dir():
        return None
    dll_name = OPTISCALER_DLSS_DLL_NAMES[key]
    versions = sorted((d for d in base.iterdir() if (d / dll_name).is_file()),
                      key=lambda d: d.name, reverse=True)
    return (versions[0] / dll_name) if versions else None


def optipatcher_latest() -> dict:
    """OptiPatcher's rolling release has no version number - its build
    commit hash (parsed from the release body text) stands in for one,
    matching RHI's own regex-on-body-text approach (Staging.cs:417-431)."""
    release = _gh_json(OPTIPATCHER_RELEASES_API)
    body = release.get("body") or ""
    m = re.search(r"Build commit:\**\s*([0-9a-fA-F]+)", body)
    version = m.group(1).lower() if m else "unknown"
    asset = next((a for a in release.get("assets", [])
                 if a["name"].lower() == "optipatcher.asi"), None)
    if not asset:
        raise RuntimeError("No OptiPatcher.asi asset found in the rolling release")
    return {"version": version, "url": asset["browser_download_url"]}


def ensure_optipatcher_staged(task_id=None) -> Path:
    """OptiScaler's own dxgi.dll loads .asi plugins itself (LoadAsiPlugins=
    true, enforced at install time) - no separate ASI loader needed."""
    OPTIPATCHER_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    dest = OPTIPATCHER_STAGING_DIR / "OptiPatcher.asi"
    version_file = OPTIPATCHER_STAGING_DIR / "version.txt"
    info = optipatcher_latest()
    if dest.is_file() and version_file.is_file() and version_file.read_text().strip() == info["version"]:
        return dest
    if task_id and task_id in TASKS:
        TASKS[task_id]["detail"] = "Downloading OptiPatcher"
    data = _gh_bytes(info["url"])
    dest.write_bytes(data)
    version_file.write_text(info["version"])
    return dest


def _resolve_reshade_reclaim_name(rs_rec, optiscaler_rec) -> str:
    """Which filename ReShade should go back to once OptiScaler is removed
    - same API-correct resolution install_reshade uses (pcc.py has no
    DLL-override service, so unlike RHI's ResolveReShadeFilename this can't
    consult a user override first, but otherwise matches it exactly).
    Simplified port of Coexist.cs:14-43."""
    exe = rs_rec.get("exe") or optiscaler_rec.get("exe")
    if not exe:
        return "dxgi.dll"
    _, regular, delay = pe_imports(exe)
    return resolve_auto_reshade_filename(_detect_all_graphics_apis(regular, delay))


def scan_game_optiscaler(appid, install_path, exe_path=None) -> dict:
    """OptiScaler status for one game: detected graphics API (reused from
    Part 1a), whatever PCC has on record, and whether a newer release is
    staged than what's installed."""
    state = load_state()
    rec = state.get("rhi_optiscaler_installs", {}).get(str(appid))
    if not exe_path and rec and rec.get("exe"):
        exe_path = rec["exe"]
    exe = Path(exe_path) if exe_path else _find_game_exe(install_path)
    detected = detect_game_graphics_api(exe) if exe else {"bitness": None, "api": None}
    display = describe_graphics_api(detected["api"], exe) if exe else {"label": None, "inferred": False}
    result = {"exe": str(exe) if exe else None, "detected_api": detected["api"],
             "detected_api_display": display["label"], "detected_api_inferred": display["inferred"],
             "detected_bitness": detected["bitness"], "installed": False,
             "update_available": False}
    if rec:
        p = Path(rec["install_path"]) / rec["installed_as"]
        nightly = rec.get("variant") == "nightly"
        result.update({"installed": p.is_file(), "path": str(p),
                       "installed_as": rec["installed_as"], "variant": rec.get("variant"),
                       "gpu_type": rec.get("gpu_type"), "dlss_inputs": rec.get("dlss_inputs"),
                       "version": rec.get("version")})
        if p.is_file():
            try:
                result["update_available"] = check_optiscaler_update(nightly=nightly)
            except Exception:
                pass
    return result


def install_optiscaler(appid, install_path, exe_override=None, gpu_type=None,
                       dlss_inputs=True, variant="stable", hotkey=None,
                       task_id=None, game_name=None) -> dict:
    """Installs OptiScaler for one game: resolves the effective DLL name
    from the detected graphics API (dxgi.dll, or winmm.dll for Vulkan),
    renames an already-installed ReShade out of the way first if its
    filename would otherwise collide, deploys the staged release + INI +
    any cached DLSS DLLs, and (for AMD/Intel) OptiPatcher. For the handful
    of RE Engine games with a dedicated OptiScaler-compatible REFramework
    build (manifest.json's pdUpscalerGames), also swaps that in over an
    already-installed standard REFramework - non-fatal on failure, since
    OptiScaler itself is already successfully installed by that point."""
    nightly = variant == "nightly"
    exe = Path(exe_override).expanduser() if exe_override else _find_game_exe(install_path)
    if not exe or not exe.is_file():
        raise RuntimeError("Couldn't find the game's .exe under its install folder — "
                           "point Command Center at it manually.")
    detected = detect_game_graphics_api(exe)
    gpu_type = gpu_type or primary_gpu_vendor()
    if gpu_type == "unknown":
        gpu_type = "NVIDIA"

    staging_dir = ensure_optiscaler_staging(nightly=nightly, task_id=task_id)
    version = optiscaler_staging_version(nightly=nightly)
    effective_dll_name = _resolve_optiscaler_dll_name(detected["api"])
    target_dir = exe.parent

    # ReShade coexistence - rename it out of the way first if it would
    # otherwise collide with OptiScaler's chosen filename. Port of
    # Install.cs:90-161. Only a real conflict if ReShade lives in the SAME
    # directory OptiScaler is about to deploy into - matching by filename
    # alone isn't enough (e.g. a wrong-exe auto-detection could put the two
    # in different folders entirely, and a bare .rename() across
    # directories would silently relocate ReShade instead of refusing).
    state = load_state()
    rs_rec = state.get("rhi_reshade_installs", {}).get(str(appid))
    if rs_rec:
        rs_path = Path(rs_rec["path"])
        if (rs_path.is_file() and rs_path.parent == target_dir
                and rs_path.name.lower() == effective_dll_name.lower()
                and rs_path.name.lower() != "reshade64.dll"):
            rs_dest = target_dir / "ReShade64.dll"
            if rs_dest.exists():
                rs_dest.unlink()
            rs_path.rename(rs_dest)
            rs_rec["path"] = str(rs_dest)
            save_state(state)

    # DXVK coexistence - move a conflicting DXVK DLL into OptiScaler/plugins/
    # BEFORE the deploy loop below, so the plain _backup_original_if_exists
    # step never mistakes DXVK's own file for a game original (it doesn't
    # do identity checks - it backs up whatever is already there). Port of
    # the reverse direction of Part 3's _resolve_dxvk_dll_targets. Confirms
    # the file at that path is STILL actually DXVK (IsDxvkFileStatic-style
    # content check, port of DxvkService.IsDxvkFileStatic) before relocating
    # it - the state record could be stale (e.g. the user manually swapped
    # in a different DLL, or a previous operation left it inconsistent), and
    # relocating an untracked/foreign file into OptiScaler/plugins/ would
    # both lose it from its expected location and wrongly mark it as DXVK's.
    dxvk_rec = state.get("rhi_dxvk_installs", {}).get(str(appid))
    if dxvk_rec and Path(dxvk_rec["install_path"]) == target_dir:
        installed_dlls = list(dxvk_rec.get("installed_dlls", []))
        conflicting = [d for d in installed_dlls if d.lower() == effective_dll_name.lower()]
        if conflicting:
            plugins_dir = target_dir / "OptiScaler" / "plugins"
            plugins_dir.mkdir(parents=True, exist_ok=True)
            for dll in conflicting:
                src = target_dir / dll
                if src.is_file() and _is_dxvk_file(src):
                    dest = plugins_dir / dll
                    dest.unlink(missing_ok=True)
                    src.rename(dest)
                    installed_dlls.remove(dll)
                    dxvk_rec.setdefault("plugin_dlls", []).append(dll)
            dxvk_rec["installed_dlls"] = installed_dlls
            state.setdefault("rhi_dxvk_installs", {})[str(appid)] = dxvk_rec
            save_state(state)

    if task_id and task_id in TASKS:
        TASKS[task_id]["detail"] = "Deploying OptiScaler files"
        TASKS[task_id]["progress"] = 80

    # Track exactly what gets deployed (filenames + subdir names) so
    # remove/update can act on precisely this install's own footprint later,
    # rather than re-scanning a staging dir that may have since been
    # replaced by a newer version (e.g. from updating a different game).
    deployed_files = []
    deployed_subdirs = []
    for item in staging_dir.iterdir():
        name = item.name
        if name.lower() in ("version.txt", "optiscaler.ini"):
            continue
        if item.is_file() and name.lower().endswith((".bat", ".sh", ".txt")):
            continue
        if item.is_dir():
            if name.lower() == "licenses":
                continue
            dest_dir = target_dir / name
            for sub in item.rglob("*"):
                if sub.is_file():
                    rel = sub.relative_to(item)
                    dest = dest_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sub, dest)
            deployed_subdirs.append(name)
            continue
        dest_name = effective_dll_name if name.lower() == "optiscaler.dll" else name
        dest = target_dir / dest_name
        _backup_original_if_exists(dest)
        shutil.copy2(item, dest)
        if name.lower() != "optiscaler.dll":
            deployed_files.append(name)

    # INI seed + deploy + enforce
    seed_optiscaler_user_inis()
    user_ini = get_optiscaler_user_ini_path(gpu_type, dlss_inputs, nightly)
    game_ini = target_dir / "OptiScaler.ini"
    if not game_ini.is_file() and user_ini.is_file():
        shutil.copy2(user_ini, game_ini)
    if game_ini.is_file():
        _enforce_ini_flag(game_ini, "LoadReshade", "true")
        _enforce_ini_flag(game_ini, "LoadAsiPlugins", "true")
        effective_hotkey = hotkey or state.get("rhi_optiscaler_hotkey")
        if effective_hotkey:
            write_optiscaler_shortcut_key(game_ini, effective_hotkey)

    # DLSS DLL swap - best-effort, never blocks the install itself.
    for key, dll_name in OPTISCALER_DLSS_DLL_NAMES.items():
        try:
            src = get_staged_dlss_dll(key)
            if src:
                dest = target_dir / dll_name
                _backup_original_if_exists(dest)
                shutil.copy2(src, dest)
        except Exception:
            pass

    state = load_state()
    installs = state.setdefault("rhi_optiscaler_installs", {})
    installs[str(appid)] = {
        "install_path": str(target_dir), "installed_as": effective_dll_name,
        "variant": variant, "gpu_type": gpu_type, "dlss_inputs": dlss_inputs,
        "version": version, "exe": str(exe),
        "deployed_files": deployed_files, "deployed_subdirs": deployed_subdirs,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_state(state)

    # OptiPatcher for AMD/Intel - best-effort, install still counts as a
    # success if this fails (matches RHI's own try/catch around this step).
    if gpu_type in ("AMD", "Intel"):
        try:
            if task_id and task_id in TASKS:
                TASKS[task_id]["detail"] = "Downloading OptiPatcher"
                TASKS[task_id]["progress"] = 95
            asi = ensure_optipatcher_staged(task_id=task_id)
            plugins_dir = target_dir / "plugins"
            plugins_dir.mkdir(exist_ok=True)
            shutil.copy2(asi, plugins_dir / "OptiPatcher.asi")
        except Exception as e:
            if task_id and task_id in TASKS:
                TASKS[task_id]["detail"] = f"OptiPatcher deploy skipped: {e}"

    # PD-Upscaler REFramework swap - see docstring. Only when standard
    # REFramework is already installed here (a dinput8.dll exists) - this
    # never installs REFramework on its own, only swaps an existing one.
    pd_upscaler_installed = False
    if game_name and (target_dir / "dinput8.dll").is_file():
        artifact = pd_upscaler_artifact_for_game(game_name)
        if artifact:
            try:
                if task_id and task_id in TASKS:
                    TASKS[task_id]["detail"] = "Installing PD-Upscaler REFramework"
                install_pd_upscaler_re_framework(appid, target_dir, artifact, task_id=task_id)
                pd_upscaler_installed = True
            except Exception as e:
                if task_id and task_id in TASKS:
                    TASKS[task_id]["detail"] = f"PD-Upscaler REFramework skipped: {e}"

    if task_id:
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Installed OptiScaler {version}",
                          "result": {"version": version, "installed_as": effective_dll_name}}
    return {"installed": True, "installed_as": effective_dll_name, "version": version,
            "api": detected["api"], "gpu_type": gpu_type,
            "pd_upscaler_installed": pd_upscaler_installed}


def _install_optiscaler_task(task_id, appid, install_path, exe, gpu_type,
                             dlss_inputs, variant, hotkey, game_name=None) -> None:
    try:
        install_optiscaler(appid, install_path, exe_override=exe, gpu_type=gpu_type,
                           dlss_inputs=dlss_inputs, variant=variant, hotkey=hotkey,
                           task_id=task_id, game_name=game_name)
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


def remove_optiscaler(appid) -> dict:
    """Uninstalls OptiScaler for one game: deletes the deployed DLL (+
    restores its .original backup, unless ReShade is about to reclaim that
    exact filename), OptiScaler.ini, deployed DLSS DLLs, companion
    subdirectories, OptiPatcher, and restores ReShade's real filename if it
    was parked at ReShade64.dll for coexistence. Port of Uninstall
    (Install.cs:423-736)."""
    state = load_state()
    installs = state.get("rhi_optiscaler_installs", {})
    rec = installs.pop(str(appid), None)
    if not rec:
        raise RuntimeError("No OptiScaler install tracked for this game.")
    target_dir = Path(rec["install_path"])
    installed_dll = rec["installed_as"]

    # Restore standard REFramework first if PD-Upscaler was swapped in for
    # this game - matches RHI's own ordering (UninstallOptiScaler does this
    # before touching OptiScaler's own files). restore_standard_re_framework
    # persists its own change independently, so its result is folded back
    # into THIS function's `state` (rather than reassigning `state`
    # wholesale) - the save_state(state) at the end of this function is a
    # full-dict overwrite that would otherwise clobber it with a stale copy,
    # along with silently undoing the installs.pop() a few lines up.
    ref_rec = state.get("rhi_reframework_installs", {}).get(str(appid))
    if ref_rec and ref_rec.get("version") == "PD-Upscaler":
        restore_standard_re_framework(appid, target_dir)
        fresh_ref = load_state().get("rhi_reframework_installs", {}).get(str(appid))
        if fresh_ref:
            state.setdefault("rhi_reframework_installs", {})[str(appid)] = fresh_ref

    rs_rec = state.get("rhi_reshade_installs", {}).get(str(appid))
    rs_reclaim_name = None
    rs_coexist_path = target_dir / "ReShade64.dll"
    if rs_rec and rs_coexist_path.is_file():
        rs_reclaim_name = _resolve_reshade_reclaim_name(rs_rec, rec)

    dll_path = target_dir / installed_dll
    if dll_path.is_file():
        dll_path.unlink()
        if rs_reclaim_name is None or installed_dll.lower() != rs_reclaim_name.lower():
            _restore_original_if_exists(dll_path)
        else:
            dll_path.with_name(dll_path.name + ".original").unlink(missing_ok=True)

    (target_dir / "OptiScaler.ini").unlink(missing_ok=True)

    for dll_name in OPTISCALER_DLSS_DLL_NAMES.values():
        p = target_dir / dll_name
        if p.is_file():
            p.unlink()
            _restore_original_if_exists(p)

    # Every other file/subdirectory install_optiscaler() deployed - read
    # from this install's OWN recorded footprint (deployed_files/
    # deployed_subdirs, captured at install/update time) rather than
    # re-scanning the current staging dir, which may have since been
    # replaced by a newer version via another game's install/update.
    # Records from before this tracking existed fall back to scanning
    # staging (best-effort, matches the old behavior).
    if "deployed_files" in rec:
        companion_files = rec.get("deployed_files", [])
        companion_subdirs = rec.get("deployed_subdirs", [])
    else:
        staging_dir = _optiscaler_staging_dir(rec.get("variant") == "nightly")
        companion_files, companion_subdirs = [], []
        if staging_dir.is_dir():
            for item in staging_dir.iterdir():
                if item.name.lower() in ("version.txt", "optiscaler.dll", "optiscaler.ini"):
                    continue
                if item.is_file():
                    companion_files.append(item.name)
                elif item.is_dir() and item.name.lower() != "licenses":
                    companion_subdirs.append(item.name)

    for name in companion_files:
        p = target_dir / name
        if p.is_file():
            p.unlink()
            _restore_original_if_exists(p)
    for name in companion_subdirs:
        game_sub = target_dir / name
        if not game_sub.is_dir():
            continue
        for sub in game_sub.rglob("*"):
            if sub.is_file():
                sub.unlink()
                _restore_original_if_exists(sub)
        shutil.rmtree(game_sub, ignore_errors=True)

    optipatcher_path = target_dir / "plugins" / "OptiPatcher.asi"
    if optipatcher_path.is_file():
        optipatcher_path.unlink()
        plugins_dir = target_dir / "plugins"
        if plugins_dir.is_dir() and not any(plugins_dir.iterdir()):
            plugins_dir.rmdir()

    # DXVK coexistence - move any DXVK DLL parked in OptiScaler/plugins/
    # back to the game root before the OptiScaler/ subfolder gets removed
    # below, so removing OptiScaler never takes DXVK's files down with it.
    dxvk_rec = state.get("rhi_dxvk_installs", {}).get(str(appid))
    if dxvk_rec and dxvk_rec.get("plugin_dlls"):
        dxvk_plugins_dir = target_dir / "OptiScaler" / "plugins"
        remaining = []
        for dll in list(dxvk_rec["plugin_dlls"]):
            src = dxvk_plugins_dir / dll
            dest = target_dir / dll
            if src.is_file() and not dest.exists():
                src.rename(dest)
                dxvk_rec.setdefault("installed_dlls", []).append(dll)
            else:
                remaining.append(dll)
        dxvk_rec["plugin_dlls"] = remaining
        state.setdefault("rhi_dxvk_installs", {})[str(appid)] = dxvk_rec

    optiscaler_subdir = target_dir / "OptiScaler"
    if optiscaler_subdir.is_dir():
        shutil.rmtree(optiscaler_subdir, ignore_errors=True)

    if rs_rec and rs_coexist_path.is_file():
        resolved_name = rs_reclaim_name or "ReShade64.dll"
        resolved_path = target_dir / resolved_name
        if resolved_name.lower() != "reshade64.dll" and not resolved_path.is_file():
            rs_coexist_path.rename(resolved_path)
            rs_rec["path"] = str(resolved_path)
        else:
            rs_rec["path"] = str(rs_coexist_path)
        state.setdefault("rhi_reshade_installs", {})[str(appid)] = rs_rec

    save_state(state)
    return {"removed": True}


def update_optiscaler(appid, task_id=None) -> dict:
    """Updates an installed OptiScaler to the latest staged release:
    redeploys files (no .original backups - these are all previously
    OptiScaler-owned, not game originals), removes stale companions no
    longer shipped, and merge-preserves any user-changed OptiScaler.ini
    values across the fresh template. Port of UpdateAsync (Install.cs:
    739-985)."""
    state = load_state()
    rec = state.get("rhi_optiscaler_installs", {}).get(str(appid))
    if not rec:
        raise RuntimeError("No OptiScaler install tracked for this game.")
    nightly = rec.get("variant") == "nightly"
    target_dir = Path(rec["install_path"])
    installed_dll = rec["installed_as"]

    staging_dir = ensure_optiscaler_staging(nightly=nightly, task_id=task_id)
    version = optiscaler_staging_version(nightly=nightly)

    if task_id and task_id in TASKS:
        TASKS[task_id]["detail"] = "Updating OptiScaler files"
        TASKS[task_id]["progress"] = 60

    # Remove companions the new release no longer ships, diffing against
    # THIS install's own previously-recorded footprint (not a hardcoded
    # name list - see the same reasoning in remove_optiscaler()). Records
    # from before this tracking existed have nothing to diff against, so
    # nothing stale gets removed for them (best-effort, matches old
    # behavior rather than risking deleting an unrelated game file).
    old_files = set(rec.get("deployed_files", []))
    old_subdirs = set(rec.get("deployed_subdirs", []))
    new_files = {p.name for p in staging_dir.iterdir()
                if p.is_file() and p.name.lower() not in ("version.txt", "optiscaler.dll", "optiscaler.ini")
                and not p.name.lower().endswith((".bat", ".sh", ".txt"))}
    new_subdirs = {p.name for p in staging_dir.iterdir() if p.is_dir() and p.name.lower() != "licenses"}
    for name in old_files - new_files:
        p = target_dir / name
        if p.is_file() and name.lower() != installed_dll.lower():
            p.unlink()
    for name in old_subdirs - new_subdirs:
        game_sub = target_dir / name
        if game_sub.is_dir():
            shutil.rmtree(game_sub, ignore_errors=True)

    deployed_files, deployed_subdirs = [], []
    for item in staging_dir.iterdir():
        name = item.name
        if name.lower() in ("version.txt", "optiscaler.ini"):
            continue
        if item.is_file() and name.lower().endswith((".bat", ".sh", ".txt")):
            continue
        if item.is_dir():
            if name.lower() == "licenses":
                continue
            game_sub = target_dir / name
            if game_sub.is_dir():
                shutil.rmtree(game_sub)
            shutil.copytree(item, game_sub)
            deployed_subdirs.append(name)
            continue
        dest_name = installed_dll if name.lower() == "optiscaler.dll" else name
        shutil.copy2(item, target_dir / dest_name)
        if name.lower() != "optiscaler.dll":
            deployed_files.append(name)

    game_ini = target_dir / "OptiScaler.ini"
    staged_ini = staging_dir / "OptiScaler.ini"
    if game_ini.is_file() and staged_ini.is_file():
        user_values = _parse_ini_sections(game_ini)
        staged_values = _parse_ini_sections(staged_ini)
        shutil.copy2(staged_ini, game_ini)
        for section, keys in user_values.items():
            for key, value in keys.items():
                staged_val = staged_values.get(section, {}).get(key)
                if staged_val is not None and staged_val.lower() == value.lower():
                    continue
                set_optiscaler_ini_value(str(target_dir), section, key, value)
    elif not game_ini.is_file() and staged_ini.is_file():
        shutil.copy2(staged_ini, game_ini)

    for key, dll_name in OPTISCALER_DLSS_DLL_NAMES.items():
        try:
            src = get_staged_dlss_dll(key)
            if src:
                shutil.copy2(src, target_dir / dll_name)
        except Exception:
            pass

    rec["version"] = version
    rec["deployed_files"] = deployed_files
    rec["deployed_subdirs"] = deployed_subdirs
    rec["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["rhi_optiscaler_installs"][str(appid)] = rec
    save_state(state)

    if task_id:
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Updated OptiScaler to {version}",
                          "result": {"version": version}}
    return {"updated": True, "version": version}


def _update_optiscaler_task(task_id, appid) -> None:
    try:
        update_optiscaler(appid, task_id=task_id)
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


# --------------------------------------------------------------------------
# RHI port: DXVK variant management (Development/Stable/Lilium HDR)
# --------------------------------------------------------------------------
# RHI's own DXVK+ReShade coexistence relies on switching ReShade to a
# global Vulkan implicit layer - the same Windows-only mechanism Part 1d
# already confirmed dead under Wine/Proton. This port replaces it with the
# rename-based coexistence pattern already shipped for OptiScaler+ReShade
# (rename the conflicting file to ReShade64.dll, restore on removal)
# instead of porting the broken mechanism.
DXVK_DATA_DIR = RHI_DATA_DIR / "dxvk"
DXVK_DEV_DIR = DXVK_DATA_DIR / "development"      # nightly.link master builds, .zip
DXVK_STABLE_DIR = DXVK_DATA_DIR / "stable"        # doitsujin/dxvk tagged releases, .tar.gz
DXVK_LILIUM_DIR = DXVK_DATA_DIR / "lilium"        # EndlesslyFlowering/dxvk HDR fork, .7z
DXVK_VARIANTS = ("development", "stable", "lilium")

DXVK_NIGHTLY_LINK_URL = "https://nightly.link/doitsujin/dxvk/workflows/artifacts/master"
DXVK_STABLE_RELEASES_API = "https://api.github.com/repos/doitsujin/dxvk/releases/latest"
DXVK_LILIUM_RELEASES_API = "https://api.github.com/repos/EndlesslyFlowering/dxvk/releases/latest"

# DX9-11 only - ported from DetermineRequiredDlls, DX8 dropped (RHI's own
# source is internally inconsistent about its DLL name, see plan notes).
DXVK_REQUIRED_DLLS = {
    "d3d9": ["d3d9.dll"],
    "d3d10": ["d3d10core.dll", "dxgi.dll"],
    "d3d11": ["d3d11.dll", "dxgi.dll"],
}

DXVK_DEFAULT_CONF = (
    "dxgi.enableHDR = True\n"
    "dxvk.allowFse = False\n"
    "dxvk.latencySleep = Auto\n"
    "d3d9.dpiAware = True\n"
)

# Ported verbatim from DxvkService.cs's LiliumD3d9Presets/LiliumD3d11Presets
# (real authored data, not invented) - each is a COMPLETE dxvk.conf, no
# base lines prepended. Async is always enabled (RHI is Windows-only there
# too - not a Linux-specific addition).
DXVK_LILIUM_D3D9_PRESETS = [
    ("Safest", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d9.enableSwapChainUpgrade = true
d3d9.upgradeSwapChainFormatTo = rgba16_sfloat
d3d9.upgradeSwapChainColorSpaceTo = scRGB
d3d9.enforceWindowModeInternally = disabled
"""),
    ("2nd Safest", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d9.enableBackBufferUpgrade = true
d3d9.upgradeBackBufferTo = rgba16_unorm
d3d9.enableSwapChainUpgrade = true
d3d9.upgradeSwapChainFormatTo = rgba16_sfloat
d3d9.upgradeSwapChainColorSpaceTo = scRGB
d3d9.enforceWindowModeInternally = disabled
"""),
    ("Slightly Unsafe", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d9.enableSwapChainUpgrade = true
d3d9.upgradeSwapChainFormatTo = rgba16_sfloat
d3d9.upgradeSwapChainColorSpaceTo = scRGB
d3d9.enableBackBufferUpgrade = true
d3d9.upgradeBackBufferTo = rgba16_sfloat
d3d9.enforceWindowModeInternally = disabled
"""),
    ("Unsafer", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d9.enableRenderTargetUpgrades = true
d3d9.upgrade_B5G6R5_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGR5A1_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGR5X1_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGRA4_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGRX4_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_RGBA8_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_RGBX8_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGRA8_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGRX8_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_RGB10A2_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGR10A2_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_RGBA16_UNORM_renderTargetTo = rgba16_unorm
d3d9.enableBackBufferUpgrade = true
d3d9.upgradeBackBufferTo = rgba16_unorm
d3d9.enableSwapChainUpgrade = true
d3d9.upgradeSwapChainFormatTo = rgba16_sfloat
d3d9.upgradeSwapChainColorSpaceTo = scRGB
d3d9.enforceWindowModeInternally = disabled
"""),
    ("Even Unsafer", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d9.enableRenderTargetUpgrades = true
d3d9.upgrade_B5G6R5_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGR5A1_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGR5X1_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGRA4_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGRX4_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_RGBA8_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_RGBX8_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGRA8_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGRX8_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_RGB10A2_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_BGR10A2_UNORM_renderTargetTo = rgba16_unorm
d3d9.upgrade_RGBA16_UNORM_renderTargetTo = rgba16_unorm
d3d9.enableBackBufferUpgrade = true
d3d9.upgradeBackBufferTo = rgba16_sfloat
d3d9.enableSwapChainUpgrade = true
d3d9.upgradeSwapChainFormatTo = rgba16_sfloat
d3d9.upgradeSwapChainColorSpaceTo = scRGB
d3d9.enforceWindowModeInternally = disabled
"""),
    ("Experimental", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d9.enableRenderTargetUpgrades = true
d3d9.upgrade_B5G6R5_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_BGR5A1_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_BGR5X1_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_BGRA4_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_BGRX4_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_RGBA8_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_RGBX8_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_BGRA8_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_BGRX8_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_RGB10A2_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_BGR10A2_UNORM_renderTargetTo = rgba16_sfloat
d3d9.upgrade_RGBA16_UNORM_renderTargetTo = rgba16_sfloat
d3d9.enableBackBufferUpgrade = true
d3d9.upgradeBackBufferTo = rgba16_sfloat
d3d9.enableSwapChainUpgrade = true
d3d9.upgradeSwapChainFormatTo = rgba16_sfloat
d3d9.upgradeSwapChainColorSpaceTo = scRGB
d3d9.enforceWindowModeInternally = disabled
"""),
]

DXVK_LILIUM_D3D11_PRESETS = [
    ("Safest", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d11.enableSwapChainUpgrade = true
d3d11.upgradeSwapChainFormatTo = rgba16_sfloat
d3d11.upgradeSwapChainColorSpaceTo = scRGB
"""),
    ("2nd Safest", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d11.enableBackBufferUpgrade = true
d3d11.upgradeBackBufferTo = rgba16_unorm
d3d11.enableSwapChainUpgrade = true
d3d11.upgradeSwapChainFormatTo = rgba16_sfloat
d3d11.upgradeSwapChainColorSpaceTo = scRGB
"""),
    ("Slightly Unsafe", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d11.enableBackBufferUpgrade = true
d3d11.upgradeBackBufferTo = rgba16_sfloat
d3d11.enableSwapChainUpgrade = true
d3d11.upgradeSwapChainFormatTo = rgba16_sfloat
d3d11.upgradeSwapChainColorSpaceTo = scRGB
"""),
    ("Unsafer", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d11.enableRenderTargetUpgrades = true
d3d11.upgrade_RGBA8_UNORM_renderTargetTo = rgba16_unorm
d3d11.upgrade_BGRA8_UNORM_renderTargetTo = rgba16_unorm
d3d11.upgrade_BGRX8_UNORM_renderTargetTo = rgba16_unorm
d3d11.upgrade_RGBA8_UNORM_SRGB_renderTargetTo = rgba16_unorm
d3d11.upgrade_BGRA8_UNORM_SRGB_renderTargetTo = rgba16_unorm
d3d11.upgrade_BGRX8_UNORM_SRGB_renderTargetTo = rgba16_unorm
d3d11.upgrade_RGBA8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_BGRA8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_BGRX8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_RGB10A2_UNORM_renderTargetTo = rgba16_unorm
d3d11.upgrade_RGB10A2_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.enableBackBufferUpgrade = true
d3d11.upgradeBackBufferTo = rgba16_unorm
d3d11.enableSwapChainUpgrade = true
d3d11.upgradeSwapChainFormatTo = rgba16_sfloat
d3d11.upgradeSwapChainColorSpaceTo = scRGB
"""),
    ("Even Unsafer", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d11.enableRenderTargetUpgrades = true
d3d11.upgrade_RGBA8_UNORM_renderTargetTo = rgba16_unorm
d3d11.upgrade_BGRA8_UNORM_renderTargetTo = rgba16_unorm
d3d11.upgrade_BGRX8_UNORM_renderTargetTo = rgba16_unorm
d3d11.upgrade_RGBA8_UNORM_SRGB_renderTargetTo = rgba16_unorm
d3d11.upgrade_BGRA8_UNORM_SRGB_renderTargetTo = rgba16_unorm
d3d11.upgrade_BGRX8_UNORM_SRGB_renderTargetTo = rgba16_unorm
d3d11.upgrade_RGBA8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_BGRA8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_BGRX8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_RGB10A2_UNORM_renderTargetTo = rgba16_unorm
d3d11.upgrade_RGB10A2_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.enableBackBufferUpgrade = true
d3d11.upgradeBackBufferTo = rgba16_sfloat
d3d11.enableSwapChainUpgrade = true
d3d11.upgradeSwapChainFormatTo = rgba16_sfloat
d3d11.upgradeSwapChainColorSpaceTo = scRGB
"""),
    ("Slightly Experimental", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d11.enableRenderTargetUpgrades = true
d3d11.upgrade_RGBA8_UNORM_renderTargetTo = rgba16_sfloat
d3d11.upgrade_BGRA8_UNORM_renderTargetTo = rgba16_sfloat
d3d11.upgrade_BGRX8_UNORM_renderTargetTo = rgba16_sfloat
d3d11.upgrade_RGBA8_UNORM_SRGB_renderTargetTo = rgba16_sfloat
d3d11.upgrade_BGRA8_UNORM_SRGB_renderTargetTo = rgba16_sfloat
d3d11.upgrade_BGRX8_UNORM_SRGB_renderTargetTo = rgba16_sfloat
d3d11.upgrade_RGBA8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_BGRA8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_BGRX8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_RGB10A2_UNORM_renderTargetTo = rgba16_sfloat
d3d11.upgrade_RGB10A2_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.enableBackBufferUpgrade = true
d3d11.upgradeBackBufferTo = rgba16_sfloat
d3d11.enableSwapChainUpgrade = true
d3d11.upgradeSwapChainFormatTo = rgba16_sfloat
d3d11.upgradeSwapChainColorSpaceTo = scRGB
"""),
    ("Fully Experimental", """dxvk.enableAsync = true
dxvk.gplAsyncCache = true
d3d11.enableRenderTargetUpgrades = true
d3d11.upgrade_RGBA8_UNORM_renderTargetTo = rgba16_sfloat
d3d11.upgrade_BGRA8_UNORM_renderTargetTo = rgba16_sfloat
d3d11.upgrade_BGRX8_UNORM_renderTargetTo = rgba16_sfloat
d3d11.upgrade_RGBA8_UNORM_SRGB_renderTargetTo = rgba16_sfloat
d3d11.upgrade_BGRA8_UNORM_SRGB_renderTargetTo = rgba16_sfloat
d3d11.upgrade_BGRX8_UNORM_SRGB_renderTargetTo = rgba16_sfloat
d3d11.upgrade_RGBA8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_BGRA8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_BGRX8_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_RGB10A2_UNORM_renderTargetTo = rgba16_sfloat
d3d11.upgrade_RGB10A2_TYPELESS_renderTargetTo = rgba16_typeless
d3d11.upgrade_RG11B10_UFLOAT_renderTargetTo = rgba16_sfloat
d3d11.upgrade_RGBA16_UNORM_renderTargetTo = rgba16_sfloat
d3d11.enableBackBufferUpgrade = true
d3d11.upgradeBackBufferTo = rgba16_sfloat
d3d11.enableSwapChainUpgrade = true
d3d11.upgradeSwapChainFormatTo = rgba16_sfloat
d3d11.upgradeSwapChainColorSpaceTo = scRGB
"""),
]


def _dxvk_required_dlls(api) -> list:
    dlls = DXVK_REQUIRED_DLLS.get(api or "")
    if not dlls:
        raise RuntimeError(f"DXVK doesn't support this game's graphics API "
                           f"({api or 'unknown'}) - only D3D9/D3D10/D3D11 are supported.")
    return dlls


def get_dxvk_lilium_conf(api, preset_index=0) -> str:
    presets = DXVK_LILIUM_D3D9_PRESETS if api == "d3d9" else DXVK_LILIUM_D3D11_PRESETS
    if preset_index < 0 or preset_index >= len(presets):
        preset_index = 0
    return presets[preset_index][1]


def _dxvk_staging_dir(variant) -> Path:
    if variant not in DXVK_VARIANTS:
        raise RuntimeError(f"Unknown DXVK variant: {variant}")
    return {"development": DXVK_DEV_DIR, "stable": DXVK_STABLE_DIR, "lilium": DXVK_LILIUM_DIR}[variant]


def dxvk_staging_version(variant):
    p = _dxvk_staging_dir(variant) / "version.txt"
    return p.read_text().strip() if p.is_file() else None


def dxvk_staging_ready(variant) -> bool:
    d = _dxvk_staging_dir(variant)
    return (d / "x64" / "d3d9.dll").is_file() and (d / "version.txt").is_file()


def dxvk_latest(variant) -> dict:
    """Latest DXVK release metadata for one variant, 6h cached. Port of
    EnsureStagingNightlyAsync/EnsureStagingGitHubAsync/EnsureStagingLiliumAsync
    (Staging.cs) - each variant's real source, not a unified API."""
    state = load_state()
    cache_key = f"dxvk_latest_{variant}"
    cache = state.get(cache_key)
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        return cache["data"]

    if variant == "development":
        req = urllib.request.Request(DXVK_NIGHTLY_LINK_URL, headers={"User-Agent": "Mozilla/5.0 pcc"})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
        m = re.search(r'href="(https://nightly\.link/doitsujin/dxvk/[^"]*\.zip)"', html)
        if not m:
            raise RuntimeError("Couldn't find a current DXVK Development build on nightly.link")
        url = m.group(1)
        hm = re.search(r"dxvk-master-([0-9a-f]+)\.zip", url)
        version = hm.group(1) if hm else "nightly-unknown"
        data = {"version": version, "url": url, "asset_name": f"dxvk-master-{version}.zip"}
    elif variant == "stable":
        release = _gh_json(DXVK_STABLE_RELEASES_API)
        asset = next((a for a in release.get("assets", [])
                     if a["name"].lower().endswith(".tar.gz")), None)
        if not asset:
            raise RuntimeError("No .tar.gz asset found in the latest DXVK release")
        data = {"version": release["tag_name"], "url": asset["browser_download_url"],
                "asset_name": asset["name"]}
    elif variant == "lilium":
        release = _gh_json(DXVK_LILIUM_RELEASES_API)
        asset = next((a for a in release.get("assets", [])
                     if a["name"].lower().endswith(".7z")
                     and "gplasync" not in a["name"].lower()), None)
        if not asset:
            raise RuntimeError("No non-gplasync .7z asset found in the latest Lilium HDR release")
        data = {"version": release["tag_name"], "url": asset["browser_download_url"],
                "asset_name": asset["name"]}
    else:
        raise RuntimeError(f"Unknown DXVK variant: {variant}")

    state[cache_key] = {"ts": now, "data": data}
    save_state(state)
    return data


def check_dxvk_update(variant) -> bool:
    try:
        info = dxvk_latest(variant)
    except Exception:
        return False
    return dxvk_staging_ready(variant) and dxvk_staging_version(variant) != info["version"]


def _find_dxvk_content_root(extract_dir, prefer=None) -> Path:
    """Locates the folder containing x64/(+x32/) inside an extracted DXVK
    archive. `prefer` (Lilium's "normal" subfolder, sibling of a gplasync
    variant sometimes present too) is tried first, matching RHI's own
    two-step search (Staging.cs:447-465); falls back to the parent of
    whichever x64/ folder is found anywhere in the tree."""
    if prefer:
        hit = next(iter(extract_dir.rglob(prefer)), None)
        if hit and hit.is_dir():
            return hit
    hit = next(iter(extract_dir.rglob("x64")), None)
    if not hit:
        raise RuntimeError("x64/ folder not found in the extracted DXVK archive "
                           "(its release format may have changed)")
    return hit.parent


def _extract_dxvk_lilium_7z(archive, extract_dir) -> None:
    seven_zip = _find_7z_binary()
    if not seven_zip:
        raise RuntimeError(
            "DXVK's Lilium HDR variant needs the 7-Zip CLI to extract its .7z release - "
            "install it first (Arch: `sudo pacman -S 7zip`, other distros: "
            "the `p7zip` package) then try again.")
    proc = subprocess.run([seven_zip, "x", str(archive), f"-o{extract_dir}", "-y"],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"7z extraction failed: {proc.stderr.strip()[:300]}")


def ensure_dxvk_staging(variant, task_id=None) -> Path:
    """Downloads+extracts the latest DXVK release for one variant. Plain
    zip (Development) and gzip-compressed tar (Stable) use stdlib
    zipfile/tarfile directly - a real simplification over RHI's own code,
    which shells out to 7z for these too since its own environment has no
    stdlib equivalent. Only Lilium HDR's .7z needs the system `7z` binary
    (reused from Part 2's OptiScaler staging, same dependency, nothing
    new)."""
    info = dxvk_latest(variant)
    staging_dir = _dxvk_staging_dir(variant)
    if dxvk_staging_ready(variant) and dxvk_staging_version(variant) == info["version"]:
        return staging_dir

    if variant == "lilium" and not _find_7z_binary():
        raise RuntimeError(
            "DXVK's Lilium HDR variant needs the 7-Zip CLI to extract its .7z release - "
            "install it first (Arch: `sudo pacman -S 7zip`, other distros: "
            "the `p7zip` package) then try again.")

    if task_id:
        TASKS[task_id] = {"status": "running", "progress": 10,
                          "detail": f"Downloading DXVK {variant} {info['version']}"}
    data = _gh_bytes(info["url"], task_id)

    with tempfile.TemporaryDirectory(prefix="pcc_dxvk_") as tmp:
        tmp = Path(tmp)
        archive = tmp / info["asset_name"]
        archive.write_bytes(data)
        extract_dir = tmp / "extracted"
        extract_dir.mkdir()
        if task_id:
            TASKS[task_id]["detail"] = "Extracting"
            TASKS[task_id]["progress"] = 75

        if variant == "lilium":
            _extract_dxvk_lilium_7z(archive, extract_dir)
            source_root = _find_dxvk_content_root(extract_dir, prefer="normal")
        elif info["asset_name"].lower().endswith(".tar.gz"):
            import tarfile
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(extract_dir, filter="data")
            source_root = _find_dxvk_content_root(extract_dir)
        else:
            import zipfile
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_dir)
            source_root = _find_dxvk_content_root(extract_dir)

        if staging_dir.is_dir():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)
        for arch in ("x64", "x32"):
            src = source_root / arch
            if src.is_dir():
                shutil.copytree(src, staging_dir / arch)

    (staging_dir / "version.txt").write_text(info["version"])
    if task_id:
        TASKS[task_id]["detail"] = f"DXVK {info['version']} staged"
        TASKS[task_id]["progress"] = 90
    return staging_dir


def _is_dxvk_file(path) -> bool:
    """Byte-scan for DXVK's signature strings in the first 2MB - port of
    IsDxvkFileStatic (DxvkService.cs:601-629)."""
    try:
        with Path(path).open("rb") as f:
            data = f.read(2 * 1024 * 1024)
    except OSError:
        return False
    return b"dxvk" in data or b"DXVK_" in data


def detect_dxvk_installation(install_path, api) -> str | None:
    """Scans the DLL names required for one graphics API for DXVK's
    signature, in both the game root and OptiScaler/plugins/ - port of
    DetectInstallation (Install.cs:706-765)."""
    base = Path(install_path)
    if not base.is_dir():
        return None
    try:
        candidates = _dxvk_required_dlls(api)
    except RuntimeError:
        candidates = ["d3d9.dll", "d3d10core.dll", "d3d11.dll", "dxgi.dll"]
    for dll in candidates:
        if _is_dxvk_file(base / dll):
            return dll
    plugins_dir = base / "OptiScaler" / "plugins"
    if plugins_dir.is_dir():
        for dll in candidates:
            if _is_dxvk_file(plugins_dir / dll):
                return dll
    return None


def _resolve_dxvk_dll_targets(required_dlls, install_path) -> tuple:
    """Splits required DXVK DLLs into (root_dlls, plugin_dlls): any DLL
    whose filename collides with OptiScaler's own installed filename is
    routed to OptiScaler/plugins/ instead of the game root - port of
    ResolveDeploymentPaths (DxvkService.cs:558-582)."""
    os_installed = detect_optiscaler_installation(install_path)
    root_dlls, plugin_dlls = [], []
    for dll in required_dlls:
        if os_installed and dll.lower() == os_installed.lower():
            plugin_dlls.append(dll)
        else:
            root_dlls.append(dll)
    return root_dlls, plugin_dlls


def scan_game_dxvk(appid, install_path, exe_path=None, game_name=None) -> dict:
    """DXVK status for one game - detected graphics API (reused from Part
    1a), whatever PCC has on record, whether a newer release is staged than
    what's installed, and whether this game is on the anti-cheat DXVK
    blacklist (so the UI can warn/disable before the user even tries)."""
    state = load_state()
    rec = state.get("rhi_dxvk_installs", {}).get(str(appid))
    if not exe_path and rec and rec.get("exe"):
        exe_path = rec["exe"]
    exe = Path(exe_path) if exe_path else _find_game_exe(install_path)
    detected = detect_game_graphics_api(exe) if exe else {"bitness": None, "api": None}
    display = describe_graphics_api(detected["api"], exe) if exe else {"label": None, "inferred": False}
    result = {"exe": str(exe) if exe else None, "detected_api": detected["api"],
             "detected_api_display": display["label"], "detected_api_inferred": display["inferred"],
             "detected_bitness": detected["bitness"], "installed": False,
             "update_available": False,
             "blacklisted": is_dxvk_blacklisted(game_name)}
    if rec:
        p = Path(rec["install_path"])
        installed = all((p / dll).is_file() or (p / "OptiScaler" / "plugins" / dll).is_file()
                        for dll in rec.get("installed_dlls", []) + rec.get("plugin_dlls", []))
        result.update({"installed": installed, "install_path": rec["install_path"],
                       "variant": rec.get("variant"), "api": rec.get("api"),
                       "lilium_preset": rec.get("lilium_preset"), "version": rec.get("version")})
        if installed:
            try:
                result["update_available"] = check_dxvk_update(rec["variant"])
            except Exception:
                pass
    return result


def install_dxvk(appid, install_path, variant, exe_override=None, lilium_preset=0,
                 task_id=None, game_name=None) -> dict:
    """Installs DXVK for one game: resolves the required DLLs from the
    detected graphics API, routes any DLL that collides with an installed
    OptiScaler's filename to OptiScaler/plugins/, renames a same-directory
    ReShade out of the way first if it would otherwise collide, deploys
    the staged build + dxvk.conf. Refuses outright (no override) for a game
    on manifest.json's dxvkBlacklist - titles with anti-cheat software that
    can flag or ban DXVK's presence, matching RHI's own toggle-disable
    behavior for these games (GameCardViewModel.Dxvk.cs's IsDxvkBlacklisted)."""
    if variant not in DXVK_VARIANTS:
        raise RuntimeError(f"Unknown DXVK variant: {variant}")
    if is_dxvk_blacklisted(game_name):
        raise RuntimeError(
            f"DXVK is blocked for {game_name} - this game's anti-cheat software "
            "can flag or ban players for DXVK's presence, even unused. This isn't "
            "overridable from here.")
    exe = Path(exe_override).expanduser() if exe_override else _find_game_exe(install_path)
    if not exe or not exe.is_file():
        raise RuntimeError("Couldn't find the game's .exe under its install folder — "
                           "point Command Center at it manually.")
    detected = detect_game_graphics_api(exe)
    api = detected["api"]
    required_dlls = _dxvk_required_dlls(api)
    bitness = detected["bitness"] or 64
    arch = "x32" if bitness == 32 else "x64"
    target_dir = exe.parent

    staging_dir = ensure_dxvk_staging(variant, task_id=task_id)
    version = dxvk_staging_version(variant)
    for dll in required_dlls:
        if not (staging_dir / arch / dll).is_file():
            raise RuntimeError(f"Staged DLL not found: {dll} ({arch}) - try removing and "
                               "reinstalling DXVK.")

    root_dlls, plugin_dlls = _resolve_dxvk_dll_targets(required_dlls, str(target_dir))

    # ReShade coexistence - same directory-scoped rename pattern as
    # Part 2's OptiScaler+ReShade fix, not RHI's Vulkan-layer switch (that
    # needs a global Windows Vulkan implicit-layer registration, which has
    # no Wine/Proton equivalent - see OPTISCALER_DLSS_MANIFEST_URL area for
    # the coexistence design notes). Renaming ReShade to ReShade64.dll only
    # actually keeps it working if something in this same folder chainloads
    # that filename - today that's OptiScaler alone (see
    # _resolve_reshade_reclaim_name's docstring). Without OptiScaler
    # present, renaming would silently orphan a working ReShade install
    # (nothing loads ReShade64.dll), so this refuses instead - install
    # OptiScaler too (which DOES chainload it), or remove ReShade first.
    state = load_state()
    rs_rec = state.get("rhi_reshade_installs", {}).get(str(appid))
    reshade_renamed = False
    if rs_rec:
        rs_path = Path(rs_rec["path"])
        if (rs_path.is_file() and rs_path.parent == target_dir
                and rs_path.name.lower() in {d.lower() for d in root_dlls}
                and rs_path.name.lower() != "reshade64.dll"):
            os_rec = state.get("rhi_optiscaler_installs", {}).get(str(appid))
            os_installed = bool(os_rec and Path(os_rec.get("install_path", "")) == target_dir
                                and (target_dir / os_rec.get("installed_as", "")).is_file())
            if not os_installed:
                raise RuntimeError(
                    f"DXVK needs {rs_path.name} for this game, which is where ReShade is "
                    "currently installed. Nothing would load ReShade if it got renamed "
                    "aside here (that only works when OptiScaler is also installed, since "
                    "OptiScaler is what chainloads a renamed ReShade64.dll) - install "
                    "OptiScaler for this game first, or remove ReShade, then retry.")
            rs_dest = target_dir / "ReShade64.dll"
            if rs_dest.exists():
                rs_dest.unlink()
            rs_path.rename(rs_dest)
            rs_rec["path"] = str(rs_dest)
            save_state(state)
            reshade_renamed = True

    if task_id and task_id in TASKS:
        TASKS[task_id]["detail"] = "Deploying DXVK files"
        TASKS[task_id]["progress"] = 80

    backed_up_files = []
    for dll in root_dlls:
        src = staging_dir / arch / dll
        dest = target_dir / dll
        if dest.is_file() and dest.name.lower() not in {d.lower() for d in backed_up_files}:
            ident = _identify_dxgi_file(dest)
            if ident == "unknown":
                _backup_original_if_exists(dest)
                backed_up_files.append(dll)
            elif ident not in ("reshade", "optiscaler", "dxvk"):
                _backup_original_if_exists(dest)
                backed_up_files.append(dll)
        shutil.copy2(src, dest)

    if plugin_dlls:
        plugins_dir = target_dir / "OptiScaler" / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        for dll in plugin_dlls:
            shutil.copy2(staging_dir / arch / dll, plugins_dir / dll)

    conf_path = target_dir / "dxvk.conf"
    conf_content = (get_dxvk_lilium_conf(api, lilium_preset) if variant == "lilium"
                    else DXVK_DEFAULT_CONF)
    conf_path.write_text(conf_content)

    state = load_state()
    installs = state.setdefault("rhi_dxvk_installs", {})
    installs[str(appid)] = {
        "install_path": str(target_dir), "variant": variant, "api": api, "bitness": bitness,
        "installed_dlls": root_dlls, "plugin_dlls": plugin_dlls,
        "backed_up_files": backed_up_files, "deployed_conf": True,
        "lilium_preset": lilium_preset if variant == "lilium" else None,
        "reshade_renamed": reshade_renamed, "version": version, "exe": str(exe),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_state(state)

    if task_id:
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Installed DXVK {variant} {version}",
                          "result": {"version": version}}
    return {"installed": True, "variant": variant, "version": version, "api": api,
            "installed_dlls": root_dlls, "plugin_dlls": plugin_dlls}


def _install_dxvk_task(task_id, appid, install_path, variant, exe, lilium_preset,
                       game_name=None) -> None:
    try:
        install_dxvk(appid, install_path, variant, exe_override=exe,
                    lilium_preset=lilium_preset, task_id=task_id, game_name=game_name)
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


def remove_dxvk(appid) -> dict:
    """Uninstalls DXVK for one game: deletes the deployed DLLs (root +
    OptiScaler/plugins/), restores any true game-original `.original`
    backups, deletes dxvk.conf if PCC deployed it, restores ReShade's
    filename if it was parked at ReShade64.dll for coexistence."""
    state = load_state()
    installs = state.get("rhi_dxvk_installs", {})
    rec = installs.pop(str(appid), None)
    if not rec:
        raise RuntimeError("No DXVK install tracked for this game.")
    target_dir = Path(rec["install_path"])

    for dll in rec.get("installed_dlls", []):
        p = target_dir / dll
        if p.is_file():
            p.unlink()
            if dll in rec.get("backed_up_files", []):
                _restore_original_if_exists(p)

    plugin_dlls = rec.get("plugin_dlls", [])
    if plugin_dlls:
        plugins_dir = target_dir / "OptiScaler" / "plugins"
        for dll in plugin_dlls:
            p = plugins_dir / dll
            p.unlink(missing_ok=True)
        if plugins_dir.is_dir() and not any(plugins_dir.iterdir()):
            plugins_dir.rmdir()

    if rec.get("deployed_conf"):
        (target_dir / "dxvk.conf").unlink(missing_ok=True)

    if rec.get("reshade_renamed"):
        rs_rec = state.get("rhi_reshade_installs", {}).get(str(appid))
        rs_coexist_path = target_dir / "ReShade64.dll"
        if rs_rec and rs_coexist_path.is_file():
            reclaim_name = _resolve_reshade_reclaim_name(rs_rec, rec)
            resolved_path = target_dir / reclaim_name
            if reclaim_name.lower() != "reshade64.dll" and not resolved_path.is_file():
                rs_coexist_path.rename(resolved_path)
                rs_rec["path"] = str(resolved_path)
                state.setdefault("rhi_reshade_installs", {})[str(appid)] = rs_rec

    save_state(state)
    return {"removed": True}


def update_dxvk(appid, task_id=None) -> dict:
    """Updates an installed DXVK to the latest staged build for the same
    variant: re-stages if needed, redeploys the recorded DLL set (no
    backups - these are all previously DXVK-owned), rewrites dxvk.conf."""
    state = load_state()
    rec = state.get("rhi_dxvk_installs", {}).get(str(appid))
    if not rec:
        raise RuntimeError("No DXVK install tracked for this game.")
    variant = rec["variant"]
    target_dir = Path(rec["install_path"])
    arch = "x32" if rec.get("bitness") == 32 else "x64"

    staging_dir = ensure_dxvk_staging(variant, task_id=task_id)
    version = dxvk_staging_version(variant)

    if task_id and task_id in TASKS:
        TASKS[task_id]["detail"] = "Updating DXVK files"
        TASKS[task_id]["progress"] = 60

    for dll in rec.get("installed_dlls", []):
        src = staging_dir / arch / dll
        if src.is_file():
            shutil.copy2(src, target_dir / dll)

    plugin_dlls = rec.get("plugin_dlls", [])
    if plugin_dlls:
        plugins_dir = target_dir / "OptiScaler" / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        for dll in plugin_dlls:
            src = staging_dir / arch / dll
            if src.is_file():
                shutil.copy2(src, plugins_dir / dll)

    if rec.get("deployed_conf"):
        conf_content = (get_dxvk_lilium_conf(rec.get("api"), rec.get("lilium_preset") or 0)
                        if variant == "lilium" else DXVK_DEFAULT_CONF)
        (target_dir / "dxvk.conf").write_text(conf_content)

    rec["version"] = version
    rec["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["rhi_dxvk_installs"][str(appid)] = rec
    save_state(state)

    if task_id:
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Updated DXVK to {version}",
                          "result": {"version": version}}
    return {"updated": True, "version": version}


def _update_dxvk_task(task_id, appid) -> None:
    try:
        update_dxvk(appid, task_id=task_id)
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


def reset_dxvk_conf(appid) -> dict:
    """Rewrites dxvk.conf back to PCC's default template for this game's
    tracked variant/API/preset, discarding any manual edits - the same
    content update_dxvk() would write, without re-downloading or
    redeploying any DLLs."""
    state = load_state()
    rec = state.get("rhi_dxvk_installs", {}).get(str(appid))
    if not rec:
        raise RuntimeError("No DXVK install tracked for this game.")
    target_dir = Path(rec["install_path"])
    variant = rec.get("variant", "stable")
    conf_content = (get_dxvk_lilium_conf(rec.get("api"), rec.get("lilium_preset") or 0)
                    if variant == "lilium" else DXVK_DEFAULT_CONF)
    (target_dir / "dxvk.conf").write_text(conf_content)
    return {"reset": True}


# --------------------------------------------------------------------------
# Owned library (community profile XML - no API key needed)
# --------------------------------------------------------------------------

def get_steamid64(root: Path):
    """Most recent login's SteamID64 from config/loginusers.vdf."""
    lu = root / "config/loginusers.vdf"
    if not lu.is_file():
        return None
    try:
        data = vdf_parse(lu.read_text(errors="replace"))
    except Exception:
        return None
    users = ci_get(data, "users") or {}
    best, best_ts = None, -1
    for sid, meta in users.items():
        if not isinstance(meta, dict):
            continue
        if ci_get(meta, "MostRecent") == "1":
            return sid
        ts = int(ci_get(meta, "Timestamp") or 0)
        if ts > best_ts:
            best, best_ts = sid, ts
    return best


def fetch_owned_games(root: Path, force=False) -> dict:
    """All games the user owns, via the public community profile XML.
    Cached in state.json for 6 hours. Returns {'games': [...], 'error': str|None}."""
    state = load_state()
    cache = state.get("owned", {})
    if not force and cache.get("games") and \
            time.time() - cache.get("ts", 0) < 6 * 3600:
        return {"games": cache["games"], "error": None, "cached": True}
    sid = get_steamid64(root)
    if not sid:
        return {"games": cache.get("games", []),
                "error": "Couldn't find a Steam login in loginusers.vdf"}
    url = f"https://steamcommunity.com/profiles/{sid}/games?tab=all&xml=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "proton-command-center"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        tree = ET.fromstring(raw)
        if tree.find("error") is not None:
            return {"games": cache.get("games", []),
                    "error": tree.findtext("error")}
        games = []
        for g in tree.iter("game"):
            appid = g.findtext("appID")
            name = g.findtext("name")
            if appid and name:
                games.append({"appid": appid, "name": name})
        if not games:
            return {"games": cache.get("games", []),
                    "error": "Profile returned no games — set 'Game details' "
                             "to Public in Steam privacy settings"}
        state["owned"] = {"ts": int(time.time()), "games": games}
        save_state(state)
        return {"games": games, "error": None}
    except Exception as e:
        return {"games": cache.get("games", []),
                "error": f"Couldn't reach steamcommunity.com: {e}"}



# --------------------------------------------------------------------------
# Auto-tune: engine detection + curated tuning rules
# --------------------------------------------------------------------------
NING_RULES = {
    "base_env": {"PROTON_ENABLE_NVAPI": "1", "PROTON_USE_NTSYNC": "1"},
    "base_wrappers": ["gamemoderun"],
    "vram_cap_below_mb": 12000,
    "engines": {
        "unreal4": {
            "env": {},
            "notes": ["UE4: PSO stutter is the usual hitching cause — let Steam "
                      "finish its shader pass before judging performance",
                      "If hitching persists, cap FPS a few frames below refresh "
                      "(frame pacing) and test with Frame Generation OFF"],
        },
        "unreal5": {
            "env": {},
            "notes": ["UE5: PSO/traversal stutter is common — let Steam finish "
                      "its shader pass, then judge frame pacing",
                      "Frame Generation can worsen UE5 frame pacing — A/B test "
                      "with the Benchmark tab",
                      "If traversal stutter remains, an fps cap (DXVK_FRAME_RATE) "
                      "slightly under refresh smooths delivery"],
        },
        "unity": {"env": {}, "notes": ["Unity titles are usually clean under "
                                       "Proton — Wayland + HDR safe to enable"]},
        "re-engine": {"env": {}, "notes": ["RE Engine runs well by default; "
                                           "enable HDR if your display supports it"]},
        "source": {"env": {}, "notes": []},
        "source2": {"env": {}, "notes": []},
        "godot": {"env": {}, "notes": []},
        "gamemaker": {"env": {}, "notes": []},
    },
    "name_overrides": [
        {"match": "stellar blade",
         "env": {"PROTON_ENABLE_HDR": "1", "DXVK_HDR": "1"},
         "desktop_env": {"SteamDeck": "0"},
         "vram_cap": True,
         "notes": ["Known VRAM-pressure title: memory cap applied to prevent "
                   "eviction hitching",
                   "Engine.ini pool-size tweaks help further (r.Streaming settings)"]},
        {"match": "mortal shell",
         "env": {},
         "vram_cap": True,
         "fps_cap_hint": True,
         "notes": ["Rhythmic hitching profile: let shaders warm, test with "
                   "FG off, VRAM cap applied",
                   "If hitching survives all three, capture a Benchmark run and "
                   "check stutter % before/after each change"]},
    ],
}


MI = ("jupiter", "galileo", "rog ally", "legion go", "ayaneo",
                "gpd win", "onexplayer", "steam deck")


def protondb_cached(appid: str):
    """Return a previously-fetched ProtonDB rating from state without ever
    hitting the network. Used to repopulate the badge when the app reopens, so
    a rating the user already checked stays visible. Returns None if never
    checked."""
    state = load_state()
    cache = state.get("protondb", {}).get(str(appid))
    if cache:
        return cache.get("data")
    return None


def protondb_summary(appid: str):
    """Community compatibility tier from ProtonDB (cached 24h)."""
    state = load_state()
    cache = state.get("protondb", {}).get(str(appid))
    if cache and time.time() - cache.get("ts", 0) < 86400:
        return cache.get("data")
    try:
        req = urllib.request.Request(
            f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json",
            headers={"User-Agent": "proton-command-center"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        out = {"tier": data.get("tier"), "confidence": data.get("confidence"),
               "total": data.get("total")}
    except Exception:
        out = None
    state.setdefault("protondb", {})[str(appid)] = {"ts": time.time(), "data": out}
    save_state(state)
    return out


OMPAT_TOOLS = [
    ("", "Steam default"),
    ("proton_experimental", "Proton Experimental"),
    ("proton_hotfix", "Proton Hotfix"),
    ("proton_9", "Proton 9.0"),
    ("proton_10", "Proton 10.0"),
]


# --------------------------------------------------------------------------
# Per-build environment variable support
# --------------------------------------------------------------------------
# Proton builds differ in what they understand. GE-Proton11-1 reads 29 vars
# that Valve's Proton 11.0 has never heard of (PROTON_ENABLE_WAYLAND and
# DXVK_HDR among them), so a launch string that's correct under GE can be
# silently inert under Valve's build.
#
# Each build ships its launcher as a plain Python script, so the variables it
# reads can just be scanned out of it. That's local, offline, and needs no
# hardcoded table to go stale as builds move on.
#
# The catch: the scan can only see what the *script* reads. Variables consumed
# further down the stack are invisible to it - DXVK_NVAPI_VKREFLEX is read by
# the dxvk-nvapi DLL and appears in no proton script, yet works fine. Treating
# "not found" as "unsupported" would wrongly disable it.
#
# So absence is only meaningful for a variable we can demonstrably detect
# elsewhere. The union across every installed build is our evidence of what the
# technique can see; anything outside that union we simply don't know about, and
# unknown must mean "leave it alone" rather than "disable it".
PROTON_ENV_RE = re.compile(
    r"\b((?:PROTON|DXVK|VKD3D|WINE|WINEALSA|WINEDLL|MANGOHUD)_[A-Z0-9_]{2,})\b")


def proton_env_vars(tool_dir: Path) -> set:
    """Environment variables a build's launcher script reads."""
    script = Path(tool_dir) / "proton"
    if not script.is_file():
        return set()
    try:
        return set(PROTON_ENV_RE.findall(script.read_text(errors="replace")))
    except OSError:
        return set()


def _official_slug(dir_name: str):
    """Steam's internal name for an official Proton build directory.

    Steam uses a slug for its own builds but the plain directory name for
    custom ones, which is why no single rule ever matched both. Verified
    against a real CompatToolMapping in config.vdf:
        "Proton 11.0"           -> proton_11
        "Proton - Experimental" -> proton_experimental
        "Proton Hotfix"         -> proton_hotfix
        "GE-Proton11-1"         -> GE-Proton11-1   (custom: name as-is)

    Returns None for anything not matching a confirmed pattern. That's
    deliberate: emitting a guessed slug would write a name Steam doesn't
    recognise and silently break the game's Proton setting, which is worse
    than leaving the build out of the list.
    """
    n = " ".join(dir_name.split()).lower()
    if n in ("proton - experimental", "proton experimental"):
        return "proton_experimental"
    if n == "proton hotfix":
        return "proton_hotfix"
    m = re.fullmatch(r"proton (\d+)\.0", n)
    return "proton_" + m.group(1) if m else None


def _custom_tool_name(tool_dir: Path):
    """A custom tool's internal name + label, as declared in its own
    compatibilitytool.vdf. Custom builds are authoritative about their name -
    only the official ones need a slug derived."""
    vdf = tool_dir / "compatibilitytool.vdf"
    if not vdf.is_file():
        return None
    try:
        data = vdf_parse(vdf.read_text(errors="replace"))
        compat = ci_get(data, "compatibilitytools") or {}
        compat = ci_get(compat, "compat_tools") or {}
        for internal, meta in compat.items():
            label = (meta.get("display_name", internal)
                     if isinstance(meta, dict) else internal)
            return internal, label
    except Exception:
        pass
    return None


def _compat_dirs(root: Path):
    """Every compatibilitytools.d Steam scans, including system packages
    (CachyOS installs proton-cachyos to /usr/share/steam/compatibilitytools.d)
    and STEAM_EXTRA_COMPAT_TOOLS_PATHS."""
    dirs = [root / "compatibilitytools.d",
            Path.home() / ".steam/root/compatibilitytools.d",
            Path("/usr/share/steam/compatibilitytools.d"),
            Path("/usr/local/share/steam/compatibilitytools.d")]
    for extra in os.environ.get("STEAM_EXTRA_COMPAT_TOOLS_PATHS", "").split(":"):
        if extra:
            dirs.append(Path(extra))
    return dirs


def _tool_dirs(root: Path) -> dict:
    """Installed builds, keyed by the name Steam uses in CompatToolMapping.

    Keying on the Steam name (not the directory) means capability lookups are
    an exact match against what the compat selector holds, instead of the
    fuzzy label matching that quietly matched nothing.
    """
    out = {}
    common = root / "steamapps/common"
    if common.is_dir():
        for sub in sorted(common.iterdir()):
            if (sub / "proton").is_file():
                slug = _official_slug(sub.name)
                if slug:
                    out[slug] = sub
    scanned = set()
    for d in _compat_dirs(root):
        try:
            rd = d.resolve()
        except OSError:
            continue
        if rd in scanned or not d.is_dir():
            continue
        scanned.add(rd)
        for sub in sorted(d.iterdir()):
            if not (sub / "proton").is_file():
                continue
            found = _custom_tool_name(sub)
            out[found[0] if found else sub.name] = sub
    return out


def proton_capabilities(root: Path) -> dict:
    """Which env vars each installed build supports, and how sure we are.

    `known` is the union over all builds: the vars this scan can actually see.
    A var in `known` but missing from a build is genuinely unsupported there.
    A var outside `known` is invisible to us, not absent - callers must leave
    those enabled.
    """
    tool_dirs = _tool_dirs(root)
    per_tool = {name: sorted(proton_env_vars(d)) for name, d in tool_dirs.items()}
    known = sorted(set().union(*(set(v) for v in per_tool.values()))
                   if per_tool else [])
    dlss_presets = {name: nvapi_dll_dlss_support(d) for name, d in tool_dirs.items()}
    return {"tools": per_tool, "known": known, "dlss_presets": dlss_presets,
            "dlss_preset_ceiling": NGX_RENDER_PRESETS}


def list_compat_tools(root: Path):
    """Only builds actually present on disk.

    The old hardcoded list offered proton_9 and proton_10 whether or not they
    existed, and stopped at 10 - so a real Proton 11.0 install couldn't be
    selected at all, while two builds that weren't installed could be. Reading
    the disk fixes the phantoms and the staleness together, and means new
    Proton releases appear on their own.
    """
    tools = [{"name": "", "label": "Steam default", "custom": False}]
    common = root / "steamapps/common"
    if common.is_dir():
        for sub in sorted(common.iterdir()):
            if not (sub / "proton").is_file():
                continue
            slug = _official_slug(sub.name)
            if slug:
                tools.append({"name": slug, "label": sub.name, "custom": False})
    seen = {t["name"] for t in tools}
    scanned = set()
    for d in _compat_dirs(root):
        try:
            rd = d.resolve()
        except OSError:
            continue
        if rd in scanned or not d.is_dir():
            continue
        scanned.add(rd)
        for sub in sorted(d.iterdir()):
            # Deliberately NOT requiring a "proton" script here: not every
            # Steam compat tool is Proton (Luxtorpeda, Boxtron and friends
            # declare a compatibilitytool.vdf and have no proton script), and
            # demanding one would drop them from the selector entirely. Only
            # the capability scan needs the script.
            found = _custom_tool_name(sub)
            if not found:
                continue
            name, label = found
            if name in seen:
                continue
            seen.add(name)
            tools.append({"name": name, "label": label, "custom": True})
    return tools


def _config_vdf(root: Path):
    return root / "config/config.vdf"


def _compat_mapping_node(data, create=False):
    store = (ci_ensure(data, "InstallConfigStore") if create
             else ci_get(data, "InstallConfigStore"))
    if store is None:
        return None
    sw = ci_ensure(store, "Software") if create else ci_get(store, "Software")
    if sw is None:
        return None
    valve = ci_ensure(sw, "Valve") if create else ci_get(sw, "Valve")
    if valve is None:
        return None
    steam = ci_ensure(valve, "Steam") if create else ci_get(valve, "Steam")
    if steam is None:
        return None
    return (ci_ensure(steam, "CompatToolMapping") if create
            else ci_get(steam, "CompatToolMapping"))


def get_compat_tool(root: Path, appid: str) -> dict:
    cfg = _config_vdf(root)
    if not cfg.is_file():
        return {"name": "", "source": None}
    try:
        data = vdf_parse(cfg.read_text(errors="replace"))
    except Exception:
        return {"name": "", "source": None}
    mapping = _compat_mapping_node(data) or {}
    entry = ci_get(mapping, str(appid))
    if isinstance(entry, dict):
        return {"name": entry.get("name", ""), "source": str(cfg)}
    return {"name": "", "source": str(cfg)}


def set_compat_tool(root: Path, appid: str, tool_name, close_steam=False) -> dict:
    if steam_running():
        if close_steam:
            shutdown_steam()
        else:
            raise RuntimeError("Steam is running. Close Steam first — it "
                               "overwrites config.vdf on exit.")
    cfg = _config_vdf(root)
    if not cfg.is_file():
        raise RuntimeError(f"config.vdf not found at {cfg}")
    data = vdf_parse(cfg.read_text(errors="replace"))
    mapping = _compat_mapping_node(data, create=True)
    for k in list(mapping.keys()):          # drop existing entry, any case
        if k == str(appid):
            del mapping[k]
    if tool_name:
        mapping[str(appid)] = {"name": tool_name, "config": "",
                               "priority": "250"}
    bak = cfg.with_suffix(f".vdf.pcc-{int(time.time())}.bak")
    shutil.copy2(cfg, bak)
    tmp = cfg.with_suffix(".vdf.pcc-tmp")
    tmp.write_text(vdf_dump(data))
    tmp.replace(cfg)
    return {"saved": True, "tool": tool_name, "backup": str(bak)}


# --------------------------------------------------------------------------
# Non-Steam game shortcuts (shortcuts.vdf) - "add your own game"
# --------------------------------------------------------------------------
def shortcuts_path(root: Path) -> Path | None:
    """shortcuts.vdf lives next to localconfig.vdf, per user. Reuses the same
    userdata/<id>/config discovery as everything else here instead of
    computing the SteamID3 folder name ourselves."""
    cfgs = find_localconfigs(root)
    return cfgs[0].parent / "shortcuts.vdf" if cfgs else None


def _find_shortcut(root: Path, appid: str):
    """Locate a shortcut entry by its unsigned Steam app id. Non-Steam games
    don't have a localconfig.vdf `apps.<appid>` node at all - Steam reads
    their launch options straight off the shortcut entry itself - so callers
    that need to read/write launch options must check here first. Returns
    (parsed_shortcuts_data, shortcuts_path, dict_key) or (None, None, None)."""
    path = shortcuts_path(root)
    if not path or not path.is_file():
        return None, None, None
    try:
        data = binvdf_parse(path.read_bytes())
    except Exception:
        return None, None, None
    for key, entry in (data.get("shortcuts") or {}).items():
        if isinstance(entry, dict) and entry.get("appid") is not None:
            if str(entry["appid"] & 0xFFFFFFFF) == str(appid):
                return data, path, key
    return None, None, None


def compute_shortcut_id(exe: str, appname: str):
    """Steam's own convention for a non-Steam shortcut's App ID: crc32 of the
    (already-quoted) exe path concatenated with the app name, OR'd with the
    high bit. Verified against a real shortcut Steam itself created on this
    box - its grid art is filed under exactly this unsigned value. Returns
    (unsigned_top32, signed_int32_for_storage)."""
    top32 = zlib.crc32((exe + appname).encode("utf-8")) | 0x80000000
    signed = struct.unpack("<i", struct.pack("<I", top32))[0]
    return top32, signed


def _dir_size_bytes(path, max_files=200_000) -> int:
    """Total size of every file under path. Steam tracks SizeOnDisk itself
    for real games (just a VDF field read, no disk I/O) - a non-Steam
    shortcut has no such record since Steam didn't install it, so this has
    to actually walk the folder. Capped at max_files entries as a sanity
    bound against a pathological tree (network mount, symlink loop, etc.) -
    returns whatever's been summed so far rather than hanging indefinitely."""
    total = 0
    count = 0
    try:
        for entry in Path(path).rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
                count += 1
                if count >= max_files:
                    break
    except OSError:
        pass
    return total


def _shortcut_size_bytes(appid, start_dir) -> int:
    """Cached (1h) on-disk size for a non-Steam shortcut's install folder -
    walking a whole game folder on every /api/games poll would be far too
    slow for a large library, so this is recomputed at most hourly per
    appid rather than on every call."""
    state = load_state()
    cache = state.setdefault("shortcut_size_cache", {})
    entry = cache.get(str(appid))
    now = time.time()
    if entry and now - entry.get("ts", 0) < 3600:
        return entry.get("size", 0)
    size = _dir_size_bytes(start_dir)
    cache[str(appid)] = {"ts": now, "size": size}
    save_state(state)
    return size


def list_shortcuts(root: Path) -> list:
    """Every non-Steam shortcut Steam knows about, already in Command
    Center's game shape (appid/name/install_path/library/...) so it merges
    straight into the normal library list via all_games(). `library` points
    at the same steamapps Steam itself uses for a shortcut's Proton compat
    data, so per-game lookups resolve unmodified."""
    path = shortcuts_path(root)
    if not path or not path.is_file():
        return []
    try:
        data = binvdf_parse(path.read_bytes())
    except Exception:
        return []
    out = []
    for entry in (data.get("shortcuts") or {}).values():
        if not isinstance(entry, dict) or entry.get("appid") is None:
            continue
        top32 = entry["appid"] & 0xFFFFFFFF
        exe = entry.get("Exe", "")
        exe_unquoted = exe.strip('"')
        start_dir = entry.get("StartDir", "").strip('"') or str(Path(exe_unquoted).parent)
        out.append({
            "appid": str(top32),
            "name": entry.get("AppName") or exe_unquoted or str(top32),
            "install_path": start_dir,
            "installed": True,
            "fully_installed": True,
            "download_pct": None,
            "size_bytes": _shortcut_size_bytes(top32, start_dir),
            "library": str(root / "steamapps"),
            "custom": True,
            "exe": exe_unquoted,
            "launch_options_shortcut": entry.get("LaunchOptions", ""),
        })
    return out


def all_games(root: Path) -> list:
    """Installed Steam games plus non-Steam shortcuts, merged - the single
    list every per-game route (dlss/cache/launch options/...) should look up
    against so a custom game gets the same treatment as a real one."""
    return list_games(root) + list_shortcuts(root)


def _new_shortcut_entry(name, quoted_exe, start_dir, launch_options, signed_appid):
    # Field set/order matches a real Steam-written entry exactly (including
    # the empty "tags" object) - captured from this box's own shortcuts.vdf.
    return {
        "appid": signed_appid,
        "AppName": name,
        "Exe": quoted_exe,
        "StartDir": start_dir,
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": launch_options or "",
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "sortas": "",
        "tags": {},
    }


def add_shortcut(root: Path, name: str, exe: str, start_dir: str = "",
                 launch_options: str = "", close_steam=False) -> dict:
    """Add a non-Steam game shortcut. Steam must be closed - like every other
    write here, it rewrites shortcuts.vdf on exit and would clobber this."""
    if steam_running():
        if close_steam:
            shutdown_steam()
        else:
            raise RuntimeError("Steam is running. Close Steam first — it "
                               "overwrites shortcuts.vdf on exit.")
    name = name.strip()
    exe = exe.strip()
    if not name:
        raise RuntimeError("Name is required")
    if not exe:
        raise RuntimeError("Choose an executable first")
    quoted_exe = exe if exe.startswith('"') else f'"{exe}"'
    start_dir = start_dir.strip() or str(Path(exe).parent)
    path = shortcuts_path(root)
    if not path:
        raise RuntimeError("No localconfig.vdf found under userdata/ — log "
                           "into Steam at least once first")
    data = {"shortcuts": {}}
    if path.is_file():
        try:
            data = binvdf_parse(path.read_bytes())
        except Exception as e:
            raise RuntimeError(f"Couldn't parse existing shortcuts.vdf: {e}")
    shortcuts = data.setdefault("shortcuts", {})
    top32, signed = compute_shortcut_id(quoted_exe, name)
    # A crc32 collision is astronomically unlikely, but a duplicate key would
    # silently overwrite an existing shortcut on write - refuse instead.
    for entry in shortcuts.values():
        if isinstance(entry, dict) and entry.get("appid") == signed:
            raise RuntimeError("A shortcut with this exe + name already exists")
    next_idx = str(max((int(k) for k in shortcuts if k.isdigit()), default=-1) + 1)
    shortcuts[next_idx] = _new_shortcut_entry(name, quoted_exe, start_dir,
                                              launch_options, signed)
    bak = None
    if path.is_file():
        bak = path.with_suffix(f".vdf.pcc-{int(time.time())}.bak")
        shutil.copy2(path, bak)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".vdf.pcc-tmp")
    tmp.write_bytes(binvdf_dump(data))
    tmp.replace(path)
    return {"saved": True, "appid": str(top32), "backup": str(bak) if bak else None}


def remove_shortcut(root: Path, appid: str, close_steam=False) -> dict:
    if steam_running():
        if close_steam:
            shutdown_steam()
        else:
            raise RuntimeError("Steam is running. Close Steam first — it "
                               "overwrites shortcuts.vdf on exit.")
    path = shortcuts_path(root)
    if not path or not path.is_file():
        raise RuntimeError("No shortcuts.vdf found")
    data = binvdf_parse(path.read_bytes())
    shortcuts = data.get("shortcuts") or {}
    target = int(appid) & 0xFFFFFFFF
    key = next((k for k, e in shortcuts.items()
               if isinstance(e, dict) and (e.get("appid", 0) & 0xFFFFFFFF) == target), None)
    if key is None:
        raise RuntimeError("Shortcut not found")
    del shortcuts[key]
    bak = path.with_suffix(f".vdf.pcc-{int(time.time())}.bak")
    shutil.copy2(path, bak)
    tmp = path.with_suffix(".vdf.pcc-tmp")
    tmp.write_bytes(binvdf_dump(data))
    tmp.replace(path)
    return {"removed": True, "backup": str(bak)}


EXE_EXTENSIONS = {".exe", ".sh", ".appimage", ".bin", ".py"}


def quick_locations() -> list:
    """Shortcuts shown above the folder browser's listing: home, common game
    spots, and anything mounted under /run/media or /media (external drives -
    udisks2's default automount layout is <mountpoint>/<user>/<label>)."""
    home = Path.home()
    out = [{"label": "Home", "path": str(home)}]
    for name in ("Desktop", "Downloads", "Games"):
        p = home / name
        if p.is_dir():
            out.append({"label": name, "path": str(p)})
    root = steam_root()
    if root:
        common = root / "steamapps" / "common"
        if common.is_dir():
            out.append({"label": "Steam common", "path": str(common)})
    seen = set()
    for mountbase in (Path("/run/media"), Path("/media")):
        if not mountbase.is_dir():
            continue
        try:
            userdirs = list(mountbase.iterdir())
        except OSError:
            continue
        for userdir in userdirs:
            try:
                if not userdir.is_dir():
                    continue
                children = list(userdir.iterdir())
            except OSError:
                continue   # e.g. /run/media/root, unreadable by this user
            for d in sorted(children):
                try:
                    if d.is_dir() and d.resolve() not in seen:
                        seen.add(d.resolve())
                        out.append({"label": d.name, "path": str(d)})
                except OSError:
                    continue
    return out


def browse_dir(path_str: str) -> dict:
    """List a directory for the in-browser folder picker behind 'Add own
    game'. Browsers can't expose real filesystem paths through a native file
    input, so this (server-side, 127.0.0.1-only - no more exposed than a
    native file manager running as the same user) is how the frontend gets
    one to actually launch later."""
    base = Path(path_str).expanduser() if path_str else Path.home()
    try:
        base = base.resolve()
    except OSError:
        base = Path.home()
    if not base.is_dir():
        base = base.parent if base.parent.is_dir() else Path.home()
    entries = []
    try:
        listing = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        listing = []
    for p in listing:
        try:
            is_dir = p.is_dir()
            executable = (not is_dir) and (
                os.access(p, os.X_OK) or p.suffix.lower() in EXE_EXTENSIONS)
            if p.name.startswith(".") :
                continue   # dotfiles/dirs add noise; games don't live there
            entries.append({"name": p.name, "dir": is_dir, "executable": executable})
        except OSError:
            continue
    parent = base.parent
    try:
        quick = quick_locations()
    except OSError:
        quick = []
    return {
        "path": str(base),
        "parent": str(parent) if parent != base else None,
        "entries": entries,
        "quick": quick,
    }


# --------------------------------------------------------------------------
# Full library (owned games) via Steam Web API
# --------------------------------------------------------------------------
def steamid64(root: Path):
    """Most recent login from loginusers.vdf; falls back to config.vdf."""
    lu = root / "config/loginusers.vdf"
    try:
        data = vdf_parse(lu.read_text(errors="replace"))
        users = ci_get(data, "users") or {}
        best = None
        for sid, meta in users.items():
            if not sid.isdigit():
                continue
            if isinstance(meta, dict) and meta.get("MostRecent") == "1":
                return sid
            best = best or sid
        if best:
            return best
    except Exception:
        pass
    return None


def owned_games(root: Path, force=False):
    key = load_config().get("steam_api_key", "").strip()
    if not key:
        raise RuntimeError("No Steam Web API key set — add one in settings "
                           "(free at steamcommunity.com/dev/apikey)")
    sid = steamid64(root)
    if not sid:
        raise RuntimeError("Couldn't detect your SteamID64 from loginusers.vdf")
    state = load_state()
    cache = state.get("owned_cache", {})
    if not force and cache.get("sid") == sid             and time.time() - cache.get("ts", 0) < 3600:
        return cache["games"]
    url = ("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
           f"?key={urllib.parse.quote(key)}&steamid={sid}"
           "&include_appinfo=1&include_played_free_games=1")
    req = urllib.request.Request(url, headers={"User-Agent": "pcc"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    apps = (data.get("response") or {}).get("games") or []
    out = [{"appid": str(a["appid"]), "name": a.get("name") or str(a["appid"])}
           for a in apps]
    out.sort(key=lambda g: g["name"].lower())
    state["owned_cache"] = {"sid": sid, "ts": time.time(), "games": out}
    save_state(state)
    return out



def install_progress(root: Path):
    """Cheap poll: manifest-only install state for every game."""
    out = []
    for g in list_games(root):
        out.append({
            "appid": g["appid"],
            "name": g["name"],
            "installed": g["installed"],
            "fully_installed": g["fully_installed"],
            "download_pct": g["download_pct"],
            "size_bytes": g["size_bytes"],
        })
    return out


def install_game(appid: str):
    exe = shutil.which("steam")
    if not exe:
        raise RuntimeError("'steam' command not found in PATH")
    return _spawn_detached([exe, f"steam://install/{appid}"])


# --------------------------------------------------------------------------
# Hardware detection + MangoHud configuration
# --------------------------------------------------------------------------
MANGOHUD_DIR = Path(os.environ.get("XDG_CONFIG_HOME",
                                   str(Path.home() / ".config"))) / "MangoHud"

FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/JetBrainsMono-Regular.ttf",
    "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Regular.ttf",
    "/usr/share/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf",
    "/usr/share/fonts/TTF/FiraCode-Regular.ttf",
    "/usr/share/fonts/TTF/Hack-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


def find_font():
    for f in FONT_CANDIDATES:
        if Path(f).is_file():
            return f
    for root_dir in ("/usr/share/fonts",):
        base = Path(root_dir)
        if base.is_dir():
            for p in base.rglob("*Mono*.ttf"):
                return str(p)
    return None


def _short_gpu_name(name):
    """Turn a full GPU string into a compact label for the overlay.
    'NVIDIA GeForce RTX 5070 Laptop GPU' -> 'RTX 5070'
    'AMD Radeon 860M Graphics'          -> 'Radeon 860M'."""
    if not name:
        return "GPU"
    n = name.strip()
    # NVIDIA: 'RTX/GTX <number>' plus optional 'Ti' and/or 'Super'
    m = re.search(r"\b(RTX|GTX)\s*(\d{3,4})\s*(Ti)?\s*(Super)?", n, re.I)
    if m and m.group(2):
        parts = [m.group(1).upper(), m.group(2)]
        if m.group(3):
            parts.append("Ti")
        if m.group(4):
            parts.append("Super")
        return " ".join(parts)
    m = re.search(r"Radeon\s+([A-Z]*\s*\d{3,4}\s*[A-Z]{0,2})", n, re.I)
    if m:
        return f"Radeon {m.group(1).strip()}"
    m = re.search(r"\bArc\s+([A-Z]?\d{3,4})", n, re.I)
    if m:
        return f"Arc {m.group(1)}"
    for junk in ("NVIDIA", "GeForce", "AMD", "Radeon", "Intel", "Graphics",
                 "Laptop", "GPU", "(R)", "(TM)"):
        n = re.sub(rf"\b{re.escape(junk)}\b", "", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" -")
    return n[:18] or "GPU"


def _short_cpu_name(name):
    """Compact CPU label. 'AMD Ryzen AI 9 365 w/ Radeon...' -> 'Ryzen AI 9 365'.
    'AMD Ryzen 7 7800X3D 8-Core...'     -> 'Ryzen 7 7800X3D'.
    '13th Gen Intel Core i7-13700K'     -> 'Core i7-13700K'."""
    if not name:
        return "CPU"
    # strip trademark markers up front so they don't break matching
    n = re.sub(r"\((?:R|TM)\)", "", name, flags=re.I).strip()
    # AMD Ryzen (incl. 'Ryzen AI 9 365')
    m = re.search(r"Ryzen\s+(AI\s+)?(\d+)\s+([\w-]+)", n, re.I)
    if m:
        ai = "AI " if m.group(1) else ""
        return f"Ryzen {ai}{m.group(2)} {m.group(3)}"
    # Intel Core i3/i5/i7/i9 and Core Ultra
    m = re.search(r"Core\s+(i[3579])-?(\w+)?", n, re.I)
    if m:
        suffix = f"-{m.group(2)}" if m.group(2) else ""
        return f"Core {m.group(1)}{suffix}"
    m = re.search(r"Core\s+Ultra\s+(\d)\s*(\w+)?", n, re.I)
    if m:
        suffix = f" {m.group(2)}" if m.group(2) else ""
        return f"Core Ultra {m.group(1)}{suffix}"
    # fallback: strip vendor/marketing, cut at 'with'/'w/'
    n = re.split(r"\bw(?:ith|/)\b", n, flags=re.I)[0]
    for junk in ("AMD", "Intel", "Processor", "CPU", "Gen"):
        n = re.sub(rf"\b{re.escape(junk)}\b", "", n, flags=re.I)
    n = re.sub(r"\d+(?:th|st|nd|rd)?\s*-?\s*Core.*", "", n, flags=re.I)
    n = re.sub(r"\s+", " ", n).strip(" -")
    return n[:20] or "CPU"


def cpu_name():
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "Unknown CPU"


def _nvidia_gpus():
    out = []
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,pci.bus_id,memory.total,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=6)
        if r.returncode != 0:
            return out
        for line in r.stdout.strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 2:
                continue
            name, bus = parts[0], parts[1]
            # 00000000:01:00.0 -> 0000:01:00.0 (MangoHud pci_dev format)
            m = re.search(r"([0-9a-fA-F]{4}):([0-9a-fA-F]{2}):"
                          r"([0-9a-fA-F]{2})\.(\d)", bus)
            pci = f"{m.group(1)}:{m.group(2)}:{m.group(3)}.{m.group(4)}".lower() \
                if m else None
            out.append({"name": name, "vendor": "NVIDIA", "pci_dev": pci,
                        "vram_mb": int(parts[2].split()[0]) if len(parts) > 2
                                   and parts[2].split()[0].isdigit() else None,
                        "driver": parts[3] if len(parts) > 3 else None,
                        "discrete": True})
    except Exception:
        pass
    return out


def _drm_gpus():
    """AMD/Intel GPUs via sysfs, so iGPUs are named too."""
    out = []
    base = Path("/sys/class/drm")
    if not base.is_dir():
        return out
    seen = set()
    for card in sorted(base.glob("card[0-9]")):
        dev = card / "device"
        try:
            vendor = (dev / "vendor").read_text().strip()
            pci = os.path.basename(os.path.realpath(dev))
        except OSError:
            continue
        if pci in seen:
            continue
        seen.add(pci)
        vmap = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}
        vname = vmap.get(vendor.lower())
        if not vname or vname == "NVIDIA":     # NVIDIA handled by nvidia-smi
            continue
        label = None
        try:
            label = (dev / "product_name").read_text().strip()
        except OSError:
            pass
        out.append({"name": label or f"{vname} GPU ({pci})", "vendor": vname,
                    "pci_dev": pci, "vram_mb": None, "driver": None,
                    "discrete": False})
    return out


def detect_hardware() -> dict:
    gpus = _nvidia_gpus() + _drm_gpus()
    return {
        "cpu": cpu_name(),
        "cores": os.cpu_count(),
        "gpus": gpus,
        "hybrid": len(gpus) > 1,
        "font": find_font(),
        "mangohud": shutil.which("mangohud") is not None,
        "config_path": str(MANGOHUD_DIR / "MangoHud.conf"),
        "config_exists": (MANGOHUD_DIR / "MangoHud.conf").is_file(),
    }


def primary_gpu_vendor() -> str:
    """Pick the vendor that should drive theming/toggle gating: NVIDIA wins
    whenever a discrete NVIDIA GPU is present (the common hybrid-laptop
    case - an NVIDIA dGPU next to an AMD/Intel iGPU - since DLSS is what
    matters there), otherwise AMD if any AMD GPU is present. Returns
    "NVIDIA", "AMD", or "unknown" (neither vendor detected)."""
    vendors = {g.get("vendor") for g in _nvidia_gpus() + _drm_gpus()}
    if "NVIDIA" in vendors:
        return "NVIDIA"
    if "AMD" in vendors:
        return "AMD"
    return "unknown"


# MangoHud 0.8.2 with legacy_layout=false draws a column per listed param.
# Horizontal single line, each stat block colour-coded (blue CPU, NVIDIA-green
# GPU, violet VRAM, amber RAM) instead of one flat orange for everything, on a
# slightly blue-tinted charcoal background matching PCC's own panel colour
# (--panel2 #1e252e) rather than plain black/grey. round_corners matches
# PCC's own --radius so the overlay reads as part of the same UI.
MANGOHUD_STYLE = {
    "horizontal": True,             # single-line layout like the reference
    "legacy_layout": False,
    "table_columns": 14,
    "background_alpha": 0.6,
    "round_corners": 10,
    "font_size": 22,
    "font_size_text": 22,
    "cellpadding_y": -0.03,
    # colours (hex, no #): one accent per stat category, echoing PCC's own
    # brand palette (--launch/--dlss/--compiled/--cache) instead of a single
    # orange for everything and grey for the rest.
    "gpu_color": "76B900",          # NVIDIA green - matches --dlss
    "cpu_color": "4DA3FF",          # PCC blue - matches --launch
    "vram_color": "B07AFF",         # PCC violet - matches --compiled
    "ram_color": "E8A33D",          # PCC amber - matches --cache
    "engine_color": "FF5D5D",       # PCC red - matches --danger
    "io_color": "A491D3",
    "frametime_color": "FFFFFF",
    "background_color": "12181F",
    "text_color": "FFFFFF",
    "media_player_color": "FFFFFF",
    "network_color": "FFFFFF",
    "separator_color": "2A333E",    # matches PCC's own --line
    "battery_color": "FFFFFF",
    "wine_color": "FF5D5D",
}

MANGOHUD_PRESETS = {
    # Order: CPU block, then GPU block, then FPS + frame-time graph.
    # gpu_name/cpu_name are intentionally omitted so the overlay doesn't print
    # the long device-name prefix (e.g. "NVIDIA GeForce RTX 5070 Laptop").
    "reference": ["cpu_name", "cpu_stats", "cpu_load_change", "cpu_temp",
                  "cpu_power", "gpu_name", "gpu_stats", "gpu_load_change",
                  "gpu_temp", "gpu_power", "vram", "fps", "frame_timing=1"],
    "minimal": ["fps", "frame_timing=1", "cpu_stats", "gpu_stats"],
    "standard": ["fps", "fps_color_change", "frame_timing=1", "cpu_name",
                 "cpu_stats", "cpu_temp", "cpu_load_change", "cpu_power",
                 "ram", "gpu_name", "gpu_stats", "gpu_temp", "gpu_load_change",
                 "gpu_power", "vram"],
    "benchmark": ["fps", "fps_color_change", "frame_timing=1", "histogram",
                  "cpu_stats", "cpu_temp", "cpu_power", "cpu_load_change",
                  "ram", "swap", "gpu_stats", "gpu_temp", "gpu_power",
                  "gpu_load_change", "vram", "io_read", "io_write",
                  "vulkan_driver", "engine_version", "resolution",
                  "benchmark_percentiles=AVG,1,0.1"],
    "stutter": ["fps", "frame_timing=1", "histogram", "frametime",
                "cpu_stats", "cpu_load_change", "gpu_stats",
                "gpu_load_change", "throttling_status", "present_mode"],
}


def mangohud_config(preset="reference", hw=None, pin_gpu=None,
                    toggle_key="Shift_R+F12"):
    hw = hw or detect_hardware()
    lines = [
        "### Generated by Proton Command Center",
        f"### CPU: {hw['cpu']}",
    ]
    for g in hw["gpus"]:
        vram = f", {g['vram_mb']} MB" if g.get("vram_mb") else ""
        lines.append(f"### GPU: {g['name']} ({g['vendor']}{vram})")
    lines.append("")

    # layout + style block (order-independent, so grouped for readability)
    if MANGOHUD_STYLE.get("horizontal"):
        lines.append("horizontal")
    lines.append("legacy_layout=false")
    for k, v in MANGOHUD_STYLE.items():
        if k in ("horizontal", "legacy_layout"):
            continue
        if isinstance(v, bool):
            if v:
                lines.append(k)
        else:
            lines.append(f"{k}={v}")
    if hw.get("font"):
        lines.append(f"font_file={hw['font']}")
    lines.append("text_outline")

    # Short custom labels so the overlay shows "Ryzen AI 9 365" / "RTX 5070"
    # instead of the full auto-detected marketing string.
    cpu_lbl = _short_cpu_name(hw.get("cpu"))
    if cpu_lbl:
        lines.append(f"cpu_text={cpu_lbl}")
    disc = next((g for g in hw["gpus"] if g.get("discrete")), None) \
        or (hw["gpus"][0] if hw["gpus"] else None)
    if disc:
        lines.append(f"gpu_text={_short_gpu_name(disc.get('name'))}")

    if hw["hybrid"]:
        target = pin_gpu or next((g["pci_dev"] for g in hw["gpus"]
                                  if g["discrete"] and g["pci_dev"]), None)
        if target:
            lines += ["", "### hybrid GPU: pin stats to the discrete card",
                      f"pci_dev={target}"]

    lines += [""] + MANGOHUD_PRESETS.get(preset, MANGOHUD_PRESETS["reference"])
    lines += ["", f"toggle_hud={toggle_key}", "toggle_logging=Shift_L+F2"]
    return "\n".join(lines) + "\n"


def apply_mangohud_config(preset="reference", pin_gpu=None) -> dict:
    MANGOHUD_DIR.mkdir(parents=True, exist_ok=True)
    dest = MANGOHUD_DIR / "MangoHud.conf"
    backup = None
    if dest.is_file():
        backup = dest.with_suffix(f".conf.pcc-{int(time.time())}.bak")
        shutil.copy2(dest, backup)
    text = mangohud_config(preset, pin_gpu=pin_gpu)
    tmp = dest.with_suffix(".conf.pcc-tmp")
    tmp.write_text(text)
    tmp.replace(dest)
    return {"written": str(dest), "backup": str(backup) if backup else None,
            "preset": preset}




# --------------------------------------------------------------------------
# Backup / restore - export and re-import PCC's own data (survives reinstalls)
# --------------------------------------------------------------------------
def export_backup(dest_dir=None) -> dict:
    """Bundle PCC's data (DLL library, backups, state, config/API keys,
    MangoHud config) into a single .tar.gz the user can keep and re-import
    after an OS reinstall. Returns the archive path."""
    import tarfile
    dest_dir = Path(dest_dir) if dest_dir else (Path.home() / "Downloads"
               if (Path.home() / "Downloads").is_dir() else Path.home())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = dest_dir / f"pcc-backup-{stamp}.tar.gz"
    mango = Path.home() / ".config/MangoHud/MangoHud.conf"
    with tarfile.open(archive, "w:gz") as tar:
        # everything under DATA_DIR except the transient art cache
        for item in DATA_DIR.iterdir():
            if item.name in ("artcache", "art_cache"):
                continue
            tar.add(item, arcname=f"pcc-data/{item.name}")
        if mango.is_file():
            tar.add(mango, arcname="mangohud/MangoHud.conf")
    return {"archive": str(archive), "size": archive.stat().st_size}


def restore_backup(archive_path) -> dict:
    """Restore a PCC backup archive produced by export_backup. Existing data is
    overwritten by the archive's contents; anything not in the archive is left
    alone. MangoHud config is restored to its standard location."""
    import tarfile
    src = Path(archive_path).expanduser()
    if not src.is_file():
        raise RuntimeError(f"Backup not found: {src}")
    restored = {"data": 0, "mangohud": False}
    with tarfile.open(src, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("pcc-data/"):
                rel = name[len("pcc-data/"):]
                if not rel or ".." in rel:
                    continue
                target = DATA_DIR / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                fobj = tar.extractfile(member)
                if fobj:
                    target.write_bytes(fobj.read())
                    restored["data"] += 1
                elif member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
            elif name == "mangohud/MangoHud.conf":
                mdir = Path.home() / ".config/MangoHud"
                mdir.mkdir(parents=True, exist_ok=True)
                fobj = tar.extractfile(member)
                if fobj:
                    (mdir / "MangoHud.conf").write_bytes(fobj.read())
                    restored["mangohud"] = True
    return {"restored": True, **restored}



# --------------------------------------------------------------------------
# Proton compatibility tool management (GE-Proton install / update awareness)
# --------------------------------------------------------------------------
GE_PROTON_RELEASES = "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases"
COMPAT_INSTALL_DIR = Path.home() / ".local/share/Steam/compatibilitytools.d"


def _installed_ge_versions():
    """Version dirs already present in the user compatibilitytools.d."""
    out = set()
    if COMPAT_INSTALL_DIR.is_dir():
        for d in COMPAT_INSTALL_DIR.iterdir():
            if d.is_dir():
                out.add(d.name)
    return out


def list_ge_proton(limit=10) -> dict:
    """List recent GE-Proton releases from GitHub with an 'installed' flag.
    Cached 6h. This is the 'what's available / am I up to date' view."""
    state = load_state()
    cache = state.get("ge_releases")
    now = time.time()
    if cache and now - cache.get("ts", 0) < 21600:
        rels = cache["data"]
    else:
        try:
            data = _gh_json(GE_PROTON_RELEASES)
        except Exception as e:
            return {"error": str(e), "releases": []}
        rels = []
        for r in data[:limit]:
            # GE-Proton 11+ ships both x86_64 and aarch64 (ARM) tarballs.
            # The x86_64 asset is named like "GE-Proton11-1.tar.gz" (no arch
            # suffix); ARM is "GE-Proton11-1-aarch64.tar.gz". Pick x86_64 and
            # never the ARM build (which breaks on x64 - see GE issue #569).
            def _is_x86(a):
                n = a.get("name", "")
                return (n.endswith(".tar.gz")
                        and "aarch64" not in n
                        and "arm64" not in n
                        and not n.endswith(".sha512sum"))
            asset = next((a for a in r.get("assets", []) if _is_x86(a)), None)
            if not asset:
                continue
            rels.append({"tag": r["tag_name"],
                         "name": r["name"] or r["tag_name"],
                         "url": asset["browser_download_url"],
                         "size": asset.get("size", 0),
                         "published": r.get("published_at", "")[:10]})
        state["ge_releases"] = {"ts": now, "data": rels}
        save_state(state)
    installed = _installed_ge_versions()
    for r in rels:
        # GE tarballs extract to a dir named after the tag (e.g. GE-Proton9-27)
        r["installed"] = any(r["tag"] in name or name in r["tag"]
                             for name in installed)
    newest = rels[0]["tag"] if rels else None
    up_to_date = bool(newest and any(r["installed"] and r["tag"] == newest
                                     for r in rels))
    return {"releases": rels, "newest": newest, "up_to_date": up_to_date,
            "installed": sorted(installed)}


def install_ge_proton(task_id, url, tag) -> None:
    """Download a GE-Proton tarball and extract it into the user
    compatibilitytools.d. Steam picks it up on next launch."""
    import tarfile, io
    TASKS[task_id] = {"status": "running", "progress": 10,
                      "detail": f"Downloading {tag}"}
    try:
        data = _gh_bytes(url, task_id)
        TASKS[task_id] = {"status": "running", "progress": 80,
                          "detail": f"Extracting {tag}"}
        COMPAT_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            # safety: refuse absolute paths or traversal in members
            for m in tar.getmembers():
                if m.name.startswith("/") or ".." in m.name.split("/"):
                    raise RuntimeError(f"unsafe path in archive: {m.name}")
            tar.extractall(COMPAT_INSTALL_DIR)
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Installed {tag} — restart Steam to use it"}
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0,
                          "detail": f"{tag}: {e}"}


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        root = steam_root()
        try:
            if self.path in ("/", "/index.html"):
                html = (APP_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, must-revalidate")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            elif self.path == "/api/status":
                self._json({
                    "steam_root": str(root) if root else None,
                    "steam_running": steam_running(),
                    "driver": driver_version(),
                    "gpu_vendor": primary_gpu_vendor(),
                    "version": VERSION,
                    "started_at": STARTED_AT,
                })
            elif self.path == "/api/games":
                if not root:
                    self._json({"error": "Steam not found"}, 500); return
                games = all_games(root)
                state = load_state()
                drv = driver_version()
                # launch options: one parse of the newest localconfig
                lo_appids = set()
                cfgs = find_localconfigs(root)
                if cfgs:
                    try:
                        data = vdf_parse(cfgs[0].read_text(errors="replace"))
                        apps = _apps_node(ci_get(data, "UserLocalConfigStore")) or {}
                        for aid, entry in apps.items():
                            if isinstance(entry, dict) and ci_get(entry, "LaunchOptions"):
                                lo_appids.add(aid)
                    except Exception:
                        pass
                dlss_seen = state.get("dlss_seen", {})
                ultraplus_seen = state.get("ultraplus_seen", {})
                try:
                    ultraplus_cat = ultraplus_catalog()
                except Exception:
                    ultraplus_cat = {"games": {}}  # offline/CDN hiccup - just skip the tag
                # RHI (ReShade/OptiScaler/DXVK/RE Framework) install records
                # are already tracked per-appid on install/remove, so "has
                # RHI" needs no live scan or separate "seen" cache like
                # Ultra+'s - just membership in any of these state dicts.
                rhi_installed_appids = (set(state.get("rhi_reshade_installs", {}))
                                        | set(state.get("rhi_optiscaler_installs", {}))
                                        | set(state.get("rhi_dxvk_installs", {}))
                                        | set(state.get("rhi_reframework_installs", {})))
                rhi_supported_seen = state.get("rhi_supported_seen", {})
                for g in games:
                    # Shortcuts carry their own LaunchOptions field (surfaced by
                    # list_shortcuts as launch_options_shortcut) rather than living
                    # in localconfig.vdf's apps node like real Steam games do.
                    g["has_launch_options"] = (bool(g.get("launch_options_shortcut"))
                                               if g.get("custom")
                                               else g["appid"] in lo_appids)
                    g["has_dlss"] = bool(dlss_seen.get(g["appid"]))
                    g["has_ultraplus"] = bool(ultraplus_seen.get(g["appid"]))
                    match = match_ultraplus_catalog(g["name"], ultraplus_cat)
                    g["ultraplus_supported"] = match is not None
                    g["ultraplus_url"] = match[1]["url"] if match else ""
                    g["has_rhi"] = g["appid"] in rhi_installed_appids
                    g["rhi_supported"] = bool(rhi_supported_seen.get(g["appid"]))
                self._json({"games": games})
            elif m := re.match(r"^/api/game/(\d+)/launch_options$", self.path):
                self._json(get_launch_options(root, m.group(1)))
            elif m := re.match(r"^/api/game/(\d+)/dlss$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                other_roots = [g2["install_path"] for aid2, g2 in games.items()
                               if aid2 != m.group(1) and g2.get("install_path")]
                dlls = scan_game_dlss(g["install_path"], other_roots=other_roots) if g else []
                state = load_state()
                state.setdefault("dlss_seen", {})[m.group(1)] = bool(dlls)
                save_state(state)
                self._json({"dlls": dlls})
            elif m := re.match(r"^/api/game/(\d+)/ultraplus$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                result = scan_ultraplus(g["install_path"]) if g else {"installed": False}
                if g:
                    state = load_state()
                    state.setdefault("ultraplus_seen", {})[m.group(1)] = bool(result.get("installed"))
                    save_state(state)
                self._json(result)
            elif m := re.match(r"^/api/game/(\d+)/mods$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                catalog = ultraplus_catalog()
                matched = match_ultraplus_catalog(g["name"], catalog)
                if not matched:
                    self._json({"supported": False}); return
                game_key, info = matched
                rec = load_state().get("mod_installs", {}).get(m.group(1))
                config_dir = mod_config_dir(game_key, g["install_path"], catalog) if rec else None
                self._json({
                    "supported": True, "game_key": game_key, "full_name": info["full_name"],
                    "url": info["url"], "installed": rec, "versions": list_mod_versions(game_key),
                    "presets": list_presets(config_dir) if config_dir else [],
                })
            elif m := re.match(r"^/api/game/(\d+)/mods/settings$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                catalog = ultraplus_catalog()
                matched = match_ultraplus_catalog(g["name"], catalog)
                if not matched:
                    self._json({"settings": [], "categories": []}); return
                game_key, _ = matched
                config_dir = mod_config_dir(game_key, g["install_path"], catalog)
                if not config_dir or not (config_dir / "UltraPlusConfig.ini").is_file():
                    self._json({"settings": [], "categories": []}); return
                overrides = ultraplus_overrides()
                self._json({
                    "settings": list_mod_settings(config_dir, overrides),
                    "categories": list(overrides.get("categories", {}).keys()) + ["Other"],
                })
            elif m := re.match(r"^/api/game/(\d+)/mods/addons$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                catalog = ultraplus_catalog()
                matched = match_ultraplus_catalog(g["name"], catalog)
                self._json({"addons": list_addons(matched[0], catalog, m.group(1)) if matched else []})
            elif m := re.match(r"^/api/game/(\d+)/rhi/reshade$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                result = scan_game_reshade(m.group(1), g["install_path"])
                # A detectable graphics API means ReShade/OptiScaler/DXVK
                # could work here - same "supported" signal all three RHI
                # subsystems key off of. Detecting this for real needs a
                # directory walk + PE scan (_find_game_exe), too slow to do
                # for every game on every /api/games poll, so it's recorded
                # here as a side effect of the per-game scan already run
                # when the user opens this game's RHI tab - same lazily-
                # populated "_seen" cache shape as dlss_seen/ultraplus_seen.
                state = load_state()
                state.setdefault("rhi_supported_seen", {})[m.group(1)] = bool(result.get("detected_api"))
                save_state(state)
                self._json(result)
            elif m := re.match(r"^/api/game/(\d+)/rhi/builds$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                self._json(detect_game_builds(g["install_path"]))
            elif m := re.match(r"^/api/game/(\d+)/rhi/refwork$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                self._json(scan_re_framework(m.group(1), g["install_path"]))
            elif self.path == "/api/rhi/shader_packs":
                self._json({"packs": get_shader_pack_catalog()})
            elif m := re.match(r"^/api/game/(\d+)/rhi/shaders$", self.path):
                self._json({"selection": get_game_shader_selection(m.group(1))})
            elif self.path == "/api/rhi/addons":
                self._json({"addons": reshade_addons_catalog()})
            elif m := re.match(r"^/api/game/(\d+)/rhi/addons$", self.path):
                self._json({"selection": get_game_addon_selection(m.group(1))})
            elif m := re.match(r"^/api/game/(\d+)/rhi/optiscaler$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                self._json(scan_game_optiscaler(m.group(1), g["install_path"]))
            elif m := re.match(r"^/api/game/(\d+)/rhi/dxvk$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                self._json(scan_game_dxvk(m.group(1), g["install_path"], game_name=g["name"]))
            elif self.path == "/api/progress":
                self._json({"games": install_progress(root)})
            elif self.path == "/api/owned_games":
                self._json({"games": owned_games(root)})
            elif self.path == "/api/hardware":
                self._json(detect_hardware())
            elif self.path == "/api/backup/export":
                self._json(export_backup())
            elif self.path == "/api/proton/list":
                self._json(list_ge_proton())
            elif m := re.match(r"^/api/mangohud(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(1) or "")
                preset = (qs.get("preset") or ["standard"])[0]
                hw = detect_hardware()
                self._json({"hardware": hw, "preset": preset,
                            "preview": mangohud_config(preset, hw)})
            elif self.path == "/api/proton_capabilities":
                self._json(proton_capabilities(root))
            elif self.path == "/api/compat_tools":
                self._json({"tools": list_compat_tools(root)})
            elif m := re.match(r"^/api/browse(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(1) or "")
                self._json(browse_dir((qs.get("path") or [""])[0]))
            elif m := re.match(r"^/api/game/(\d+)/compat_tool$", self.path):
                self._json(get_compat_tool(root, m.group(1)))
            elif m := re.match(r"^/api/game/(\d+)/protondb(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(2) or "")
                if qs.get("cached"):
                    self._json(protondb_cached(m.group(1)) or {"tier": None,
                                                               "cached": True})
                else:
                    self._json(protondb_summary(m.group(1)) or {"tier": None})
            elif m := re.match(r"^/api/owned(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(1) or "")
                force = (qs.get("refresh") or ["0"])[0] == "1"
                self._json(fetch_owned_games(root, force=force))
            elif self.path == "/api/dlss/library":
                self._json({"dlls": dll_library()})
            elif self.path == "/api/streamline/latest":
                self._json({"latest": streamline_sdk_latest(),
                            "cached": streamline_sdk_library()})
            elif m := re.match(r"^/api/game/(\d+)/rhi/streamline$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                self._json(scan_streamline_for_game(g["install_path"]))
            elif m := re.match(r"^/api/art_debug/(\d+)(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(2) or "")
                gname = (qs.get("name") or [None])[0]
                ART_MISSES.pop(m.group(1), None)      # force a real attempt
                tr = []
                try:
                    res = sgdb_art(m.group(1), name=gname, trace=tr)
                    self._json({"resolved": bool(res), "trace": tr})
                except Exception as e:
                    self._json({"resolved": False, "trace": tr + [str(e)]})
            elif m := re.match(r"^/api/art/(\d+)(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(2) or "")
                gname = (qs.get("name") or [None])[0]
                try:
                    res = sgdb_art(m.group(1), name=gname)
                except Exception:
                    res = None
                if not res:
                    self._json({"error": "no art"}, 404); return
                img, ct = res
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(img)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(img)
            elif self.path == "/api/settings":
                key = load_config().get("sgdb_api_key", "")
                skey = load_config().get("steam_api_key", "")
                self._json({"sgdb_api_key_set": bool(key.strip()),
                            "sgdb_api_key_hint": (key[:4] + "…") if key else "",
                            "steam_api_key_set": bool(skey.strip())})
            elif m := re.match(r"^/api/tasks/([\w-]+)$", self.path):
                self._json(TASKS.get(m.group(1), {"status": "unknown"}))
            elif self.path == "/api/dlss_preset_defaults":
                self._json(get_dlss_preset_defaults())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        root = steam_root()
        try:
            body = self._body()
            if m := re.match(r"^/api/game/(\d+)/save$", self.path):
                self._json(set_game_config(
                    root, m.group(1),
                    launch_value=body.get("launch_options"),
                    compat_tool=body.get("compat_tool"),
                    close_steam=bool(body.get("close_steam"))))
            elif m := re.match(r"^/api/game/(\d+)/launch_options$", self.path):
                self._json(set_launch_options(root, m.group(1), body.get("value", ""),
                                              close_steam=bool(body.get("close_steam"))))
            elif self.path == "/api/dlss_preset_defaults":
                self._json(set_dlss_preset_defaults(body.get("settings", {})))
            elif self.path == "/api/backup/restore":
                self._json(restore_backup(body["archive"]))
            elif self.path == "/api/mods/install":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                catalog = ultraplus_catalog()
                matched = match_ultraplus_catalog(g["name"], catalog)
                if not matched:
                    self._json({"error": "no Ultra+ mod catalog entry for this game"}, 400); return
                game_key, _ = matched
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_install_mod_task,
                                 args=(tid, appid, g["install_path"], game_key, catalog,
                                       body["download_url"], body["filename"],
                                       bool(body.get("skip_ue4ss"))),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/mods/remove":
                self._json(remove_mod(body["appid"]))
            elif self.path == "/api/mods/apply_preset":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                catalog = ultraplus_catalog()
                matched = match_ultraplus_catalog(g["name"], catalog)
                if not matched:
                    self._json({"error": "no Ultra+ mod catalog entry for this game"}, 400); return
                game_key, _ = matched
                config_dir = mod_config_dir(game_key, g["install_path"], catalog)
                if not config_dir:
                    self._json({"error": "could not resolve the game's config directory"}, 400); return
                self._json(apply_preset(config_dir, body["preset"]))
            elif self.path == "/api/mods/settings/set":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                catalog = ultraplus_catalog()
                matched = match_ultraplus_catalog(g["name"], catalog)
                if not matched:
                    self._json({"error": "no Ultra+ mod catalog entry for this game"}, 400); return
                game_key, _ = matched
                config_dir = mod_config_dir(game_key, g["install_path"], catalog)
                if not config_dir:
                    self._json({"error": "could not resolve the game's config directory"}, 400); return
                self._json(set_mod_setting(config_dir, body["key"], body["value"]))
            elif self.path == "/api/mods/settings/restore_defaults":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                catalog = ultraplus_catalog()
                matched = match_ultraplus_catalog(g["name"], catalog)
                if not matched:
                    self._json({"error": "no Ultra+ mod catalog entry for this game"}, 400); return
                game_key, _ = matched
                config_dir = mod_config_dir(game_key, g["install_path"], catalog)
                if not config_dir:
                    self._json({"error": "could not resolve the game's config directory"}, 400); return
                self._json(restore_mod_defaults(config_dir))
            elif self.path == "/api/mods/addons/install":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                catalog = ultraplus_catalog()
                matched = match_ultraplus_catalog(g["name"], catalog)
                if not matched:
                    self._json({"error": "no Ultra+ mod catalog entry for this game"}, 400); return
                game_key, _ = matched
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_install_addon_task,
                                 args=(tid, appid, g["install_path"], game_key, catalog,
                                       body["file_name"], body["download_url"], body["filename"]),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/mods/addons/remove":
                self._json(remove_addon(body["appid"], body["file_name"]))
            elif self.path == "/api/rhi/reshade/install":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_install_reshade_task,
                                 args=(tid, appid, g["install_path"], body.get("exe"),
                                       body.get("channel", "stable"),
                                       body.get("legacy_version"), body.get("custom_filename")),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/rhi/reshade/remove":
                self._json(remove_reshade(body["appid"]))
            elif self.path == "/api/rhi/refwork/install":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_install_re_framework_task,
                                 args=(tid, appid, g["install_path"]),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/rhi/refwork/remove":
                self._json(remove_re_framework(body["appid"]))
            elif self.path == "/api/rhi/reshade/check_custom_updates":
                self._json(check_custom_reshade_updates())
            elif self.path == "/api/rhi/shaders/deploy":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                pack_ids = body.get("pack_ids") or []
                set_game_shader_selection(appid, pack_ids)
                target_dir = str(resolve_rhi_target_dir(appid, g["install_path"]))
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_deploy_shader_packs_task,
                                 args=(tid, target_dir, pack_ids),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/rhi/shader_packs/update":
                pack_id = body["pack_id"]
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_update_shader_pack_task,
                                 args=(tid, pack_id), daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/rhi/shaders/apply_preset":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                preset_text = body.get("preset_text", "")
                target_dir = str(resolve_rhi_target_dir(appid, g["install_path"]))
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Reading preset"}
                threading.Thread(target=_apply_preset_shader_packs_task,
                                 args=(tid, appid, target_dir, preset_text),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/rhi/shaders/remove":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                set_game_shader_selection(appid, [])
                target_dir = str(resolve_rhi_target_dir(appid, g["install_path"]))
                self._json(remove_reshade_shaders(target_dir))
            elif self.path == "/api/rhi/addons/deploy":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                addon_ids = body.get("addon_ids") or []
                rec = load_state().get("rhi_reshade_installs", {}).get(appid)
                if not rec:
                    self._json({"error": "Install ReShade first - addons only "
                                        "make sense once it's there"}, 400); return
                bitness = rec.get("bitness") or 64
                addon_dir = str(Path(rec["path"]).parent)   # next to dxgi.dll, not the Steam install root
                set_game_addon_selection(appid, addon_ids)
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_deploy_reshade_addons_task,
                                 args=(tid, addon_dir, addon_ids, bitness),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/rhi/addons/remove":
                appid = body["appid"]
                rec = load_state().get("rhi_reshade_installs", {}).get(appid)
                set_game_addon_selection(appid, [])
                self._json(remove_reshade_addons(str(Path(rec["path"]).parent)) if rec
                          else {"removed": 0})
            elif self.path == "/api/rhi/optiscaler/install":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_install_optiscaler_task,
                                 args=(tid, appid, g["install_path"], body.get("exe"),
                                       body.get("gpu_type"), body.get("dlss_inputs", True),
                                       body.get("variant", "stable"), body.get("hotkey"),
                                       g["name"]),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/rhi/optiscaler/remove":
                self._json(remove_optiscaler(body["appid"]))
            elif self.path == "/api/rhi/optiscaler/update":
                appid = body["appid"]
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_update_optiscaler_task,
                                 args=(tid, appid), daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/rhi/optiscaler/hotkey":
                hotkey = body.get("hotkey", "")
                set_optiscaler_hotkey(hotkey)
                updated = apply_optiscaler_hotkey_to_all_games(hotkey) if body.get("apply_all") else 0
                self._json({"applied": True, "updated_games": updated})
            elif self.path == "/api/rhi/optiscaler/fg":
                self._json(set_optiscaler_fg(body["appid"], body.get("fg_input", "auto"),
                                             body.get("fg_output", "auto"),
                                             body.get("fg_nvngx_replacement")))
            elif self.path == "/api/rhi/dxvk/install":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_install_dxvk_task,
                                 args=(tid, appid, g["install_path"], body.get("variant", "stable"),
                                       body.get("exe"), body.get("lilium_preset", 0), g["name"]),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/rhi/dxvk/remove":
                self._json(remove_dxvk(body["appid"]))
            elif self.path == "/api/rhi/dxvk/reset_conf":
                self._json(reset_dxvk_conf(body["appid"]))
            elif self.path == "/api/rhi/dxvk/update":
                appid = body["appid"]
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_update_dxvk_task,
                                 args=(tid, appid), daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/proton/install":
                tid = str(uuid.uuid4())
                threading.Thread(target=install_ge_proton,
                                 args=(tid, body["url"], body["tag"]),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/settings":
                cfg = load_config()
                if "sgdb_api_key" in body:
                    cfg["sgdb_api_key"] = str(body["sgdb_api_key"]).strip()
                if "steam_api_key" in body:
                    cfg["steam_api_key"] = str(body["steam_api_key"]).strip()
                save_config(cfg)
                self._json({"saved": True})
            elif m := re.match(r"^/api/game/(\d+)/install$", self.path):
                self._json({"installing": install_game(m.group(1))})
            elif m := re.match(r"^/api/game/(\d+)/install$", self.path):
                self._json({"installing": install_game(m.group(1))})
            elif m := re.match(r"^/api/game/(\d+)/launch$", self.path):
                self._json({"launched": launch_game(m.group(1))})
            elif m := re.match(r"^/api/game/(\d+)/compat_tool$", self.path):
                self._json(set_compat_tool(root, m.group(1), body.get("name", ""),
                                           close_steam=bool(body.get("close_steam"))))
            elif self.path == "/api/shortcuts":
                r = add_shortcut(root, body.get("name", ""), body.get("exe", ""),
                                 start_dir=body.get("start_dir", ""),
                                 launch_options=body.get("launch_options", ""),
                                 close_steam=bool(body.get("close_steam")))
                # Steam's already closed by add_shortcut above if it was
                # running, so this second write needs no close_steam of its own.
                if body.get("compat_tool"):
                    set_compat_tool(root, r["appid"], body["compat_tool"], close_steam=False)
                self._json(r)
            elif self.path == "/api/shortcuts/remove":
                self._json(remove_shortcut(root, body["appid"],
                                           close_steam=bool(body.get("close_steam"))))
            elif self.path == "/api/art/reset":
                n = 0
                for f in ART_DIR.iterdir():
                    f.unlink(missing_ok=True)
                    n += 1
                ART_MISSES.clear()
                self._json({"cleared": n})
            elif self.path == "/api/mangohud/apply":
                self._json(apply_mangohud_config(
                    body.get("preset", "standard"),
                    pin_gpu=body.get("pin_gpu")))
            elif self.path == "/api/steam/launch":
                self._json({"launched": launch_steam()})
            elif self.path == "/api/dlss/swap":
                self._json(swap_dll(body["game_dll"], body["library_dll"]))
            elif self.path == "/api/dlss/restore":
                self._json(restore_dll(body["game_dll"]))
            elif self.path == "/api/dlss/import":
                self._json(import_dll(body["path"]))
            elif m := re.match(r"^/api/game/(\d+)/dlss/deploy_nr$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                if not g:
                    self._json({"error": "game not found"}, 404); return
                other_roots = [g2["install_path"] for aid2, g2 in games.items()
                               if aid2 != m.group(1) and g2.get("install_path")]
                self._json(deploy_dlssnr_to_game(g["install_path"], other_roots=other_roots))
            elif self.path == "/api/dlss/download":
                kind = body.get("kind", "sr")
                if kind not in DLL_SOURCES:
                    self._json({"error": f"unknown kind {kind}"}, 400); return
                tid = str(uuid.uuid4())
                threading.Thread(target=download_dlss, args=(tid, kind),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/dlss/download_sr":
                tid = str(uuid.uuid4())
                threading.Thread(target=download_latest_sr, args=(tid,), daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/streamline/download":
                tid = str(uuid.uuid4())
                threading.Thread(target=download_streamline_sdk, args=(tid,),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/rhi/streamline/deploy":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                self._json(deploy_streamline_to_game(g["install_path"], body.get("version")))
            elif self.path == "/api/rhi/streamline/remove":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                self._json(remove_streamline_from_game(g["install_path"]))
            else:
                self._json({"error": "not found"}, 404)
        except RuntimeError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def main() -> None:
    root = steam_root()
    print(f"Proton Command Center  ->  http://localhost:{PORT}")
    print(f"Steam root: {root or 'NOT FOUND'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
