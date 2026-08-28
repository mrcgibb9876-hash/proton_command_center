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
| **Optional** | An NVIDIA driver for DLSS features |

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
offered, and new releases show up on their own. The main toggle list sticks to
what most people actually reach for - DXVK, HDR, native Wayland, MangoHud,
Reflex, the NGX/DLSS auto-updater and auto-upgrade, VKD3D's experimental
descriptor-heap path - plus an Ultra+ mod loader toggle
(`WINEDLLOVERRIDES=dwmapi=n,b`) for UE4SS-based mods from
[theultraplace.com](https://theultraplace.com) - it forces Proton to load
dwmapi.dll from disk instead of its own stub. Opening a game whose folder has
a `ue4ss/` directory shows what Ultra+ found there (the loader DLL, any
companion `.asi` fixes); as of 1.22.0 those files are installed by Command
Center itself, from the Mods tab.

Niche, hardware-specific, or troubleshooting-only options - GameMode
(`gamemoderun`), the 8GB-VRAM memory cap, D7VK, OptiScaler, the FSR4 upgrade
(AMD-only), and disabling esync - aren't in that list; type them straight
into "Extra env vars" if you need one (wrappers like `gamemoderun` belong
there too, not in "Extra arguments" - that box runs after `%command%`, this
one runs before it). Nothing already saved gets touched: a variable or
wrapper word in a game's existing launch options that the toggle list no
longer recognises round-trips through that same box instead of being
silently dropped.

A game with Ultra+ set up gets an **ULTRA+** badge on its library card - click
the logo to jump to its Mods tab.


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

The Launch tab's DLSS section covers the rest: Super Resolution mode and
render preset, Frame Generation (2x-6x - 5x/6x are DLSS 4.5's Dynamic Multi
Frame Generation, RTX 50-series only, and need a Proton/GE build with a
recent-enough dxvk-nvapi), and Ray Reconstruction, all via the
`DXVK_NVAPI_DRS_*` layer. Preset letters are read straight from the selected
Proton build's own compiled dxvk-nvapi, so anything it doesn't actually
support shows greyed out instead of silently doing nothing. "In-game overlay"
is NVIDIA's own on-screen indicator - it overlays the live DLSS SDK version,
resolution, and active mode right in the game (not something PCC draws
itself), a one-checkbox way to confirm what's actually running without
alt-tabbing out. Compact-vs-per-variable output and the save/apply-default
template live under "Advanced preset options" so a first-time user isn't
staring at raw NGX/DRS variable names before they've touched anything else.

### Mods tab

Installs, updates, and removes [Ultra+](https://theultraplace.com) mods
directly - no separate Ultra+ Manager app needed. If the game has a published
mod, pick a version and install; reinstalling over an existing copy updates it
while merging your `UltraPlusConfig.ini`/`keybinds.ini` edits into the new
shipped file, and any presets the mod ships (`preset_*.ini`) show up as
one-click apply. Remove deletes every file that install tracked and nothing
else.

Skip "I manage my own UE4SS" if you run UE4SS yourself already - it installs
only the mod's own files and Content `.pak`/`.ucas`/`.utoc` packages, not the
UE4SS runtime or loader DLL.

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

Ultra+ and the UE4SS-based mods it covers are made by the Ultra+ team at
[theultraplace.com](https://theultraplace.com). As of 1.22.0, Command Center
installs, updates, and removes these mods directly, fetching from the same
public game-data and mod catalog the official apps use - it no longer
requires the separate Ultra+ Manager application to do this. This is an
independent, unofficial integration; Command Center is not affiliated with
or endorsed by the Ultra+ team, and all Ultra+ branding, mod content, and
the game-data catalog (including the logo shown on tagged library cards)
remain the property of their respective owners.

MIT. Copyright (c) 2026 Marc Gibb.
