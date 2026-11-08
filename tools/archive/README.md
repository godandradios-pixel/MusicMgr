# Retired one-off scripts

Each of these repaired the library once, for a specific problem that has since been fixed in the app itself. They're kept for reference: the history of what was repaired and how. They aren't maintained or tested any more, so don't run one without reading it first and backing up `library.db`.

| Script | What it fixed | When |
|---|---|---|
| `fix_weekly_import.py` | A Hot 100 weekly CSV imported into the Pop Year End chart | 2026-09-30 |
| `organize_charts_by_source.py` | Re-filed the Charts tree as Source > Genre > Type > date | 2026-09-30 |
| `restore_playlists_from_backup.py` | Playlists and folders removed by a USB sync | 2026-09-30 |
| `restore_stations_from_backup.py` | AM/FM stations removed by a USB sync | 2026-10-14 |
| `fix_ghost_chart_tracks.py` | Chart positions held by file-less "ghost" songs; titles that lost their numbers | 2026-10-19 |
| `unlink_loose_chart_matches.py` | Chart positions matched to the wrong song by the old matcher | 2026-10-25 |

Today the app covers these itself: daily library backups with Restore in Settings, one-way USB sync that asks before removing playlist folders, the Library Health check, and chart re-matching that checks the artist.

Run from the repo root, for example `python tools/archive/fix_weekly_import.py`.
