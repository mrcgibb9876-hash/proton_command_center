#!/usr/bin/env python3
"""
Proton Command Center (PCC)
Per-game launch options, DLSS DLL management, and shader cache control
for Steam on Linux. Stdlib only. Run: python3 pcc.py  ->  http://localhost:8686
"""

import hashlib
import json
import os
import tempfile
import re
import shutil
import struct
import subprocess
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

VERSION = "1.19.0"
PORT = int(os.environ.get("PCC_PORT", "8686"))
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".local/share/proton-command-center"
DLL_LIBRARY = DATA_DIR / "dlls"        # dlls/<kind>/<version>/<name>.dll
BACKUP_DIR = DATA_DIR / "backups"      # backups/<appid>/<relpath>.pccbak
RESHADE_DIR = DATA_DIR / "reshade"     # reshade/<version>/ReShade{32,64}.dll
RESHADE_SHADERS_DIR = RESHADE_DIR / "shaders"  # shared Shaders/ + Textures/
DATA_DIR.mkdir(parents=True, exist_ok=True)
DLL_LIBRARY.mkdir(parents=True, exist_ok=True)
_DEDUPE_ON_IMPORT = True  # dedupe runs lazily via dll_library()
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
RESHADE_DIR.mkdir(parents=True, exist_ok=True)

