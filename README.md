# MusicMgr

A personal music library and player for Windows 11 and Linux Mint, built with Python and Qt (PySide6). It's written for one collection, played on a few PCs at home, and kept in step through a USB drive.

## What it does

- **Library:** scans music and video folders into a local SQLite database (`library.db`), with album art, artist photos and biographies, lyrics, ratings, play history and a tag editor.
- **Playlists and charts:** playlists in folders, plus Billboard and other charts matched against the library, which can be saved as playlists.
- **Now Playing:** crossfade, gapless playback, volume levelling, and a turntable view for 78s.
- **Jukebox:** a jukebox board of two-sided cards.
- **Radio:** a multi-band antique radio for old-time radio shows, internet stations and SiriusXM channels.
- **Phone remote:** control a PC from a phone's browser on the home network, including spoken song requests.
- **USB sync:** keeps the library, playlists, play history, charts and radio between PCs through a USB drive.
- **Updates:** checks GitHub Releases for signed updates and installs them after asking.

## Running from source

Python 3.13 (what the release builds use).

```
python -m venv venv
venv\Scripts\activate          (Linux: source venv/bin/activate)
pip install -r requirements-dev.txt
python run.py
```

Options: `--maximized`, or `--fullscreen` for a dedicated touch screen. A command-line tool for scans, chart imports and stats is at `python -m musicmgr.cli --help`.

Tests: `python -m pytest`

## Building

- **Windows:** `.\build.ps1` builds `dist\MusicMgr.exe`.
- **Linux:** `./build.sh` builds `dist/MusicMgr`. The binary needs `sudo apt install libxcb-cursor0`.

Releases are built by the GitHub Action in `.github/workflows/release.yml` when a `v*` tag is pushed. They're then signed with `tools/sign_release.py`.

## Repository layout

| Folder | Contents |
|---|---|
| `musicmgr/` | The app: `services/` (library, player, sync, charts, radio), `ui/` (pages and widgets), `db/` (models) |
| `tests/` | The pytest suite |
| `tools/` | Release signing, library import and copy helpers, and the USB drive's setup scripts (`tools/usb/`) |
| `tools/archive/` | Retired one-off repair scripts, kept for reference |

## License

Copyright (c) 2026 James Stevens. All rights reserved. See [LICENSE](LICENSE).
