"""Library-state sync through the USB drive (2026-09-24, USB sync step 3).

Plan: claude/2026-09-23-usb-sync-plan.md §3. Steps 1-2 carry the *files*
(music, videos, movies, artwork). This carries what only lives in
library.db - never the database file itself:

- **plays** (play_events, plus play_count / skip_count / last_played_at)
- **ratings** (tracks.rating)
- **playlists** (manual and smart ones, with their folder path)
- **the Jukebox board** (cards, genre chips)

Everything is keyed by names and paths, never by database ids (ids differ
on every PC). A track's key is its path inside a synced folder, as the
USB sees it: `Music/AC-DC/Back in Black/01 Hells Bells.mp3` is the same
key whether the file is D:\\Music\\… on Windows or ~/Music/… on Mint.
Artists are keyed by name_key.

How the merge works - the same three-way idea as the file sync:

- The USB holds one merged state: `MusicMgr\\sync\\library-state.json.gz`.
- Each library keeps a copy of that state as of its own last sync (the
  *base*, `data\\sync\\library-state-base-<drive>.json.gz`).
- For every rating, playlist and the board: changed only here → ours wins;
  changed only on the USB → theirs wins; changed on both → the newer edit
  wins (playlists: updated_at; board: when this PC first saw the change),
  and the losing board is kept as a JSON file under `sync\\_deleted\\`.
  Something deleted on one side and untouched on the other is deleted.
- Plays are simply combined (a play is identified by track + start time).

The merged state is written back to the USB and applied here.
"""

from __future__ import annotations

import copy
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import (
    Artist,
    JukeboxSlot,
    MediaFile,
    PlayEvent,
    Playlist,
    PlaylistFolder,
    PlaylistItem,
    Setting,
    SyncPair,
    Track,
)

log = logging.getLogger(__name__)

STATE_NAME = "library-state.json.gz"
FORMAT = 1

#: settings keys
PREF_BOARD_SIG = "sync_jukebox_board_sig"
PREF_BOARD_CHANGED = "sync_jukebox_board_changed_at"
_GENRES_KEY = "jukebox_genres"
_NEXT_SLOT_KEY = "jukebox_next_slot_number"

#: the four things this step can sync, each switchable per PC
PLAYS, RATINGS, PLAYLISTS, JUKEBOX = "plays", "ratings", "playlists", "jukebox"
CATEGORIES = (PLAYS, RATINGS, PLAYLISTS, JUKEBOX)
PREF_PREFIX = "sync_state_"

#: a play counts towards play_count like the player's own rule of thumb
COUNTED_MS = 30_000

