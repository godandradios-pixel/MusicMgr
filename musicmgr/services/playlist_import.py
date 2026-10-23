"""Import a playlist from a song list - a CSV or Excel (.xlsx) file.

Goes straight from the file to a manual playlist (no detour through a
chart) and reports the songs it couldn't find.

Columns are found the same forgiving way chart CSVs are (charts.py's
aliases): artist/performer/act, title/song/track, and an optional
rank/position/no that sets the order. Rows without one keep file order.

Matching is the chart matcher's (charts._candidate_tracks +
matching.best_match), album copies preferred over Billboard/Hot Country/
Oldies copies the same way (duplicates.album_copy_for), so a song matches
here exactly when it would have matched on the Charts page.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

from sqlalchemy.orm import Session

from . import playlists as pl_svc
from .charts import _candidate_tracks, _int_or_none, _map_columns, _read_csv_text
from .matching import best_match, normalize

SUPPORTED_SUFFIXES = (".csv", ".xlsx")


@dataclass
class SongRow:
    artist: str
    title: str
    rank: Optional[int] = None


@dataclass
class PlaylistImportResult:
    playlist_id: Optional[int] = None
    name: str = ""
    rows: int = 0
    added: int = 0
    #: (artist, title) the library has no match for, in list order
    unmatched: list[tuple[str, str]] = field(default_factory=list)
    #: (artist, title) that matched a song already added from this list
    repeats: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.errors and self.playlist_id is None:
            return "Nothing imported: " + "; ".join(self.errors)
        text = f"“{self.name}”: {self.added} of {self.rows} songs added"
        if self.unmatched:
            text += f"; {len(self.unmatched)} not found in your library"
        if self.repeats:
            text += f"; {len(self.repeats)} repeated in the list, added once"
        return text + "."


def name_from_path(path: Path | str) -> str:
    """"KLOVE_Eras.xlsx" -> "KLOVE Eras" - underscores become spaces, but
    the capitals stay as typed (no .title(), which would make it "Klove")."""
    return re.sub(r"\s+", " ", Path(path).stem.replace("_", " ")).strip() or "Imported playlist"


# --------------------------------------------------------------------------
# reading the file
# --------------------------------------------------------------------------

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iterfind(".//m:t", _NS))
    v = cell.find("m:v", _NS)
    if v is None or v.text is None:
        return ""
    if kind == "s":
        return shared[int(v.text)]
    text = v.text
    if kind is None and re.fullmatch(r"-?\d+\.0+", text):
        text = text.split(".")[0]  # 1.0 -> "1"
    return text


def _read_xlsx_rows(path: Path) -> list[list[str]]:
    """The first worksheet's cells as text. Standard library only - the
    app doesn't ship openpyxl, and a song list only needs the values."""
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.iterfind("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iterfind(".//m:t", _NS)))
        sheet = "xl/worksheets/sheet1.xml"
        try:  # the workbook's first sheet, which isn't always sheet1.xml
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            first = wb.find("m:sheets/m:sheet", _NS)
            rid = first.get(f"{{{_REL_NS}}}id") if first is not None else None
            rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rels:
                if rel.get("Id") == rid:
                    target = rel.get("Target", "").lstrip("/")
                    sheet = target if target.startswith("xl/") else "xl/" + target
        except (KeyError, ET.ParseError):
            pass
        root = ET.fromstring(zf.read(sheet))
    rows: list[list[str]] = []
    for row in root.iterfind(".//m:sheetData/m:row", _NS):
        values: list[str] = []
        for cell in row.iterfind("m:c", _NS):
            ref = cell.get("r")
            idx = _col_index(ref) if ref else len(values)
            while len(values) < idx:
                values.append("")
            values.append(_cell_text(cell, shared).strip())
        if any(values):
            rows.append(values)
    return rows


def read_song_rows(path: Path | str) -> tuple[list[SongRow], list[str]]:
    """(songs, errors) from a CSV or .xlsx file, in playlist order."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        try:
            table = _read_xlsx_rows(path)
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            return [], [f"couldn't read the spreadsheet ({exc})"]
    elif suffix == ".csv":
        table = list(csv.reader(io.StringIO(_read_csv_text(path))))
        table = [r for r in table if any(c.strip() for c in r)]
    else:
        return [], [f"{path.suffix or 'this'} files aren't supported - use .csv or .xlsx"]
    if not table:
        return [], ["the file is empty"]

    header = table[0]
    cols = _map_columns(header)
    missing = [k for k in ("artist", "title") if k not in cols]
    if missing:
        return [], [
            f"missing column(s): {', '.join(missing)}. "
            f"Found headers: {', '.join(h for h in header if h)}"
        ]
    pos = {key: header.index(cols[key]) for key in ("artist", "title", "rank") if key in cols}

    def get(row: list[str], key: str) -> str:
        i = pos.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    songs = []
    for row in table[1:]:
        title = get(row, "title")
        if not title:
            continue
        songs.append(SongRow(get(row, "artist"), title, _int_or_none(get(row, "rank"))))
    if songs and all(s.rank is not None for s in songs):
        songs.sort(key=lambda s: s.rank)  # stable: equal ranks keep file order
    return songs, []


# --------------------------------------------------------------------------
# matching and building the playlist
# --------------------------------------------------------------------------


def match_song(session: Session, artist: str, title: str, threshold: float = 0.72) -> Optional[int]:
    """The library track for this artist + title, or None - the same answer
    the Charts page's matcher gives (charts.match_entry)."""
    hit = best_match(title, artist, _candidate_tracks(session, normalize(title)), threshold)
    if not hit:
        return None
    from .duplicates import album_copy_for

    return album_copy_for(session, hit[0]) or hit[0]


def import_playlist(
    session: Session,
    path: Path | str,
    name: Optional[str] = None,
    folder_id: Optional[int] = None,
    threshold: float = 0.72,
) -> PlaylistImportResult:
    """Read `path`, match each song, and make a new manual playlist of the
    matches in `folder_id` (None = top level). Nothing is made when the
    file can't be read or none of its songs are in the library."""
    result = PlaylistImportResult(name=(name or "").strip() or name_from_path(path))
    songs, errors = read_song_rows(path)
    result.errors.extend(errors)
    result.rows = len(songs)
    if errors:
        return result
    if not songs:
        result.errors.append("no songs found in the file")
        return result

    track_ids: list[int] = []
    seen: set[int] = set()
    for song in songs:
        track_id = match_song(session, song.artist, song.title, threshold)
        if track_id is None:
            result.unmatched.append((song.artist, song.title))
        elif track_id in seen:
            result.repeats.append((song.artist, song.title))
        else:
            seen.add(track_id)
            track_ids.append(track_id)
    if not track_ids:
        result.errors.append("none of the songs are in your library")
        return result

    playlist = pl_svc.create_playlist(
        session, result.name, description=f"Imported from {Path(path).name}", folder_id=folder_id
    )
    for position, track_id in enumerate(track_ids):
        session.add(pl_svc.PlaylistItem(playlist_id=playlist.id, track_id=track_id, position=position))
    session.flush()
    result.playlist_id = playlist.id
    result.added = len(track_ids)
    return result
