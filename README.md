# Ripped Ripper

A companion for DJs who play from **Traktor Pro**. Search for a song (or paste a YouTube link, or an Apple Music / Spotify playlist), click **Rip**, and a tagged 320 kbps MP3 with its cover art lands in the folder Traktor already browses, numbered in the order you queued it. While you play, the Ripper shows what is on your decks, and a small **RIPPER** button sits on top of Traktor's own window so you can search and rip without leaving it.

- **Library** of every rip (never downloaded twice), plus a numbered folder for tonight's set and one folder per playlist.
- **Search** YouTube Music, or paste a YouTube / YouTube Music link.
- **Playlists**: paste an Apple Music playlist link, or a public Spotify one.
- **Traktor sync** (optional): the Ripper sees which track is on which deck and moves it from the queue to "Played tonight".
- **In Traktor's window**: a RIPPER button with a drop-down search-and-rip panel, and a "RIPPED" pop-up when a song is ready.

## Install (Windows 10 / 11)

You need **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/): on the first installer screen tick *Add python.exe to PATH*, and leave *tcl/tk and IDLE* ticked. You also need an internet connection. Everything else is installed for you.

1. **Get the files**: on GitHub press *Code > Download ZIP* and unzip it somewhere simple, for example `C:\RippedRipper` (not inside Program Files). Or `git clone https://github.com/jhugon1/Universal-Ripper.git`.
2. **Double-click `Setup.bat`.** It creates a private Python environment, installs the packages, installs **ffmpeg** with winget if you do not have it (it asks first), puts a **Ripped Ripper** icon on your Desktop, asks whether the Ripper should **start by itself when Traktor opens** (recommended), and finishes with a checklist. It takes one or two minutes, and you can run it again whenever you like.
3. **Double-click the Ripped Ripper icon.** The Ripper opens in its own window.

> If Windows says *"Windows protected your PC"* the first time (files from a downloaded ZIP get this), choose *More info > Run anyway*.

Closing the small minimised window that appears in your taskbar quits the Ripper (and the RIPPER button). Starting it a second time while it runs just brings its window back.

### Starts with Traktor

If you said yes to auto-start, you do not need the icon at all: when Traktor opens, the Ripper starts quietly in the background (the RIPPER button appears on Traktor's window, and the pop-up works). The big window stays closed until you click RIPPER > *Open full Ripper*. A little after Traktor closes, the Ripper quits by itself, and it never does that in the middle of a download. If you close the Ripper yourself while Traktor is open, it stays closed until Traktor is opened again. A Ripper you started with the icon is never closed for you.

A tiny helper (`traktor_watcher.py`, no window, almost no CPU) does the watching; it is started from your Windows Startup folder. Turn it off or on any time with `Turn off auto-start.bat` / `Turn on auto-start.bat`.

## Using it with Traktor

The Ripper works on its own, but with Traktor you get the deck sync, the RIPPER button and the pop-up.

1. **Close Traktor**, then double-click `traktor_mod\Install Traktor Mod.bat` and approve the Windows prompt. It adds two lines to Traktor's `D2.qml` mapping (your original is saved in `%USERPROFILE%\ripper-traktor-backup`) and copies five small files next to it. These are the MIT-licensed [traktor-api-client](https://github.com/ErikMinekus/traktor-api-client) by Erik Minekus.
2. In Traktor: *Preferences > Controller Manager > Add...* and add the device **Kontrol D2** (you do not need the hardware).
3. Make sure Traktor's *Music Folders* include your Music folder. Rips go to `Music\Traktor\...` (see below), so tonight's set appears in Traktor's browser; select the folder again in Traktor to refresh it after adding songs.

A Traktor update overwrites the mod: just run the installer again. `traktor_mod\Remove Traktor Mod.bat` takes it out.

## Where things go

| What | Folder (default) |
| --- | --- |
| Every rip, tagged with its cover | `Music\Traktor\DJ_Search_Rips` |
| Tonight's set, numbered `001 Artist - Title.mp3` (hard links, no extra disk space) | `Music\Traktor\Ripper Queue\<date>` |
| Playlist rips, in playlist order | `Music\Traktor\Playlist Rips\<Playlist> (<length>)` |
| Queue and settings database | `queue.db` in the install folder |
| Waveform cache, button log, icon | `%LOCALAPPDATA%\RippedRipper` |

Change any of them with the environment variables `RIPPER_LIBRARY_DIR`, `RIPPER_QUEUE_DIR`, `RIPPER_PLAYLIST_DIR` and `RIPPER_DB`. The page also has **+ Also save to folder**, which links every rip into a second folder you pick.

## Troubleshooting

- **"What is wrong?"** Run `Setup.bat` again: it ends with a checklist, and `.venv\Scripts\python run.py --check` prints the same one. Lines marked `[!!]` are the problems.
- **ffmpeg not found**: install it from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) (the *essentials* build), add its `bin` folder to PATH, and run Setup again.
- **The Ripper did not start with Traktor**: run `Turn on auto-start.bat` once, or sign out and in. `%LOCALAPPDATA%\RippedRipper\watcher.log` says what the watcher saw.
- **The RIPPER button is missing**: it only shows while Traktor's window is open and not minimised. If it ever misbehaves, `%LOCALAPPDATA%\RippedRipper\rip_button.log` says why.
- **Spotify**: public playlists work without any setup (the first 100 tracks). Private playlists need your own free Spotify developer app: the page walks you through it.
- **Updating**: `git pull` (or download the ZIP again), then run `Setup.bat` once more.
- **Uninstalling**: run `Remove Traktor Mod.bat` if you installed the mod, run `Turn off auto-start.bat`, then delete the Ripper folder and the Desktop icon. Your music stays where it is.

## For developers

```
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q          # offline tests, no Traktor needed
.venv\Scripts\python run.py --dev          # separate dev data, reloads on every file change
.venv\Scripts\python tools\fake_traktor.py --list
```

## Credits

John Tamayo · Tanner Hochberg. Traktor deck data via [traktor-api-client](https://github.com/ErikMinekus/traktor-api-client) (MIT, Erik Minekus).

---
*Built for DJs who want to spend more time mixing and less time searching.*
