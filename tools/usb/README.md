# USB drive scripts

These scripts live in `E:\MusicMgr` on the MusicMgr USB drive, next to `MusicMgr.exe`. They're kept here so they're versioned. After you change one, copy it back to the drive.

| Script | Run it when | What it does |
|---|---|---|
| `Install-MusicMgr.bat` | Setting up MusicMgr on a new or rebuilt Windows PC with your whole library | Copies `MusicMgr.exe`, the newest library snapshot (`data*` folder with `library.db`), covers and artist photos. Optionally copies Music, MusicFLAC, Videos and Movies to D:. It also makes a desktop shortcut and can set MusicMgr to start at sign-in. |
| `Install-MusicMgr-Blank.bat` | Setting up MusicMgr with an empty library | Same install steps, but starts with a new, empty `data` folder. |
| `Copy-USB-to-D.bat` | Bringing an existing PC up to date from the drive | Asks before replacing `D:\MusicMgr\data` with the newest USB snapshot (snapshot and sync history together). Then copies covers, artist photos and media E: to D: (add/update only). |

Rules all three follow:

- They refuse to run while MusicMgr is open.
- They never delete anything on the PC. A replaced `data` folder is renamed `data-before-install-<time>` or `data-before-usb-copy-<time>`.
- They don't change anything on the USB, apart from `Copy-USB-to-D.bat` writing its log next to itself.
- Afterwards, start MusicMgr and run **Settings › USB sync › Check USB now** to pick up anything newer on the drive.

Retired: `E:\Sync-USB-to-D.ps1` (mirrored the whole USB `MusicMgr` folder onto D:, including the drive's `sync` folder) was removed on 2026-10-26. Don't bring it back.
