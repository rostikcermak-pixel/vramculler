# vramculler

> # 🚧⚠️ WORK IN PROGRESS — EXPERIMENTAL ⚠️🚧
>
> **This is early, experimental, work-in-progress software. USE AT YOUR OWN RISK.**
>
> It modifies game config files on your system. It makes timestamped backups and
> has a `--restore`, but **things can still break.** Always run `--report-only`
> and `--dry-run` first, and keep your own backups.
>
> **The author is NOT responsible or liable for anything that breaks** — corrupted
> configs, broken game installs, lost settings, VAC/anti-cheat reactions, lost
> time, or anything else. No warranty of any kind (see the MIT
> [LICENSE](LICENSE)). If you run it, you accept full responsibility for the
> outcome.

A cross-platform Python CLI that **reduces VRAM pressure** for installed Steam
games by applying known-safe, per-engine config tweaks. It edits human-readable
game config files (Unreal Engine texture-streaming pool, Source texture quality)
— nothing else.

> **Not a texture compressor.** vramculler does **not** touch game binaries,
> archives, shaders, or proprietary formats, and it is **completely unrelated to
> NVIDIA RTX Neural Texture Compression (NTC)** or any other compression tech. It
> only flips documented, in-engine config values you could set by hand.

---

## ⚠️ Honest claims: VRAM pressure vs. FPS

vramculler lowers how much VRAM a game tries to use. That is **not** a universal
"FPS boost" button.

- **If a game was exceeding your VRAM** and spilling textures into system RAM
  (the classic low-VRAM symptom: traversal stutter, hitching, texture pop-in,
  frametime spikes), reducing the texture budget keeps it inside VRAM and can
  **remove that stutter and recover FPS**.
- **If a game already fits in your VRAM**, lowering the budget changes
  **nothing** you can feel — same FPS, possibly slightly less texture detail.

So the honest framing is: **vramculler reduces VRAM pressure.** Any FPS gain is a
*consequence* of stopping VRAM spillover, and only happens when spillover was
actually occurring. The tool says this in its own output too.

---

## Supported engines

| Engine | Detection | What it changes | Safe? |
| --- | --- | --- | --- |
| **Unreal Engine 4 / 5** | `Engine/` dir + a project dir with `Binaries/` + `Content/` | Writes `r.Streaming.PoolSize` and `r.Streaming.LimitPoolSizeToVRAM=1` to `[SystemSettings]` in the per-user `Engine.ini` | ✅ Documented UE texture-streaming cvars; `LimitPoolSizeToVRAM` makes the engine clamp to real VRAM |
| **Source (CS:GO, Garry's Mod, …)** | `gameinfo.txt` / `hl2*` | Sets `mat_picmip` (texture quality) in `autoexec.cfg` | ✅ In-game-equivalent value only; nothing VAC-relevant |
| **Source 2 (CS2, Dota 2)** | `game/<mod>/gameinfo.gi` | **No file change.** Prints the recommended in-game setting (Texture Quality / Streaming = Low) | ✅ No cfg knob exists that is safe to script |
| **Unity** | `*_Data/globalgamemanagers` / `UnityPlayer.dll` | **Reports** "no safe config tweak available" — never guesses keys | ✅ by omission |
| Anything else / proprietary | — | Skipped, reported as unsupported | ✅ by omission |

**Design rule:** if a knob isn't *documented and safe* for an engine,
vramculler **skips and reports** it. It never invents config keys.

---

## Profiles

All tweak values live in one editable `PROFILES` dict at the top of
`vramculler.py`, tuned for a **4 GB card**:

| Profile | UE `r.Streaming.PoolSize` | `LimitPoolSizeToVRAM` | Source `mat_picmip` |
| --- | --- | --- | --- |
| `conservative` | 1500 MB | on | 1 |
| `balanced` *(default)* | 1200 MB | on | 1 |
| `aggressive` | 800 MB | on | 2 |

Edit the dict to retune for a different card.

---

## Install

Requires **Python 3.10+**. Pure standard library; `rich` is an optional extra
for prettier tables (it degrades gracefully to ANSI when absent).

```bash
git clone https://github.com/rostikcermak-pixel/vramculler.git
cd vramculler
# optional, nicer output:
pip install rich
```

No admin/root needed — vramculler only writes user-owned files. If a target
file isn't writable it is skipped and reported, never escalated.

---

## Usage

```text
vramculler.py [--steam-path PATH] [--profile {conservative,balanced,aggressive}]
              [--report-only] [--dry-run] [--restore]
              [--no-rich] [--quiet-banner]
```

vramculler probes the usual Steam locations automatically (native packages,
Flatpak `~/.var/app/com.valvesoftware.Steam`, `~/.steam`, `~/.local/share/Steam`
on Linux; `Program Files (x86)\Steam` on Windows). Use `--steam-path` to point
at a specific library root; the path is saved for next time. Multiple libraries
declared in `libraryfolders.vdf` are all scanned.

### 1. Audit first (no changes)

```bash
# Linux
python3 vramculler.py --report-only

# Windows (PowerShell)
python vramculler.py --report-only
```

Prints every installed game, its OS/runtime (native vs Proton), the detected
engine, and what *would* be tweaked — formatted for a clean screenshot.

### 2. Preview the exact edits (still no changes)

```bash
python3 vramculler.py --dry-run --profile aggressive
```

### 3. Apply (with automatic backups + verification)

```bash
# Linux, explicit library path
python3 vramculler.py --steam-path ~/.local/share/Steam

# Windows
python vramculler.py --steam-path "D:\SteamLibrary"
```

Before editing any existing file, vramculler copies it to
`<file>.bak.<timestamp>` (the first/original backup is never overwritten on
re-runs). After writing, it re-reads the file and verifies the keys are present,
reporting pass/fail per game. Re-running is **idempotent** — already-applied
values are detected and left alone, never stacked.

### 4. Restore everything

```bash
python3 vramculler.py --restore
```

`--restore` reverts **all** changes: files vramculler *modified* are restored
from their earliest (pristine) backup, and files it *created from scratch* are
deleted. Combine with `--dry-run` to preview the restore.

---

## Linux + Proton

Most Windows games on Linux run through Proton. Their Unreal/Source configs live
**inside the Proton prefix**, not in your Linux home:

```
steamapps/compatdata/<appid>/pfx/drive_c/users/steamuser/AppData/Local/<Game>/Saved/Config/Windows/Engine.ini
```

vramculler detects Proton games (presence of `compatdata/<appid>`) and edits the
config at the same path a Windows install would use, inside the prefix. Native
Linux builds use `~/.config`, `~/.local/share`, or the install dir instead. The
verbose default output tells you exactly which path was touched for each game.

Distro only affects where Steam itself lives (Arch/CachyOS, Fedora, Debian/Ubuntu
native packages vs Flatpak) — vramculler probes the known candidates and lets you
override with `--steam-path`.

---

## Verbose output

Verbose debug output is **on by default**. Per game it prints:

- OS and runtime (native vs Proton, plus the prefix path)
- the engine detected
- the exact config file path touched
- each key as `before -> after`
- skip reasons when nothing safe applies
- a final synthwave summary table:
  `game | os/runtime | engine | action | est. effect | backup path`

---

## What vramculler will **not** do

- Touch binaries, shaders, archives, or proprietary save formats
- Compress textures (it is **not** NTC and not affiliated with NVIDIA)
- Set any cvar/launch option that could raise VAC concerns
- Guess undocumented engine keys
- Require or use admin/root, or write to files you don't own

---

## License

MIT — see [LICENSE](LICENSE).