SYNCED_PLAYLIST_KINDS = (Playlist.KIND_MANUAL, Playlist.KIND_SMART)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def _parse(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _play_key(track_key: str, started_at: Optional[str]) -> str:
    # to the second: the same play read back from two PCs must match
    return f"{track_key}|{(started_at or '')[:19]}"


# --------------------------------------------------------------------------
# track keys
# --------------------------------------------------------------------------


class TrackIndex:
    """Maps this library's tracks to portable keys and back.

    Primary key: `<USB folder>/<path inside it>` via the media sync pairs,
    compared case-insensitively. Fallback (a file outside every pair):
    `~<artist>|<title>`."""

    def __init__(self, db: Session, pairs: list[SyncPair]) -> None:
        roots = sorted(
            ((os.path.normcase(str(Path(p.local_path))).rstrip("\\/"), p.usb_rel_path.strip("/"))
             for p in pairs if p.kind == "media"),
            key=lambda r: len(r[0]), reverse=True,
        )
        self.key_of: dict[int, str] = {}
        self.track_of: dict[str, int] = {}
        rows = db.execute(
            select(MediaFile.track_id, MediaFile.path, Track.artist_display, Track.title_key)
            .join(Track, Track.id == MediaFile.track_id)
        ).all()
        for track_id, path, artist, title_key in rows:
            key = self._path_key(path, roots) or self._fallback(artist, title_key)
            if track_id not in self.key_of:
                self.key_of[track_id] = key
            self.track_of.setdefault(key.casefold(), track_id)
            fb = self._fallback(artist, title_key)
            self.track_of.setdefault(fb.casefold(), track_id)

    @staticmethod
    def _path_key(path: str, roots) -> Optional[str]:
        norm = os.path.normcase(path)
        for local, usb in roots:
            if norm.startswith((local + "\\", local + "/")):
                rel = path[len(local) + 1:].replace("\\", "/")
                return f"{usb}/{rel}"
        return None

    @staticmethod
    def _fallback(artist: Optional[str], title_key: Optional[str]) -> str:
        return f"~{(artist or '').casefold()}|{title_key or ''}"

    def track(self, key: Optional[str]) -> Optional[int]:
        return self.track_of.get(key.casefold()) if key else None


# --------------------------------------------------------------------------
# reading this library into a state
# --------------------------------------------------------------------------


def _folder_path(folder: Optional[PlaylistFolder]) -> str:
    parts = []
    seen = set()
    while folder is not None and folder.id not in seen:
        seen.add(folder.id)
        parts.append(folder.name)
        folder = folder.parent
    return "/".join(reversed(parts))


def _board_value(db: Session, idx: TrackIndex) -> dict:
    cards = []
    for slot in db.scalars(select(JukeboxSlot).order_by(JukeboxSlot.slot_number)):
        cards.append({
            "slot": slot.slot_number,
            "artist": slot.artist.name if slot.artist else None,
            "artist_key": slot.artist.name_key if slot.artist else None,
            "a": idx.key_of.get(slot.side_a_track_id) if slot.side_a_track_id else None,
            "b": idx.key_of.get(slot.side_b_track_id) if slot.side_b_track_id else None,
            "genre": slot.genre,
        })
    genres_row = db.get(Setting, _GENRES_KEY)
    try:
        genres = json.loads(genres_row.value) if genres_row and genres_row.value else None
    except ValueError:
        genres = None
    return {"cards": cards, "genres": genres}


def _board_sig(board: dict) -> str:
    return hashlib.sha1(json.dumps(board, sort_keys=True).encode("utf-8")).hexdigest()


def read_local(db: Session, idx: TrackIndex) -> dict:
    """This library as a state (see the module docstring)."""
    ratings = {}
    for track_id, rating in db.execute(select(Track.id, Track.rating).where(Track.rating.is_not(None))):
        key = idx.key_of.get(track_id)
        if key:
            ratings[key] = rating

    playlists: dict[str, dict] = {}
    for pl in db.scalars(
        select(Playlist).where(Playlist.kind.in_(SYNCED_PLAYLIST_KINDS)).order_by(Playlist.id)
    ):
        name = pl.name
        n = 2
        while name in playlists:          # duplicate names stay distinct
            name = f"{pl.name} #{n}"
            n += 1
        playlists[name] = {
            "kind": pl.kind,
            "folder": _folder_path(pl.folder),
            "description": pl.description,
            "rules": pl.rules,
            "pinned": bool(pl.is_pinned),
            "tracks": [idx.key_of.get(i.track_id) for i in pl.items if idx.key_of.get(i.track_id)]
            if pl.kind == Playlist.KIND_MANUAL else [],
            "updated_at": _iso(pl.updated_at),
        }

    plays = []
    for ev in db.scalars(select(PlayEvent).order_by(PlayEvent.started_at)):
        key = idx.key_of.get(ev.track_id)
        if key:
            plays.append([key, _iso(ev.started_at), ev.ms_played, bool(ev.completed),
                          bool(ev.skipped), ev.source])

    board = _board_value(db, idx)
    sig = _board_sig(board)
    sig_row = db.get(Setting, PREF_BOARD_SIG)
    changed_row = db.get(Setting, PREF_BOARD_CHANGED)
    if sig_row is None:
        # never synced before: the newest card's own date is the best guess
        # at when this board last changed (not "now", which would make a
        # PC's first sync always win)
        newest = db.scalar(select(func.max(JukeboxSlot.created_at)))
        changed = _iso(newest) if newest else "1970-01-01T00:00:00+00:00"
        _set(db, PREF_BOARD_SIG, sig)
        _set(db, PREF_BOARD_CHANGED, changed)
    elif sig_row.value != sig:
        # first time this PC sees the board like this: that's when it changed
        changed = _iso(_now())
        _set(db, PREF_BOARD_SIG, sig)
        _set(db, PREF_BOARD_CHANGED, changed)
    else:
        changed = changed_row.value if changed_row else _iso(_now())
    board["changed_at"] = changed

    return {"ratings": ratings, "playlists": playlists, "plays": plays, "jukebox": board}


def _set(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.flush()


# --------------------------------------------------------------------------
# state files
# --------------------------------------------------------------------------


def empty_state() -> dict:
    return {"ratings": {}, "playlists": {}, "plays": [], "jukebox": None, "unresolved": []}


def load_state(path: Path) -> Optional[dict]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError) as exc:
        log.warning("can't read %s: %s", path, exc)
        return None
    state = empty_state()
    state.update({k: data.get(k, state[k]) for k in state})
    return state


def save_state(path: Path, state: dict, pc_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(state)
    if "unresolved" in data and not data["unresolved"]:
        data.pop("unresolved")
    data["format"] = FORMAT
    data["saved"] = _iso(_now())
    data["saved_by"] = pc_id
    tmp = path.with_name(path.name + ".mmsync-tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def usb_state_path(drive) -> Path:
    return drive.sync_dir / STATE_NAME


def base_state_path(drive) -> Path:
    return config.DATA_DIR / "sync" / f"library-state-base-{drive.drive_id}.json.gz"


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------


@dataclass
class MergeNotes:
    ratings_in: int = 0
    playlists_in: list[str] = field(default_factory=list)
    playlists_removed: list[str] = field(default_factory=list)
    plays_in: int = 0
    board_in: bool = False
    board_conflict: Optional[str] = None      # which side won
    lost_board: Optional[dict] = None
    unresolved_tracks: int = 0

    def summary(self) -> str:
        parts = []
        if self.plays_in:
            parts.append(f"{self.plays_in:,} play{'s' if self.plays_in != 1 else ''} added")
        if self.ratings_in:
            parts.append(f"{self.ratings_in:,} rating{'s' if self.ratings_in != 1 else ''} updated")
        if self.playlists_in:
            n = len(self.playlists_in)
            parts.append(f"{n} playlist{'s' if n != 1 else ''} updated")
        if self.playlists_removed:
            n = len(self.playlists_removed)
            parts.append(f"{n} playlist{'s' if n != 1 else ''} removed")
        if self.board_in:
            parts.append("Jukebox updated")
        if self.board_conflict:
            parts.append(f"Jukebox changed on both — kept {self.board_conflict}")
        return " · ".join(parts) if parts else "Library data already in sync"


_MISSING = object()


def _pick(l, u, b, usb_newer: bool = False) -> tuple[object, bool]:
    """Three-way choice for one item. Returns (value, came_from_usb).
    `_MISSING` means "doesn't exist". Changed only here -> ours; changed
    only on the USB -> theirs; changed on both -> an edit beats a delete,
    otherwise the USB's only if `usb_newer`."""
    if u == b or l == u:
        return l, False
    if l == b:
        return u, True
    if l is _MISSING:
        return u, True
    if u is _MISSING:
        return l, False
    return (u, True) if usb_newer else (l, False)


def _merge_dict(local: dict, usb: dict, base: dict, newer=None) -> tuple[dict, list[str]]:
    merged, taken = {}, []
    for key in set(local) | set(usb) | set(base):
        l = local.get(key, _MISSING)
        u = usb.get(key, _MISSING)
        b = base.get(key, _MISSING)
        usb_newer = bool(newer and l is not _MISSING and u is not _MISSING and newer(key))
        value, from_usb = _pick(l, u, b, usb_newer)
        if value is not _MISSING:
            merged[key] = value
            if from_usb:
                taken.append(key)
    return merged, taken


def _strip(value, drop: str):
    if not isinstance(value, dict):
        return value
    return {k: v for k, v in value.items() if k != drop}


def merge(local: dict, usb: dict, base: dict) -> tuple[dict, MergeNotes]:
    notes = MergeNotes()

    ratings, taken = _merge_dict(local["ratings"], usb["ratings"], base["ratings"])
    notes.ratings_in = len(taken)

    # playlists compare without updated_at; both changed -> newer updated_at
    lp = {k: _strip(v, "updated_at") for k, v in local["playlists"].items()}
    up = {k: _strip(v, "updated_at") for k, v in usb["playlists"].items()}
    bp = {k: _strip(v, "updated_at") for k, v in base["playlists"].items()}

    def pl_usb_newer(name: str) -> bool:
        lu = _parse(local["playlists"][name].get("updated_at"))
        uu = _parse(usb["playlists"][name].get("updated_at"))
        return bool(lu and uu and uu > lu)

    kept, taken_pl = _merge_dict(lp, up, bp, newer=pl_usb_newer)
    playlists = {
        name: (usb["playlists"][name] if name in taken_pl else local["playlists"][name])
        for name in kept
    }
    notes.playlists_in = sorted(taken_pl)
    notes.playlists_removed = sorted(n for n in local["playlists"] if n not in playlists)

    # plays: union
    seen = {_play_key(p[0], p[1]) for p in local["plays"]}
    plays = list(local["plays"])
    for p in usb["plays"]:
        k = _play_key(p[0], p[1])
        if k not in seen:
            seen.add(k)
            plays.append(p)
            notes.plays_in += 1
    plays.sort(key=lambda p: p[1] or "")

    # the Jukebox board is one unit; both changed -> the newer change wins
    # an empty board is "no board": a PC that never used the Jukebox must
    # not wipe out another PC's cards
    def _board(b):
        return b if b and b.get("cards") else None

    lb, ub, bb = _board(local["jukebox"]), _board(usb["jukebox"]), _board(base["jukebox"])
    sl, su, sb = (_strip(x, "changed_at") if x is not None else _MISSING for x in (lb, ub, bb))
    lt = _parse((lb or {}).get("changed_at"))
    ut = _parse((ub or {}).get("changed_at"))
    board_value, from_usb = _pick(sl, su, sb, usb_newer=bool(lt and ut and ut > lt))
    board = ub if from_usb else (lb if lb is not None else local["jukebox"])
    if from_usb and ub is not None:
        notes.board_in = True
    both_changed = sl != sb and su != sb and sl != su and sl is not _MISSING and su is not _MISSING
    if both_changed:
        notes.board_conflict = "the USB's (newer)" if from_usb else "this PC's (newer)"
        notes.lost_board = lb if from_usb else ub

    return {"ratings": ratings, "playlists": playlists, "plays": plays, "jukebox": board}, notes


# --------------------------------------------------------------------------
# applying a merged state to this library
# --------------------------------------------------------------------------


def _ensure_folder(db: Session, path: str) -> Optional[int]:
    parent_id = None
    for name in [p for p in (path or "").split("/") if p]:
        folder = db.scalar(
            select(PlaylistFolder).where(
                PlaylistFolder.name == name,
                PlaylistFolder.parent_id.is_(None) if parent_id is None else PlaylistFolder.parent_id == parent_id,
            )
        )
        if folder is None:
            folder = PlaylistFolder(name=name, parent_id=parent_id)
            db.add(folder)
            db.flush()
        parent_id = folder.id
    return parent_id


def apply_state(db: Session, idx: TrackIndex, local: dict, merged: dict, notes: MergeNotes,
                include: frozenset = frozenset()) -> None:
    # ratings (compared with the database itself: `local` may carry
    # ratings this PC couldn't place before - see carry_unresolved)
    for key, rating in (merged["ratings"].items() if RATINGS in include else ()):
        tid = idx.track(key)
        if tid is not None:
            track = db.get(Track, tid)
            if track is not None and track.rating != rating:
                track.rating = rating
    for key in set(local["ratings"]) - set(merged["ratings"]):
        tid = idx.track(key)
        if tid is not None:
            track = db.get(Track, tid)
            if track is not None:
                track.rating = None

    # playlists (by display name; "#2" suffixes map to later duplicates)
    by_name: dict[str, Playlist] = {}
    for pl in db.scalars(
        select(Playlist).where(Playlist.kind.in_(SYNCED_PLAYLIST_KINDS)).order_by(Playlist.id)
    ):
        name, n = pl.name, 2
        while name in by_name:
            name = f"{pl.name} #{n}"
            n += 1
        by_name[name] = pl
    for name in notes.playlists_removed:
        pl = by_name.get(name)
        if pl is not None:
            db.delete(pl)
    for name in notes.playlists_in:
        value = merged["playlists"][name]
        pl = by_name.get(name)
        base_name = name.split(" #")[0] if name not in by_name and " #" in name else name
        if pl is None:
            pl = Playlist(name=base_name, kind=value.get("kind") or Playlist.KIND_MANUAL)
            db.add(pl)
            db.flush()
        pl.kind = value.get("kind") or pl.kind
        pl.description = value.get("description")
        pl.rules = value.get("rules")
        pl.is_pinned = bool(value.get("pinned"))
        pl.folder_id = _ensure_folder(db, value.get("folder") or "")
        if pl.kind == Playlist.KIND_MANUAL:
            db.execute(delete(PlaylistItem).where(PlaylistItem.playlist_id == pl.id))
            pos = 0
            for key in value.get("tracks") or []:
                tid = idx.track(key)
                if tid is None:
                    notes.unresolved_tracks += 1
                    continue
                db.add(PlaylistItem(playlist_id=pl.id, track_id=tid, position=pos))
                pos += 1
    db.flush()

    # plays (only the ones this PC can place count as added)
    notes.plays_in = 0
    have = {_play_key(p[0], p[1]) for p in local["plays"]}
    for key, started, ms, completed, skipped, source in merged["plays"]:
        if _play_key(key, started) in have:
            continue
        tid = idx.track(key)
        if tid is None:
            continue
        when = _parse(started)
        db.add(PlayEvent(track_id=tid, started_at=when, ms_played=ms or 0,
                         completed=bool(completed), skipped=bool(skipped), source=source))
        notes.plays_in += 1
        track = db.get(Track, tid)
        if track is None:
            continue
        if completed or ((ms or 0) >= COUNTED_MS and not skipped):
            track.play_count = (track.play_count or 0) + 1
            last = track.last_played_at
            if last is not None and last.tzinfo is None:
                last = last.replace(tzinfo=dt.timezone.utc)
            if when is not None and (last is None or when > last):
                track.last_played_at = when
        elif skipped:
            track.skip_count = (track.skip_count or 0) + 1
    db.flush()

    # jukebox board
    if notes.board_in and merged["jukebox"] is not None:
        _apply_board(db, idx, merged["jukebox"], notes)


def _apply_board(db: Session, idx: TrackIndex, board: dict, notes: MergeNotes) -> None:
    artists = {a.name_key: a.id for a in db.scalars(select(Artist))}
    db.execute(delete(JukeboxSlot))
    db.flush()
    used: set[int] = set()
    for card in board.get("cards") or []:
        a = idx.track(card.get("a"))
        b = idx.track(card.get("b"))
        artist_id = artists.get(card.get("artist_key") or "")
        if artist_id is None:
            for tid in (a, b):
                track = db.get(Track, tid) if tid else None
                if track is not None and track.release is not None and track.release.album_artist_id:
                    artist_id = track.release.album_artist_id
                    break
        if artist_id is None or (a is None and b is None):
            notes.unresolved_tracks += 1
            continue
        slot = card.get("slot")
        if slot in used:
            slot = max(used) + 1
        used.add(slot)
        db.add(JukeboxSlot(slot_number=slot, artist_id=artist_id, side_a_track_id=a,
                           side_b_track_id=b, genre=card.get("genre") or "Rock"))
    if board.get("genres"):
        _set(db, _GENRES_KEY, json.dumps(board["genres"]))
    highest = max(used) if used else 0
    row = db.get(Setting, _NEXT_SLOT_KEY)
    if row is None or int(row.value or 0) <= highest:
        _set(db, _NEXT_SLOT_KEY, str(highest + 1))
    db.flush()
    # remember the board as seen now, so it doesn't count as a local edit
    applied = _board_value(db, idx)
    _set(db, PREF_BOARD_SIG, _board_sig(applied))
    _set(db, PREF_BOARD_CHANGED, board.get("changed_at") or _iso(_now()))


# --------------------------------------------------------------------------
# the whole step
# --------------------------------------------------------------------------


def _resolvable(idx: TrackIndex, key: Optional[str]) -> bool:
    return key is None or idx.track(key) is not None


def carry_unresolved(local: dict, base: dict, idx: TrackIndex) -> None:
    """Things in the last agreed state that refer to tracks this PC doesn't
    have - or didn't have at the last sync - must not look like local
    deletions, or this PC would push them away from every other PC. Put
    them back into `local` exactly as the base had them; edits on the other
    side still flow in, and `revive` later places the ones that can now be
    placed."""
    was_missing = set(base.get("unresolved") or [])

    def foreign(key: Optional[str]) -> bool:
        return key is not None and (key in was_missing or not _resolvable(idx, key))

    # ratings
    for key, value in base["ratings"].items():
        if key not in local["ratings"] and foreign(key):
            local["ratings"][key] = value

    # playlist track lists: re-insert foreign keys after the key they followed
    for name, pl in local["playlists"].items():
        old = base["playlists"].get(name)
        if not old or pl.get("kind") != Playlist.KIND_MANUAL:
            continue
        old_tracks = old.get("tracks") or []
        if not any(foreign(k) for k in old_tracks):
            continue
        mine = [k for k in (pl.get("tracks") or []) if not foreign(k)]
        lead: list[str] = []
        after: dict[str, list[str]] = {}
        prev = None
        for k in old_tracks:
            if not foreign(k):
                prev = k
            elif prev is None:
                lead.append(k)
            else:
                after.setdefault(prev, []).append(k)
        result = list(lead)
        for k in mine:
            result.append(k)
            result.extend(after.pop(k, []))
        for rest in after.values():      # its predecessor was removed here
            result.extend(rest)
        if result == old_tracks:
            pl["updated_at"] = old.get("updated_at", pl.get("updated_at"))
        pl["tracks"] = result

    # Jukebox board: cards (or sides) this PC couldn't place
    lb, bb = local.get("jukebox"), base.get("jukebox")
    if not lb or not bb or not bb.get("cards"):
        return
    by_slot = {c["slot"]: c for c in lb.get("cards") or []}
    for card in bb["cards"]:
        a_f, b_f = foreign(card.get("a")), foreign(card.get("b"))
        if not (a_f or b_f):
            continue
        mine = by_slot.get(card["slot"])
        if mine is None:
            lb["cards"].append(dict(card))
            continue
        if a_f and mine.get("a") is None:
            mine["a"] = card.get("a")
        if b_f and mine.get("b") is None:
            mine["b"] = card.get("b")
    lb["cards"].sort(key=lambda c: c["slot"])
    if local["jukebox"] is not None and lb is not local["jukebox"]:
        local["jukebox"] = lb


def _referenced_keys(state: dict) -> set[str]:
    keys = set(state["ratings"])
    for pl in state["playlists"].values():
        keys.update(pl.get("tracks") or [])
    for card in (state.get("jukebox") or {}).get("cards") or []:
        keys.update(k for k in (card.get("a"), card.get("b")) if k)
    return keys


def revive(merged: dict, base: dict, idx: TrackIndex, notes: "MergeNotes") -> None:
    """Keys that couldn't be placed at the last sync but can now (their
    files arrived since): re-apply the playlists and board that use them."""
    now_ok = {k for k in (base.get("unresolved") or []) if _resolvable(idx, k)}
    if not now_ok:
        return
    for name, pl in merged["playlists"].items():
        if name not in notes.playlists_in and now_ok & set(pl.get("tracks") or []):
            notes.playlists_in.append(name)
    board = merged.get("jukebox") or {}
    if any(now_ok & {c.get("a"), c.get("b")} for c in board.get("cards") or []):
        notes.board_in = True


def enabled_categories(db: Session) -> frozenset:
    """Which of plays/ratings/playlists/Jukebox this PC syncs (Settings →
    USB sync checkboxes, all on by default - 2026-09-24)."""
    return frozenset(
        cat for cat in CATEGORIES
        if (db.get(Setting, PREF_PREFIX + cat) is None
            or db.get(Setting, PREF_PREFIX + cat).value != "0")
    )


def set_category_enabled(db: Session, category: str, enabled: bool) -> None:
    _set(db, PREF_PREFIX + category, "1" if enabled else "0")


def sync_library_state(db: Session, drive, pc_id: str, pairs: Optional[list[SyncPair]] = None,
                       include: Optional[frozenset] = None) -> MergeNotes:
    """Merge this library with the drive's state, apply it here, and write
    the result back to the drive and to this library's base copy.

    `include` - the categories to sync (default: this PC's Settings
    choices). A category left out is passed through untouched: this PC
    neither sends its own nor takes the drive's, the drive keeps what the
    other PCs agreed on, and this PC's base for it stays where it was - so
    switching it back on later is an ordinary three-way merge."""
    if include is None:
        include = enabled_categories(db)
    if pairs is None:
        pairs = list(db.scalars(select(SyncPair).where(SyncPair.drive_id == drive.drive_id)))
    idx = TrackIndex(db, pairs)
    local = read_local(db, idx)
    usb = load_state(usb_state_path(drive)) or empty_state()
    base = load_state(base_state_path(drive)) or empty_state()
    original_base = dict(base)
    for cat in CATEGORIES:
        if cat not in include:
            # look exactly like the drive, so nothing moves either way
            local[cat] = copy.deepcopy(usb[cat])
            base[cat] = copy.deepcopy(usb[cat])
    carry_unresolved(local, base, idx)
    merged, notes = merge(local, usb, base)
    revive(merged, base, idx, notes)
    if JUKEBOX not in include:
        notes.board_in = False
    if PLAYLISTS not in include:
        notes.playlists_in = []
    apply_state(db, idx, local, merged, notes, include)
    if notes.lost_board is not None:
        from .usb_sync import usb_deleted_dir

        keep = usb_deleted_dir(drive) / dt.date.today().isoformat() / (
            f"jukebox-board-{dt.datetime.now():%H%M%S}.json"
        )
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text(json.dumps(notes.lost_board, indent=1), encoding="utf-8")
    db.flush()
    # the merged state is what the drive and this library now agree on
    # (plays for tracks this PC doesn't have stay in it for the others)
    save_state(usb_state_path(drive), merged, pc_id)
    base_copy = dict(merged)
    for cat in CATEGORIES:
        if cat not in include:
            base_copy[cat] = original_base[cat]
    base_copy["unresolved"] = sorted(k for k in _referenced_keys(merged) if not _resolvable(idx, k))
    save_state(base_state_path(drive), base_copy, pc_id)
    return notes


def count_plays(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(PlayEvent)) or 0
