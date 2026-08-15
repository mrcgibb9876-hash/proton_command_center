# Proton Command Center

Steam on Linux hides the settings that matter. Launch options are a single-line
text box. Proton versions are a dropdown with no idea what each build supports.
DLSS DLLs mean hunting through game folders. Shader processing crawls because
Steam quietly uses a quarter of your cores.

This puts all of it in one place, in your browser, on your machine.

![Your Steam library, with status rails showing what's configured](assets/screenshots/library.png)

```
http://localhost:8686
```

Python standard library only. No dependencies, no telemetry, no account.


---

## What it does

**Builds launch options with toggles instead of guesswork.** Pick from DXVK,
HDR, Wayland, Reflex and the rest. Each one says what it does and what it
costs.

**Knows what your Proton build actually supports.** GE-Proton 11-1 reads 90
environment variables. Valve's Proton 11.0 reads 60. Set a GE-only option on a
Valve build and it does nothing, silently. Command Center scans each installed
build and greys out what won't work, so you can't ship dead options.

**Manages DLSS DLLs.** Every DLL in the game with its version, one-click
upgrade, backups you can roll back.

**Installs ReShade.** Detects the game's graphics API from its own exe and
bitness, drops the matching build next to it under the right proxy name, and
seeds a shared shader library. Refuses to overwrite a proxy DLL it didn't
install itself.

**Fixes Steam's shader processing.** Steam defaults to a fraction of your cores
for the "Processing Vulkan shaders" pass. The setting isn't in Steam's UI at
all - it lives in a file Steam doesn't even create. One click sets it to every
core but two.

**Shows where your disk went.** NVIDIA's shader cache and Steam's own, with
per-game breakdowns and clear-out buttons.

**Benchmarks before and after.** MangoHud logs split at the change, compared on
avg FPS, 1% and 0.1% lows, and stutter count.

**Works from the sofa.** Full controller navigation, fullscreen, and a hint bar
with the mapping.

**Shows your whole library.** Owned games you haven't installed appear greyed,
with an Install button and live progress.

---

## Requirements

| | |
|---|---|
| **Required** | Python 3, Steam (logged in once) |
| **Optional** | `mangohud` for the overlay and benchmarks |
| **Optional** | An NVIDIA driver for DLSS and shader cache features |

Built and tested on Arch-based distros with an NVIDIA slant. Nothing is
Arch-specific beyond the install script.

## Install

```bash
tar xzf proton-command-center-*.tar.gz
cd proton-command-center
./install.sh
```

Or from the AUR:

```bash
yay -S proton-command-center
systemctl --user enable --now proton-command-center
```

Either way you get a launcher, an app-menu entry, and a systemd user service
that starts at login and restarts itself if it dies.

## Two optional keys

Both free, both stored locally in `config.json` (mode 0600), both used only
server-side. Skip them and everything else still works.

- **[SteamGridDB](https://www.steamgriddb.com)** - artwork for games Steam's CDN
  misses: betas, demos, delisted titles.
- **[Steam Web API](https://steamcommunity.com/dev/apikey)** - your full owned
  library, not just what's installed. Your SteamID is detected automatically.

---

## The details

### Launch tab

![Launch options built from toggles, with unsupported ones greyed out](assets/screenshots/launch.png)

Compatibility tools are read from disk, so only builds you actually have are
offered, and new releases show up on their own. Toggles cover DXVK, HDR,
native Wayland, Reflex, esync, GE-only extras like D7VK and OptiScaler,
and an Ultra+ mod loader toggle (`WINEDLLOVERRIDES="version=n,b"`) for UE4SS-based
mods from [theultraplace.com](https://theultraplace.com) - it forces Proton to
load version.dll from disk instead of its own stub. Switching the toggle on
copies Command Center's own bundled build of version.dll (Ultimate ASI
Loader) into the game's exe folder, backing up whatever was already there.
Opening a game whose folder has a `ue4ss/` directory shows what Ultra+ found
there (the loader DLL, any companion `.asi` fixes); the `ue4ss/` mod itself
still comes from Ultra+ Manager, not Command Center.

A game with Ultra+ set up gets an **ULTRA+** badge on its library card - click
the logo to launch Ultra+ Manager. Get the manager itself, or point Command
Center at an already-downloaded copy, from *Settings → Ultra+ Manager*.


Options that the selected build can't act on are greyed out with the reason. The
check only greys what it can prove: it scans each build's launcher script, and a
variable it has never seen in *any* build stays enabled, because unknown isn't
the same as unsupported. (`DXVK_NVAPI_VKREFLEX` is read by dxvk-nvapi itself and
appears in no proton script, yet works fine.)

Saving needs Steam closed - it rewrites `localconfig.vdf` on exit and would
clobber the change. Confirm and it closes Steam cleanly, saves, and offers a
one-click restart.

### DLSS tab

![Every DLSS DLL in the game with its version, ready to swap or roll back](assets/screenshots/dlss.png)

Every DLSS DLL in the game with its version. Swap in a newer one from your
library, back up the original, roll back whenever. Requires an NVIDIA driver.

### ReShade tab

Scans the game's own exe for its graphics API (reading the PE import table -
same technique as [RankFTW/RHI](https://github.com/RankFTW/RHI)'s detector)
and bitness, then offers to install ReShade under the matching proxy DLL name:
`d3d9.dll` for D3D9, `dxgi.dll` for D3D10/11/12, `opengl32.dll` for OpenGL.
Native Vulkan and D3D8 aren't supported yet. Both the API and the exe it
scanned can be overridden manually if it guesses wrong.

The engine comes straight from reshade.me's own installer - it's a zip with a
stub exe prepended, so no extra tooling is needed to pull `ReShade64.dll` /
`ReShade32.dll` out of it. First install also seeds a shared shader folder
(`~/.local/share/proton-command-center/reshade/shaders`) with ReShade's own
default selection (Standard effects + SweetFX), pointed at from every game's
`ReShade.ini` so effects work immediately.

Installing never overwrites a proxy-named DLL Command Center didn't put there
itself - a `dxgi.dll` already in the game folder could belong to OptiScaler or
another mod, and clobbering it silently would break that instead. Move it
aside first if you're sure it's safe to replace.

ReShade only loads once the game process actually reads its proxy DLL from
disk instead of Proton's own - add `WINEDLLOVERRIDES=dxgi=n,b` (or whichever
DLL was installed) to the game's launch options; the ReShade tab has a button
that adds this for you once it's installed.

### Shader cache tab

![Shader cache sizes, thread count, and Steam's own background processing](assets/screenshots/shaders.png)

The headline number is what NVIDIA's shader cache is using against its ceiling,
plus what Steam is holding separately - usually far more.


**Thread count.** Steam uses only a fraction of your cores for its shader pass,
which is why that screen can crawl on an idle machine. The override lives in
`steam_dev.cfg`, a file Steam never creates and exposes nowhere in its UI. One
click sets it to every core but two. Needs a full Steam restart.

**Disk cache.** A global toggle writes NVIDIA's shader-disk-cache variables to
`/etc/environment`, consolidating compiled shaders somewhere that survives
cleanup, with a 10/30/50/100 GB ceiling. The ceiling is a limit, not a
reservation - nothing is allocated until shaders accumulate. Needs a re-login:
environment variables only reach processes started afterwards.

Command Center does **not** try to pre-empt Steam's "Processing Vulkan shaders"
pass. Steam replays its own pipeline caches and gates on its own ledger, so
nothing outside Steam can make it skip that work. If it bothers you, the real
switch is Steam → Settings → Downloads → Shader Pre-Caching, at the cost of
shaders compiling during play instead.

### Benchmark tab

Save the benchmark launch options, play, change something, play again. Logs are
split at the marked point and compared on avg FPS, 1% and 0.1% lows, and stutter
count, with frametime graphs.

### ProtonDB check

Fetches the game's community rating and shows a tier badge. Persists across
restarts.

### System panel

Names your CPU and GPU properly, and configures the MangoHud overlay: presets,
which GPU to pin, logging.

### Controller

| Input | Action |
|---|---|
| D-pad / left stick | Move |
| **Right stick** | Scroll long lists |
| **A** | Select |
| **B** | Back |
| **X** | Play or Install |
| **Y** | Search |
| **LB / RB** | Cycle tabs |
| **Start** | Settings |
| **Select** | Fullscreen |

Navigation is spatial, so down from a card lands on the card below rather than
wandering diagonally. The right stick scrolls whatever surface is open - the
settings panel, a drawer tab, the hardware readout. Cards are a single stop. Confirmations are drawn in-page,
because a native browser dialog freezes the pad polling loop and can't be
answered.

Steam's own install dialog is outside all of this: it's a separate window, and
a browser only receives gamepad input while it has focus. To drive Steam's UI
with a pad, use Steam's Desktop Layout (Steam → Settings → Controller), which
maps the controller to mouse and keyboard system-wide.

Fullscreen is also on ⛶ or **F11**; **Esc** always leaves. *Settings → Display →
Open fullscreen* makes it automatic - it fires on your first click or button
press, since browsers forbid a page going fullscreen on load.

### Settings

**Backup & restore** - your DLL library, launch settings, MangoHud config, API
keys and ProtonDB ratings into one timestamped `.tar.gz`. Art cache excluded; it
re-fetches itself.

**Proton versions** - recent GE-Proton releases with an up-to-date status and
one-click install into `compatibilitytools.d`. On CachyOS, `proton-cachyos` from
pacman is the better path since it's system-optimised.

---

## Updating

Re-run `./install.sh`, or `yay -S proton-command-center`. A red banner means the
backend is older than the frontend: `systemctl --user restart
proton-command-center` and refresh.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Missing artwork | Gear → Clear art cache & re-fetch |
| Backend not responding | `systemctl --user restart proton-command-center` |
| "Steam is running" on save | Expected - confirm and it closes Steam cleanly |
| Shader toggle did nothing | Needs a re-login; `/etc/environment` only affects new sessions |
| Thread count did nothing | Needs a full Steam restart, not just a reload |

## Uninstall

```bash
./uninstall.sh           # asks before deleting user data
./uninstall.sh --purge   # everything, including user data
```

Restore any swapped DLLs before purging.

## Development

```bash
python3 tests/test_pcc.py
```

Tests build a mock Steam install in a temp dir, so they run anywhere. A second
instance won't disturb your service: `PCC_PORT=8687 python3 pcc.py`. Games
launched via Play run in their own systemd scope, so restarting the backend
never kills a running game.

## Credits

Ultra+, Ultra+ Manager, and the UE4SS-based mods they cover are made by the
Ultra+ team at [theultraplace.com](https://theultraplace.com) and distributed
via [Nexus Mods](https://www.nexusmods.com/site/mods/1586). Command Center
doesn't install or modify any of their files itself - it only detects what
Ultra+ Manager already put in a game's folder and launches the app; all
Ultra+ branding and assets (including the logo shown on tagged library cards)
belong to their respective owners.

MIT. Copyright (c) 2026 Marc Gibb.