DLSS_KINDS = {
    "nvngx_dlss.dll":  {"kind": "sr",  "label": "DLSS Super Resolution"},
    "nvngx_dlssg.dll": {"kind": "fg",  "label": "DLSS Frame Generation"},
    "nvngx_dlssd.dll": {"kind": "rr",  "label": "DLSS Ray Reconstruction"},
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
        return {"compiled": {}}


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


# Fossilize file taxonomy inside steamapps/shadercache/<appid>/fozpipelinesv6/
#   steam_pipeline_cache.foz                        -> input (downloaded/captured)
#   steamapprun_pipeline_cache.<hash>.<n>.foz       -> input (runtime capture)
#   steamapp_pipeline_cache.foz                     -> input
#   steam_pipeline_cache_whitelist.foz              -> NOT input
#   replay_cache.<hash>.foz                         -> the replayer ledger (output)
# Pipelines live BOTH at the top level and inside steamapprun_pipeline_cache.<hash>/
# directories (one per GPU+driver), so classify by filename, never by path.
FOZ_INPUT_RE = re.compile(
    r"^(steam_pipeline_cache"
    r"|steamapp_pipeline_cache"
    r"|steamapprun_pipeline_cache\.[0-9a-f]+\.\d+)\.foz$", re.I)
FOZ_LEDGER_RE = re.compile(r"^replay_cache\.[0-9a-f]+\.foz$", re.I)


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


SHADER_ENV_VARS = {
    # Only vars that still do something on modern stock Proton (DXVK >= 2.7) are
    # included. DXVK_ASYNC and DXVK_STATE_CACHE were both removed upstream once
    # Vulkan GPL (graphics_pipeline_library) made them obsolete, so they are
    # deliberately omitted - setting them achieves nothing. What remains is the
    # NVIDIA driver-level shader disk cache, which is independent of DXVK and
    # genuinely persists compiled shaders across runs.
    "__GL_SHADER_DISK_CACHE": "1",
    "__GL_SHADER_DISK_CACHE_PATH": str(Path.home() / ".cache" / "nvidia-shaders"),
    "__GL_SHADER_DISK_CACHE_SKIP_CLEANUP": "1",   # keep cache instead of purging on size
    "__GL_SHADER_DISK_CACHE_SIZE": "10737418240",  # 10 GiB ceiling
}


def read_environment():
    path = Path("/etc/environment")
    try:
        return path.read_text()
    except OSError:
        return ""


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for dirpath, _, filenames in os.walk(p):
            for fn in filenames:
                try:
                    total += (Path(dirpath) / fn).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def nvidia_cache_info() -> dict:
    """Size of the driver's shader cache, redirected and default locations.

    Both can hold data at once: the env vars only reach processes started after
    a re-login, so anything already running (compositor, browser, Steam) keeps
    writing to the default path until you log out. Bytes in the default dir
    after enabling the redirect are stale, not a fault.
    """
    redirected = Path(SHADER_ENV_VARS["__GL_SHADER_DISK_CACHE_PATH"])
    default = Path.home() / ".cache" / "nvidia"
    return {
        "path": str(redirected),
        "size_bytes": _dir_size(redirected),
        "exists": redirected.is_dir(),
        "default_path": str(default),
        "default_size_bytes": _dir_size(default) if default.is_dir() else 0,
        "limit_bytes": int(SHADER_ENV_VARS["__GL_SHADER_DISK_CACHE_SIZE"]),
    }


def environment_shader_status() -> dict:
    txt = read_environment()
    present = {}
    for k in SHADER_ENV_VARS:
        m = re.search(rf"^{re.escape(k)}=(.*)$", txt, re.M)
        present[k] = m.group(1).strip().strip('"') if m else None
    out = {"enabled": all(present[k] is not None for k in SHADER_ENV_VARS),
           "vars": present}
    out["cache"] = nvidia_cache_info()
    out["sizes"] = [{"gb": gb, "bytes": b} for gb, b in SHADER_CACHE_SIZES]
    out["steam_cache"] = None      # filled by the route, which knows the root
    # the live ceiling is whatever /etc/environment says, not our default
    cur = present.get("__GL_SHADER_DISK_CACHE_SIZE")
    if cur and cur.isdigit():
        out["cache"]["limit_bytes"] = int(cur)
    return out


# Ceilings offered in the UI. NVIDIA's own default is 12 GB on recent drivers;
# these are a cap, not a reservation - nothing is allocated up front.
SHADER_CACHE_SIZES = [
    (10, 10 * 1024**3),
    (30, 30 * 1024**3),
    (50, 50 * 1024**3),
    (100, 100 * 1024**3),
]


def set_environment_shaders(enable, size_bytes=None) -> dict:
    """Add or remove the shader-cache env vars in /etc/environment via pkexec.
    Preserves every other line; only touches our keys.

    size_bytes overrides the cache ceiling (__GL_SHADER_DISK_CACHE_SIZE). It's
    a limit rather than an allocation, so a bigger number costs nothing until
    the shaders actually accumulate.
    """
    vars_out = dict(SHADER_ENV_VARS)
    if size_bytes is not None:
        n = int(size_bytes)
        allowed = [b for _, b in SHADER_CACHE_SIZES]
        if n not in allowed:
            raise RuntimeError(f"cache size must be one of {allowed}")
        vars_out["__GL_SHADER_DISK_CACHE_SIZE"] = str(n)
    Path(vars_out["__GL_SHADER_DISK_CACHE_PATH"]).mkdir(
        parents=True, exist_ok=True)
    txt = read_environment()
    lines = [l for l in txt.splitlines()
             if not any(l.strip().startswith(f"{k}=") for k in SHADER_ENV_VARS)]
    if enable:
        lines.append("# Proton Command Center - shader cache")
        for k, v in vars_out.items():
            lines.append(f'{k}="{v}"' if " " in v or "/" in v else f"{k}={v}")
    else:
        lines = [l for l in lines
                 if l.strip() != "# Proton Command Center - shader cache"]
    new = "\n".join(lines).rstrip() + "\n"

    # write via a temp file + pkexec cp (root-owned target)
    tmp = Path(tempfile.gettempdir()) / f"pcc-environment-{os.getpid()}"
    tmp.write_text(new)
    try:
        r = subprocess.run(["pkexec", "cp", str(tmp), "/etc/environment"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or
                               "pkexec was cancelled or failed")
    finally:
        tmp.unlink(missing_ok=True)
    return {"enabled": enable, "note": "Log out and back in for changes to apply."}


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


def scan_game_dlss(install_path):
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
    for _ in range(4):
        parent = base.parent
        if parent == base or len(parent.parts) <= 2:
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
    dwmapi.dll, sitting next to the game's own ue4ss/ folder. Companion
    fixes some games require (e.g. NaniteRayTracingFix.asi) are separate .asi
    files loaded from the same directory. None of this fires under Proton
    unless WINEDLLOVERRIDES forces the native dwmapi.dll over Proton's own
    stub, so this only reports what's on disk - the launch-option toggle is
    a separate, explicit step."""
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


ULTRAPLUS_DIR = DATA_DIR / "tools" / "ultraplus-manager"
ULTRAPLUS_NEXUS_URL = "https://www.nexusmods.com/site/mods/1586"


def ultraplus_manager_binary():
    exe = ULTRAPLUS_DIR / "UltraPlusManager.Linux"
    return exe if exe.is_file() else None


def ultraplus_manager_status() -> dict:
    exe = ultraplus_manager_binary()
    return {"installed": exe is not None, "path": str(exe) if exe else None}


def install_ultraplus_manager(zip_path_str) -> dict:
    """Unpack a manually-downloaded 'UltraPlus Manager Linux' zip into
    DATA_DIR/tools. Nexus Mods has no anonymous/API download for non-premium
    accounts, so this is a two-step flow: the Settings button opens the mod
    page in a browser, and this call points PCC at whatever the user saved
    from there (typically ~/Downloads)."""
    import zipfile
    src = Path(zip_path_str).expanduser()
    if not src.is_file():
        raise RuntimeError(f"File not found: {src}")
    if src.suffix.lower() != ".zip":
        raise RuntimeError("Expected the Ultra+ Manager Linux .zip from Nexus Mods")
    with zipfile.ZipFile(src) as zf:
        names = [n for n in zf.namelist() if n and not n.endswith("/")]
        for n in names:
            if n.startswith("/") or ".." in n.split("/"):
                raise RuntimeError(f"unsafe path in archive: {n}")
        roots = {n.split("/", 1)[0] for n in names if "/" in n}
        with tempfile.TemporaryDirectory() as tmp:
            zf.extractall(tmp)
            tmp_path = Path(tmp)
            payload = tmp_path / next(iter(roots)) if len(roots) == 1 else tmp_path
            if not (payload / "UltraPlusManager.Linux").is_file():
                raise RuntimeError("Archive doesn't look like UltraPlus Manager Linux "
                                   "(no UltraPlusManager.Linux binary inside)")
            ULTRAPLUS_DIR.parent.mkdir(parents=True, exist_ok=True)
            if ULTRAPLUS_DIR.exists():
                shutil.rmtree(ULTRAPLUS_DIR)
            shutil.move(str(payload), str(ULTRAPLUS_DIR))
    exe = ULTRAPLUS_DIR / "UltraPlusManager.Linux"
    exe.chmod(exe.stat().st_mode | 0o111)
    return ultraplus_manager_status()


def launch_ultraplus_manager() -> dict:
    """UltraPlus Manager has no CLI flag to jump to a specific game (checked
    the shipped binary/DLL for --game/--appid style args - it's GUI-only, you
    pick the game inside it), so this opens the app itself rather than any
    one game's settings."""
    exe = ultraplus_manager_binary()
    if not exe:
        raise RuntimeError("Ultra+ Manager isn't installed yet - get it from Settings")
    _spawn_detached([str(exe)])
    return {"launched": True}


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

    # PRIMARY: DLSS Swapper's manifest - refreshed constantly, carries the
    # newest SR/FG/RR versions (this is the fix for "not fetching the latest").
    try:
        TASKS[task_id]["detail"] = "Checking DLSS Swapper manifest"
        got = _manifest_latest(kind, task_id)
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
    except Exception as e:
        errors.append(f"manifest: {e}")
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


# --------------------------------------------------------------------------
# ReShade
# --------------------------------------------------------------------------

RESHADE_DOWNLOADS_PAGE = "https://reshade.me/"

# ReShade's own default package selection - crosire/reshade-shaders'
# EffectPackages.ini lists every installable shader pack; these two
# (Standard effects + SweetFX) are the ones marked Enabled=1, i.e. what the
# official Windows installer installs when you accept its defaults.
DEFAULT_SHADER_PACKAGES = [
    {"url": "https://github.com/crosire/reshade-shaders/archive/refs/heads/slim.zip",
     "deny": set()},
    {"url": "https://github.com/CeeJayDK/SweetFX/archive/refs/heads/master.zip",
     "deny": {"Template.fx"}},
]

# Which system DLL name ReShade gets installed as, per detected graphics API.
# D3D10/11/12 all create their device through DXGI's swap chain, so all three
# hook via dxgi.dll - that's the standard ReShade convention on both Windows
# and Linux/Proton. Vulkan (an implicit Vulkan layer, not a proxy DLL) and
# D3D8 (needs its own D3D9 wrapper first) aren't supported here.
RESHADE_PROXY_DLL = {
    "d3d9": "d3d9.dll",
    "d3d10": "dxgi.dll",
    "d3d11": "dxgi.dll",
    "d3d12": "dxgi.dll",
    "opengl": "opengl32.dll",
}

# (dll import name, api id, priority) - higher priority wins when a game
# imports more than one. Mirrors RankFTW/RHI's GraphicsApiDetector.
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


def pe_imports(path):
    """Read a PE exe's machine type (32/64-bit) and imported DLL names with no
    dependency beyond stdlib: parse the DOS/PE/section headers by hand and
    walk the import directory table. Returns (bitness, {lowercased dll
    names}), or (None, set()) if it doesn't look like a PE file."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None, set()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None, set()
    pe_off = struct.unpack_from("<i", data, 0x3C)[0]
    if pe_off < 0 or pe_off + 24 > len(data) or data[pe_off:pe_off + 4] != b"PE\x00\x00":
        return None, set()
    coff = pe_off + 4
    machine = struct.unpack_from("<H", data, coff)[0]
    bitness = {0x8664: 64, 0x14c: 32}.get(machine)
    n_sections = struct.unpack_from("<H", data, coff + 2)[0]
    size_opt = struct.unpack_from("<H", data, coff + 16)[0]
    opt_off = coff + 20
    if size_opt < 2 or opt_off + size_opt > len(data):
        return bitness, set()
    magic = struct.unpack_from("<H", data, opt_off)[0]
    if magic == 0x10B:      # PE32
        imp_dir_off = opt_off + 104
    elif magic == 0x20B:    # PE32+
        imp_dir_off = opt_off + 120
    else:
        return bitness, set()
    if imp_dir_off + 8 > len(data):
        return bitness, set()
    import_rva = struct.unpack_from("<I", data, imp_dir_off)[0]
    if not import_rva:
        return bitness, set()
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

    imp_off = rva2off(import_rva)
    if imp_off is None:
        return bitness, set()
    names = set()
    i = 0
    while True:
        entry_off = imp_off + i * 20
        if entry_off + 20 > len(data):
            break
        name_rva = struct.unpack_from("<I", data, entry_off + 12)[0]
        if not name_rva:
            break
        noff = rva2off(name_rva)
        if noff is not None:
            end = data.find(b"\x00", noff, noff + 256)
            if end < 0:
                end = noff + 256
            names.add(data[noff:end].decode("ascii", "ignore").lower())
        i += 1
    return bitness, names


def detect_graphics_api(dll_names) -> str | None:
    """Highest-priority graphics API among a PE's imported DLLs. A DX12 game
    that creates its device through dxgi.dll alone (no d3d12.dll import,
    common in modern engines) is inferred as DX12 when nothing higher-
    priority was found - same rule RankFTW/RHI's detector uses."""
    best, best_pri = None, 0
    for dll, api, pri in _GRAPHICS_DLL_PRIORITY:
        if dll in dll_names and pri > best_pri:
            best, best_pri = api, pri
    if "dxgi.dll" in dll_names and best_pri < 5:
        return "d3d12"
    return best


def _find_game_exe(install_path):
    """Best-effort: the largest .exe in the install tree, skipping obvious
    installers/redistributables/anti-cheat launchers by name. Nothing on disk
    says which file is the real game binary, so this is a heuristic - the
    API-override dropdown in the UI exists for when it guesses wrong."""
    base = Path(install_path)
    if not base.is_dir():
        return None
    SKIP_DIRS = {"_commonredist", "redist", "directx", "vcredist"}
    SKIP_NAME_HINTS = ("unins", "redist", "vcredist", "directx", "dxsetup",
                       "crashreporter", "crashpad", "easyanticheat",
                       "battleye", "vc_redist")
    best, best_size = None, -1
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for fn in filenames:
            low = fn.lower()
            if not low.endswith(".exe") or any(h in low for h in SKIP_NAME_HINTS):
                continue
            p = Path(dirpath) / fn
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > best_size:
                best, best_size = p, size
    return best


def reshade_latest() -> dict:
    """Scrapes reshade.me for the current version and Full Add-on Support
    build (needed for third-party addons like OptiScaler to load alongside
    it). ReShade ships no GitHub releases to query, so the download page is
    the only source. Cached 6h, same pattern as the GE-Proton release list."""
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


def ensure_reshade_engine(version, url=None, task_id=None) -> Path:
    """Downloads the ReShade setup .exe and pulls ReShade32.dll/ReShade64.dll
    straight out of it. The installer is a plain zip with a .NET stub exe
    prepended - stdlib zipfile finds the end-of-central-directory record by
    scanning back from EOF and reads it with no extra tooling needed (checked
    against the real 6.8.0 Addon build). Cached per version so repeat
    installs across games don't re-download."""
    import zipfile, io
    engine_dir = RESHADE_DIR / version
    if (engine_dir / "ReShade64.dll").is_file() and (engine_dir / "ReShade32.dll").is_file():
        return engine_dir
    if not url:
        url = reshade_latest()["url"]
    if task_id:
        TASKS[task_id] = {"status": "running", "progress": 10,
                          "detail": f"Downloading ReShade {version}"}
    data = _gh_bytes(url, task_id)
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


def ensure_default_shaders(task_id=None) -> Path:
    """One-time fetch of ReShade's own default shader selection (Standard
    effects + SweetFX) into a folder every game's ReShade.ini points at, so
    installing ReShade gives you working effects immediately instead of an
    empty effects list."""
    import zipfile, io
    shaders_dir = RESHADE_SHADERS_DIR / "Shaders"
    textures_dir = RESHADE_SHADERS_DIR / "Textures"
    if shaders_dir.is_dir() and any(shaders_dir.glob("*.fx")):
        return RESHADE_SHADERS_DIR
    shaders_dir.mkdir(parents=True, exist_ok=True)
    textures_dir.mkdir(parents=True, exist_ok=True)
    for pkg in DEFAULT_SHADER_PACKAGES:
        if task_id:
            TASKS[task_id]["detail"] = "Fetching default shaders"
        data = _gh_bytes(pkg["url"], None)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            roots = {n.split("/", 1)[0] for n in zf.namelist() if "/" in n}
            if len(roots) != 1:
                continue
            root = next(iter(roots))
            for n in zf.namelist():
                if n.endswith("/"):
                    continue
                if n.startswith(f"{root}/Shaders/"):
                    fn = n.rsplit("/", 1)[-1]
                    if fn not in pkg["deny"]:
                        (shaders_dir / fn).write_bytes(zf.read(n))
                elif n.startswith(f"{root}/Textures/"):
                    fn = n.rsplit("/", 1)[-1]
                    (textures_dir / fn).write_bytes(zf.read(n))
    return RESHADE_SHADERS_DIR


def scan_game_reshade(appid, install_path) -> dict:
    """Report ReShade status for a game: whatever PCC has on record for it,
    plus best-effort auto-detection of the exe/graphics API/bitness so the
    install call can usually proceed without asking the user anything."""
    state = load_state()
    rec = state.get("reshade_installs", {}).get(appid)
    exe = _find_game_exe(install_path)
    bitness, dll_names = pe_imports(exe) if exe else (None, set())
    api = detect_graphics_api(dll_names) if dll_names else None
    result = {"exe": str(exe) if exe else None, "detected_api": api,
             "detected_bitness": bitness, "installed": False}
    if rec:
        p = Path(rec["path"])
        result.update({"installed": p.is_file(), "path": rec["path"],
                       "api": rec.get("api"), "version": rec.get("version"),
                       "bitness": rec.get("bitness"),
                       "proxy_name": p.name})
    return result


def install_reshade(appid, install_path, exe_override=None, api_override=None,
                    task_id=None) -> dict:
    """Installs ReShade for one game: picks (or takes) the exe, detects (or
    takes) the graphics API and bitness, drops the matching ReShade DLL in
    next to the exe under the name that API hooks through, and points a
    fresh ReShade.ini at the shared shader library. Refuses to overwrite any
    proxy-named DLL it didn't put there itself - the foreign-DLL protection
    RankFTW/RHI's own installer has, since a game folder's dxgi.dll could
    just as easily belong to OptiScaler or a bundled DXVK build."""
    exe = Path(exe_override).expanduser() if exe_override else _find_game_exe(install_path)
    if not exe or not exe.is_file():
        raise RuntimeError("Couldn't find the game's .exe under its install folder — "
                           "point Command Center at it manually.")
    bitness, dll_names = pe_imports(exe)
    api = api_override or detect_graphics_api(dll_names)
    if not api:
        raise RuntimeError("Couldn't tell which graphics API this game uses — pick one manually.")
    if api not in RESHADE_PROXY_DLL:
        raise RuntimeError(f"{api.upper()} isn't supported yet (native Vulkan needs a "
                           "Wine-prefix layer registration Command Center doesn't do; "
                           "D3D8 needs a D3D9 wrapper first).")
    bitness = bitness or 64   # nearly every modern Steam game is 64-bit
    proxy_name = RESHADE_PROXY_DLL[api]
    target = exe.parent / proxy_name

    # Foreign-DLL check happens before any download, so a refusal is instant
    # and never wastes a fetch on a call that was always going to fail.
    state = load_state()
    installs = state.setdefault("reshade_installs", {})
    rec = installs.get(appid)
    ours = bool(rec and rec.get("path") == str(target))
    if target.exists() and not ours:
        raise RuntimeError(f"{proxy_name} already exists in {exe.parent} and wasn't "
                           "installed by Command Center — refusing to overwrite it "
                           "(could be DXVK, OptiScaler, or another mod's file). Move "
                           "it aside first if you're sure it's safe to replace.")

    info = reshade_latest()
    engine_dir = ensure_reshade_engine(info["version"], info["url"], task_id=task_id)
    src_dll = engine_dir / ("ReShade64.dll" if bitness == 64 else "ReShade32.dll")

    # No backup step: the refusal above already guarantees target is either
    # absent or a DLL Command Center itself put there, so there's never a
    # foreign original to preserve here.
    shutil.copy2(src_dll, target)

    ensure_default_shaders(task_id=task_id)
    ini_path = exe.parent / "ReShade.ini"
    wrote_ini = not ini_path.exists()
    if wrote_ini:
        shaders_win = "Z:" + str(RESHADE_SHADERS_DIR / "Shaders").replace("/", "\\")
        textures_win = "Z:" + str(RESHADE_SHADERS_DIR / "Textures").replace("/", "\\")
        ini_path.write_text(
            "[GENERAL]\n"
            f"EffectSearchPaths={shaders_win}\n"
            f"TextureSearchPaths={textures_win}\n"
            "PresetPath=.\\ReShadePreset.ini\n")

    installs[appid] = {"path": str(target), "api": api, "bitness": bitness,
                       "version": info["version"]}
    save_state(state)
    return {"installed": True, "path": str(target), "proxy_dll": proxy_name,
            "api": api, "bitness": bitness, "version": info["version"],
            "wrote_ini": wrote_ini,
            "winedlloverride": f"{proxy_name[:-4]}=n,b"}


def remove_reshade(appid) -> dict:
    """Removes ReShade from a game: deletes the proxy DLL Command Center
    installed. Install refuses to ever touch a pre-existing foreign DLL (see
    install_reshade), so there's never an original to restore here. The
    ReShade.ini and shared shader library are left alone - the ini may hold
    tuned settings, and the shaders are shared across every other game using
    it."""
    state = load_state()
    installs = state.get("reshade_installs", {})
    rec = installs.pop(appid, None)
    if not rec:
        raise RuntimeError("No ReShade install tracked for this game.")
    Path(rec["path"]).unlink(missing_ok=True)
    save_state(state)
    return {"removed": True}


def _reshade_install_task(task_id, appid, install_path, exe, api) -> None:
    try:
        result = install_reshade(appid, install_path, exe_override=exe,
                                 api_override=api, task_id=task_id)
        TASKS[task_id] = {"status": "done", "progress": 100,
                          "detail": f"Installed as {result['proxy_dll']}",
                          "result": result}
    except Exception as e:
        TASKS[task_id] = {"status": "error", "progress": 0, "detail": str(e)}


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
    "base_wrappers": ["game-performance"],
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
    per_tool = {name: sorted(proton_env_vars(d))
                for name, d in _tool_dirs(root).items()}
    known = sorted(set().union(*(set(v) for v in per_tool.values()))
                   if per_tool else [])
    return {"tools": per_tool, "known": known}


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


def list_shortcuts(root: Path) -> list:
    """Every non-Steam shortcut Steam knows about, already in Command
    Center's game shape (appid/name/install_path/library/...) so it merges
    straight into the normal library list via all_games(). `library` points
    at the same steamapps Steam itself uses for a shortcut's Proton compat
    data and shader cache, so has_cache/cache_info work unmodified."""
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
            "size_bytes": 0,
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
# MangoHud benchmarks (ported from Stutterless)
# --------------------------------------------------------------------------
BENCH_DIR = DATA_DIR / "benchmarks"
BENCH_DIR.mkdir(parents=True, exist_ok=True)


def benchmark_launch_string(appid: str):
    folder = BENCH_DIR / str(appid)
    folder.mkdir(parents=True, exist_ok=True)  # MangoHud won't create it
    cfg = (f"output_folder={folder},autostart_log=1,log_duration=300,"
           f"benchmark_percentiles=AVG+1+0.1")
    return f"MANGOHUD=1 MANGOHUD_CONFIG={cfg} %command%"


def _parse_mangohud_csv(path):
    """MangoHud CSVs have two header sections: a system header (line 1-2)
    then the data-column header containing 'frametime'. Frametime is in
    microseconds; normalise to ms. Falls back to fps-only logs."""
    try:
        with open(path, "r", errors="ignore") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        return None
    if len(lines) < 4:
        return None
    ft_col, data_start = None, 0
    for i, ln in enumerate(lines):
        if "frametime" in ln.lower():
            cols = [c.strip().lower() for c in ln.split(",")]
            for idx, c in enumerate(cols):
                if c.startswith("frametime"):
                    ft_col = idx
            data_start = i + 1
            break
    frametimes = []

    def push(val):
        if val <= 0:
            return
        if val > 1e5:     # nanoseconds
            ms = val / 1e6
        elif val > 200:   # microseconds
            ms = val / 1e3
        else:             # already ms
            ms = val
        if 0.1 <= ms <= 1000:
            frametimes.append(ms)

    if ft_col is not None:
        for ln in lines[data_start:]:
            parts = ln.split(",")
            if len(parts) > ft_col:
                try:
                    push(float(parts[ft_col]))
                except ValueError:
                    pass
    else:  # very old MangoHud: fps in column 0
        for ln in lines:
            try:
                fps = float(ln.split(",")[0])
            except (ValueError, IndexError):
                continue
            if fps > 0:
                push(1000.0 / fps)
    if len(frametimes) < 20:
        return None
    return frametimes


def _analyse_frametimes(ft) -> dict | None:
    n = len(ft)
    if n == 0:
        return None
    s = sorted(ft)
    avg_ft = sum(ft) / n
    k1 = max(1, n // 100)
    k01 = max(1, n // 1000)
    median = s[n // 2]
    stutters = sum(1 for x in ft if x > 2.0 * median)
    return {
        "frames": n,
        "avg_fps": round(1000.0 / avg_ft, 1),
        "low1_fps": round(1000.0 / (sum(s[-k1:]) / k1), 1),
        "low01_fps": round(1000.0 / (sum(s[-k01:]) / k01), 1),
        "stutter_count": stutters,
        "stutter_pct": round(100.0 * stutters / n, 2),
    }


def _downsample(series, target=200):
    """Bucket to ~target points using max() so stutter spikes survive."""
    n = len(series)
    if n <= target:
        return [round(x, 2) for x in series]
    bucket, out, i = n / target, [], 0.0
    while i < n:
        chunk = series[int(i):int(i + bucket) or int(i) + 1]
        if chunk:
            out.append(round(max(chunk), 2))
        i += bucket
    return out


def get_benchmark_data(root: Path, appid: str):
    folder = BENCH_DIR / str(appid)
    result = {
        "has_mangohud": shutil.which("mangohud") is not None,
        "launch_string": benchmark_launch_string(appid),
        "folder": str(folder),
        "before": None, "after": None,
        "before_graph": None, "after_graph": None,
        "improvement_pct": None, "log_count": 0, "diag": [],
    }
    diag = result["diag"]
    logs = sorted(
        ((p, p.stat().st_mtime) for p in folder.rglob("*.csv")),
        key=lambda x: x[1]) if folder.is_dir() else []
    result["log_count"] = len(logs)
    if not logs:
        diag.append("No MangoHud logs yet — save the benchmark launch options, "
                    "play for a few minutes, and check back.")
        return result
    usable = []
    for p, mt in logs:
        ft = _parse_mangohud_csv(p)
        if ft:
            usable.append((p, mt, ft))
        else:
            diag.append(f"Couldn't parse {p.name} (too short — play longer).")
    if not usable:
        diag.append("Logs found but none had enough frametime data "
                    "(play at least ~30 seconds).")
        return result
    split = load_state().get("compiled", {}).get(str(appid), {}).get("compiled_at", 0)
    before = [u for u in usable if u[1] < split] if split else []
    after = [u for u in usable if u[1] >= split] if split else []
    if (not before or not after) and len(usable) >= 2:
        before, after = [usable[0]], [usable[-1]]
        diag.append("Using oldest log as 'before' and newest as 'after'.")
    elif len(usable) == 1:
        if split and usable[0][1] >= split:
            after = [usable[0]]
            diag.append("Only an 'after' run so far — nothing to compare against.")
        else:
            before = [usable[0]]
            diag.append("Only a 'before' run — compile, play again, then compare.")

    def analyse(u):
        if not u:
            return None, None
        ft = u[-1][2]
        return _analyse_frametimes(ft), _downsample(ft)

    result["before"], result["before_graph"] = analyse(before)
    result["after"], result["after_graph"] = analyse(after)
    if result["before"] and result["after"] and result["before"]["low1_fps"] > 0:
        result["improvement_pct"] = round(
            100.0 * (result["after"]["low1_fps"] - result["before"]["low1_fps"])
            / result["before"]["low1_fps"], 1)
        diag.append("Comparison ready.")
    return result


# --------------------------------------------------------------------------
# Shader cache
# --------------------------------------------------------------------------

def cache_info(root: Path, appid: str):
    out = []
    for lib in library_folders(root):
        c = lib / "shadercache" / str(appid)
        if c.is_dir():
            size = 0
            files = 0
            for dirpath, _, filenames in os.walk(c):
                for fn in filenames:
                    try:
                        size += (Path(dirpath) / fn).stat().st_size
                        files += 1
                    except OSError:
                        pass
            # Only the count is ever used. This used to ship every .foz path -
            # hundreds of strings per game - so the UI could call .length on it.
            foz = sum(1 for _ in c.rglob("*.foz"))
            out.append({"path": str(c), "size_bytes": size, "files": files,
                        "foz": foz})
    return out


def clear_cache(root: Path, appid: str, keep_recordings=True) -> dict:
    """Default clears COMPILED artifacts but preserves fozpipelinesv6/
    recordings - Steam's source data for its shader pass, costly to
    regenerate. keep_recordings=False deletes everything."""
    cleared, kept = [], 0
    for entry in cache_info(root, appid):
        base = Path(entry["path"])
        if not keep_recordings:
            shutil.rmtree(base, ignore_errors=True)
            cleared.append(str(base))
            continue
        for child in base.iterdir():
            if child.name == "fozpipelinesv6":
                kept += sum(1 for _ in child.rglob("*.foz"))
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            cleared.append(str(child))
    return {"cleared": cleared, "kept_recordings": kept}


# --------------------------------------------------------------------------
# Steam's own shader processing ("Processing Vulkan shaders" at launch)
# --------------------------------------------------------------------------
SHADER_KEY_RE = re.compile(r"shader|fossilize|precach", re.I)


def _walk_vdf(node, prefix=()) -> None:
    for k, v in (node or {}).items():
        if isinstance(v, dict):
            yield from _walk_vdf(v, prefix + (k,))
        else:
            yield prefix + (k,), v


# --------------------------------------------------------------------------
# Steam's shader background-processing thread count
# --------------------------------------------------------------------------
# Steam defaults to a fraction of your logical cores for the "Processing Vulkan
# shaders" pass, which is why it can crawl on an otherwise idle machine. The
# override lives in steam_dev.cfg as a plain `key value` line -- NOT in the VDF
# configs, and NOT in any Steam UI.
#
# Two things that make this easy to get wrong:
#   1. Steam never creates steam_dev.cfg. It does not exist on a stock install,
#      so this has to write the file, not just edit it.
#   2. It's read once at client startup, so Steam needs a full restart -- not
#      just a settings reload -- for a change to apply.
SHADER_THREADS_KEY = "unShaderBackgroundProcessingThreads"
# Leave this many logical cores free so the desktop stays responsive while the
# pass runs. Steam pinned to every core makes the machine miserable to use.
SHADER_THREADS_RESERVE = 2


def logical_cores():
    """Logical CPUs actually usable by this process.
    sched_getaffinity respects taskset/cgroup limits; cpu_count doesn't."""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def recommended_shader_threads():
    return max(1, logical_cores() - SHADER_THREADS_RESERVE)


def steam_dev_cfg(root: Path):
    return Path(root) / "steam_dev.cfg"


def get_shader_threads(root: Path) -> int | None:
    """Current override, or None if unset (i.e. Steam's own default applies)."""
    cfg = steam_dev_cfg(root)
    if not cfg.is_file():
        return None
    for line in cfg.read_text(errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[0].lower() == SHADER_THREADS_KEY.lower():
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def set_shader_threads(root: Path, threads) -> dict:
    """Write the override, creating steam_dev.cfg if absent and preserving any
    other lines already in it. Pass threads=None to remove the override."""
    cores = logical_cores()
    if threads is not None:
        threads = int(threads)
        if not 1 <= threads <= cores:
            raise RuntimeError(f"threads must be between 1 and {cores}")
    cfg = steam_dev_cfg(root)
    lines = (cfg.read_text(errors="replace").splitlines()
             if cfg.is_file() else [])
    kept = [l for l in lines
            if l.strip().split()[:1] != [SHADER_THREADS_KEY]
            and not l.strip().lower().startswith(SHADER_THREADS_KEY.lower())]
    if threads is not None:
        kept.append(f"{SHADER_THREADS_KEY} {threads}")
    body = "\n".join(kept).strip()
    if body:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(body + "\n")
    elif cfg.is_file():
        cfg.unlink()          # nothing left worth keeping
    return {"threads": threads, "cores": cores, "file": str(cfg),
            "restart_required": True}


def shader_threads_status(root: Path) -> dict:
    return {"current": get_shader_threads(root),
            "cores": logical_cores(),
            "recommended": recommended_shader_threads(),
            "reserve": SHADER_THREADS_RESERVE,
            "file": str(steam_dev_cfg(root)),
            "exists": steam_dev_cfg(root).is_file()}


def steam_shader_settings(root: Path) -> dict:
    """Steam's shader-related BOOLEAN settings, across its configs.

    Two filters, both learned the hard way from real config data:

    1. Skip the ShaderCacheManager/App/<appid>/ subtree. Those hold
       ShaderCacheSize - a byte count per game, e.g. 8198563848 - which is
       reported data, not a setting. Matching on the key name alone surfaced
       17 of them as checkboxes; toggling one would have written "1" into a
       size field and corrupted Steam's config.
    2. Only keep values that are actually "0"/"1". Anything else isn't a
       switch, whatever its name looks like.

    Steam only persists these once you've touched them, so an empty result
    means 'still at defaults'.
    """
    out = []
    candidates = [root / "config/config.vdf"] + list(find_localconfigs(root))
    for cfg in candidates:
        if not cfg.is_file():
            continue
        try:
            data = vdf_parse(cfg.read_text(errors="replace"))
        except Exception:
            continue
        keys = []
        for kp, val in _walk_vdf(data):
            if not SHADER_KEY_RE.search(kp[-1]):
                continue
            if any(seg.lower() == "app" for seg in kp[:-1]):
                continue                      # per-game data, not a setting
            if str(val) not in ("0", "1"):
                continue                      # not a switch
            keys.append({"path": "/".join(kp), "key": kp[-1], "value": val})
        if keys:
            out.append({"file": str(cfg), "keys": keys})
    return {"files": out, "found": bool(out)}


def steam_shader_cache_sizes(root: Path) -> dict:
    """Steam's own per-game shader cache sizes, as it records them.

    Same subtree the settings scan deliberately skips - useful as a total,
    dangerous as toggles.
    """
    cfg = root / "config/config.vdf"
    total, games = 0, 0
    if cfg.is_file():
        try:
            data = vdf_parse(cfg.read_text(errors="replace"))
            for kp, val in _walk_vdf(data):
                if kp[-1].lower() != "shadercachesize":
                    continue
                if not any(seg.lower() == "app" for seg in kp[:-1]):
                    continue
                try:
                    n = int(val)
                except (TypeError, ValueError):
                    continue
                if n > 0:
                    total += n
                    games += 1
        except Exception:
            pass
    return {"total_bytes": total, "games": games}


def set_steam_shader_setting(root: Path, file, path, value, close_steam=False) -> dict:
    cfg = Path(file)
    if cfg.name not in ("config.vdf", "localconfig.vdf") or not cfg.is_file():
        raise RuntimeError("refusing to write an unexpected file")
    # Belt and braces alongside the filter in steam_shader_settings: the
    # ShaderCacheManager/App/<appid>/ subtree holds ShaderCacheSize byte counts,
    # and writing a 0/1 into one would tell Steam a multi-GB cache is "1".
    segs = [s.lower() for s in str(path).split("/")]
    if "app" in segs[:-1]:
        raise RuntimeError("refusing to write per-game shader cache data")
    if str(value) not in ("0", "1"):
        raise RuntimeError("shader settings are booleans; refusing to write "
                           f"{value!r}")
    if steam_running():
        if close_steam:
            shutdown_steam()
        else:
            raise RuntimeError("Steam is running — it overwrites its configs "
                               "on exit. Close it first.")
    data = vdf_parse(cfg.read_text(errors="replace"))
    parts = path.split("/")
    node = data
    for k in parts[:-1]:
        nxt = ci_get(node, k)
        if not isinstance(nxt, dict):
            raise RuntimeError(f"key path not found: {path}")
        node = nxt
    for k in list(node.keys()):
        if k.lower() == parts[-1].lower():
            node[k] = str(value)
            break
    else:
        raise RuntimeError(f"key not found: {path}")
    bak = cfg.with_suffix(f".vdf.pcc-{int(time.time())}.bak")
    shutil.copy2(cfg, bak)
    tmp = cfg.with_suffix(".vdf.pcc-tmp")
    tmp.write_text(vdf_dump(data))
    tmp.replace(cfg)
    return {"saved": True, "path": path, "value": str(value), "backup": str(bak)}



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
                    log_dir=None, toggle_key="Shift_R+F12"):
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
    if log_dir:
        lines += [f"output_folder={log_dir}", "log_duration=300",
                  "autostart_log=0", "benchmark_percentiles=AVG,1,0.1"]
    return "\n".join(lines) + "\n"


def apply_mangohud_config(preset="reference", pin_gpu=None, log_dir=None) -> dict:
    MANGOHUD_DIR.mkdir(parents=True, exist_ok=True)
    dest = MANGOHUD_DIR / "MangoHud.conf"
    backup = None
    if dest.is_file():
        backup = dest.with_suffix(f".conf.pcc-{int(time.time())}.bak")
        shutil.copy2(dest, backup)
    text = mangohud_config(preset, pin_gpu=pin_gpu, log_dir=log_dir)
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
                reshade_installs = state.get("reshade_installs", {})
                for g in games:
                    # Shortcuts carry their own LaunchOptions field (surfaced by
                    # list_shortcuts as launch_options_shortcut) rather than living
                    # in localconfig.vdf's apps node like real Steam games do.
                    g["has_launch_options"] = (bool(g.get("launch_options_shortcut"))
                                               if g.get("custom")
                                               else g["appid"] in lo_appids)
                    g["has_cache"] = any(
                        (Path(lib) / "shadercache" / g["appid"]).is_dir()
                        for lib in [g["library"]])
                    g["has_dlss"] = bool(dlss_seen.get(g["appid"]))
                    g["has_ultraplus"] = bool(ultraplus_seen.get(g["appid"]))
                    g["has_reshade"] = g["appid"] in reshade_installs
                self._json({"games": games})
            elif m := re.match(r"^/api/game/(\d+)/launch_options$", self.path):
                self._json(get_launch_options(root, m.group(1)))
            elif m := re.match(r"^/api/game/(\d+)/dlss$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                dlls = scan_game_dlss(g["install_path"]) if g else []
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
            elif self.path == "/api/ultraplus_manager/status":
                self._json(ultraplus_manager_status())
            elif self.path == "/api/progress":
                self._json({"games": install_progress(root)})
            elif self.path == "/api/owned_games":
                self._json({"games": owned_games(root)})
            elif self.path == "/api/steam/shader_settings":
                self._json(steam_shader_settings(root))
            elif self.path == "/api/steam/shader_threads":
                self._json(shader_threads_status(root))
            elif self.path == "/api/hardware":
                self._json(detect_hardware())
            elif self.path == "/api/backup/export":
                self._json(export_backup())
            elif self.path == "/api/proton/list":
                self._json(list_ge_proton())
            elif self.path == "/api/env/shaders":
                st = environment_shader_status()
                if root:
                    st["steam_cache"] = steam_shader_cache_sizes(root)
                self._json(st)
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
            elif m := re.match(r"^/api/game/(\d+)/cache$", self.path):
                self._json({"caches": cache_info(root, m.group(1))})
            elif m := re.match(r"^/api/game/(\d+)/protondb(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(2) or "")
                if qs.get("cached"):
                    self._json(protondb_cached(m.group(1)) or {"tier": None,
                                                               "cached": True})
                else:
                    self._json(protondb_summary(m.group(1)) or {"tier": None})
            elif m := re.match(r"^/api/game/(\d+)/benchmark$", self.path):
                self._json(get_benchmark_data(root, m.group(1)))
            elif m := re.match(r"^/api/owned(?:\?(.*))?$", self.path):
                qs = urllib.parse.parse_qs(m.group(1) or "")
                force = (qs.get("refresh") or ["0"])[0] == "1"
                self._json(fetch_owned_games(root, force=force))
            elif self.path == "/api/dlss/library":
                self._json({"dlls": dll_library()})
            elif self.path == "/api/reshade/latest":
                self._json(reshade_latest())
            elif m := re.match(r"^/api/game/(\d+)/reshade$", self.path):
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(m.group(1))
                self._json(scan_game_reshade(m.group(1), g["install_path"]) if g
                          else {"installed": False})
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
            elif self.path == "/api/env/shaders":
                self._json(set_environment_shaders(
                    bool(body.get("enable")), body.get("size_bytes")))
            elif self.path == "/api/backup/restore":
                self._json(restore_backup(body["archive"]))
            elif self.path == "/api/ultraplus_manager/install":
                self._json(install_ultraplus_manager(body["zip"]))
            elif self.path == "/api/ultraplus_manager/launch":
                self._json(launch_ultraplus_manager())
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
            elif self.path == "/api/steam/shader_settings":
                self._json(set_steam_shader_setting(
                    root, body["file"], body["path"], body["value"],
                    close_steam=bool(body.get("close_steam"))))
            elif self.path == "/api/steam/shader_threads":
                # threads omitted -> use the recommended value; null -> unset
                t = (body["threads"] if "threads" in body
                     else recommended_shader_threads())
                self._json(set_shader_threads(root, t))
            elif self.path == "/api/mangohud/apply":
                self._json(apply_mangohud_config(
                    body.get("preset", "standard"),
                    pin_gpu=body.get("pin_gpu"),
                    log_dir=str(BENCH_DIR) if body.get("enable_logging") else None))
            elif self.path == "/api/steam/launch":
                self._json({"launched": launch_steam()})
            elif self.path == "/api/dlss/swap":
                self._json(swap_dll(body["game_dll"], body["library_dll"]))
            elif self.path == "/api/dlss/restore":
                self._json(restore_dll(body["game_dll"]))
            elif self.path == "/api/dlss/import":
                self._json(import_dll(body["path"]))
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
            elif self.path == "/api/reshade/install":
                appid = body["appid"]
                games = {g["appid"]: g for g in all_games(root)}
                g = games.get(appid)
                if not g:
                    self._json({"error": "unknown appid"}, 404); return
                tid = str(uuid.uuid4())
                TASKS[tid] = {"status": "running", "progress": 0, "detail": "Starting"}
                threading.Thread(target=_reshade_install_task,
                                 args=(tid, appid, g["install_path"],
                                       body.get("exe"), body.get("api")),
                                 daemon=True).start()
                self._json({"task": tid})
            elif self.path == "/api/reshade/remove":
                self._json(remove_reshade(body["appid"]))
            elif m := re.match(r"^/api/game/(\d+)/cache/clear$", self.path):
                self._json(clear_cache(root, m.group(1),
                                       keep_recordings=body.get("keep_recordings", True)))
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
