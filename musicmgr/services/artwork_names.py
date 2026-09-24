"""Name-based artwork files, MusicBee style (2026-09-24, USB sync step 2).

James: "Notice how MusicBee names the artist thumb picture the same name"
(E:\\MusicBee\\Artist Pictures\\Thumb\\Captain & Tennille.jpg). Plan:
claude/2026-09-23-usb-sync-plan.md §2.

Before this, image files were named after local database ids
(`artwork\\release_6357_8c79….jpg`, `artists\\1003_The_Weeknd.jpg`). Ids
differ between PCs, so the folders couldn't be shared. Now:

    artists\\Captain & Tennille.jpg
    artwork\\Captain & Tennille - Love Will Keep Us Together.jpg
    artwork\\Various Artists - Northbeam Singles.jpg

Every PC works the name out from its own data, so the artwork folders are
ordinary two-way USB sync pairs (services/usb_sync.py) and the name *is*
the link - no index file.

- `safe_image_name` builds the file name (MusicBee's character rules).
- `find_image` looks a name up ignoring case and punctuation, so
  `AC-DC.jpg`, `acdc.png` and `AC DC.jpg` all reach AC/DC.
- `release_cover_target` / `artist_image_target` say where an image for a
  release/artist belongs, reusing an existing spelling if there is one.
- `save_release_cover` / `save_artist_image` write them: the scanner never
  overwrites an existing picture; a download or a manual choice does.
- `relink` fills in cover_path / image_path from files that arrived by sync.
- `migrate` renames an existing id-named library once (with a DB backup).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import Artist, Release, Setting

log = logging.getLogger(__name__)

#: settings key: "2" once this data folder uses name-based artwork
PREF_NAMING = "artwork_naming"
NAMING_VERSION = "2"

#: folders inside artwork\ / artists\ that hold set-aside files - never
#: synced, never looked up
UNUSED_DIR = "_unused"
SUPERSEDED_DIR = "_superseded"

MAX_NAME = 150
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
#: lookup order when a name exists with more than one extension
_EXT_ORDER = (".jpg", ".png", ".jpeg", ".webp", ".bmp", ".gif")


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------


def safe_image_name(text: Optional[str]) -> str:
    """A file-name stem MusicBee would write for `text`: characters Windows
    forbids become a space (AC/DC -> "AC DC"), runs of spaces collapse,
    leading/trailing spaces and dots go, reserved device names get a "_",
    and anything over 150 characters is cut with a short stable hash so
    two long names never collide."""
    s = _ILLEGAL.sub(" ", text or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = "Unknown"
    if s.casefold() in _RESERVED:
        s += "_"
    if len(s) > MAX_NAME:
        digest = hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:6]
        s = s[: MAX_NAME - 8].rstrip(" .") + " ~" + digest
    return s


def lookup_key(stem: str) -> str:
    """Case-, accent- and punctuation-insensitive key for matching a file
    stem to a name: "AC DC", "ac/dc" and "AC-DC" all give "ac dc" (but
    "ACDC" doesn't). Deliberately gentler than matching.normalize (which drops
    "(Live)"/"(Deluxe)"): two different albums must never share a key."""
    s = unicodedata.normalize("NFKD", stem)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = re.sub(r"[\W_]+", " ", s)
    return s.strip()


def release_stem(release: Release) -> str:
    if release.is_compilation:
        artist = "Various Artists"
    else:
        artist = (
            release.artist_display
            or (release.album_artist.name if release.album_artist is not None else None)
            or "Unknown Artist"
        )
    return safe_image_name(f"{artist} - {release.title or 'Untitled'}")


def artist_stem(artist: Artist) -> str:
    return safe_image_name(artist.name)


# --------------------------------------------------------------------------
# folder index
# --------------------------------------------------------------------------


class _FolderIndex:
    """lookup_key(stem) -> file name, per folder, rebuilt when the folder's
    modified time changes (creating/renaming/deleting a file updates it)."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[int, dict[str, str]]] = {}

    def get(self, folder: Path) -> dict[str, str]:
        key = os.path.normcase(str(folder))
        try:
            mtime = os.stat(folder).st_mtime_ns
        except OSError:
            return {}
        hit = self._cache.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]
        index: dict[str, str] = {}
        ranks: dict[str, int] = {}
        try:
            entries = list(os.scandir(folder))
        except OSError:
            entries = []
        for entry in entries:
            if not entry.is_file():
                continue
            stem, ext = os.path.splitext(entry.name)
            ext = ext.lower()
            if ext not in config.IMAGE_EXTENSIONS:
                continue
            k = lookup_key(stem)
            rank = _EXT_ORDER.index(ext) if ext in _EXT_ORDER else len(_EXT_ORDER)
            if k not in index or rank < ranks[k]:
                index[k] = entry.name
                ranks[k] = rank
        self._cache[key] = (mtime, index)
        return index

    def forget(self, folder: Path) -> None:
        self._cache.pop(os.path.normcase(str(folder)), None)


_INDEX = _FolderIndex()


def find_image(folder: Path, stem: str) -> Optional[Path]:
    """The image in `folder` whose name matches `stem`, ignoring case and
    punctuation, or None."""
    name = _INDEX.get(Path(folder)).get(lookup_key(stem))
    return Path(folder) / name if name else None


def _target(folder: Path, stem: str, ext: str) -> Path:
    """Where an image for `stem` goes: the existing spelling if one exists
    (so ACE.jpg and Ace.jpg never both appear), else `stem` + `ext`."""
    existing = find_image(folder, stem)
    if existing is not None and existing.suffix.lower() == ext.lower():
        return existing
    if existing is not None:
        return existing.with_suffix(ext.lower())
    return Path(folder) / f"{stem}{ext.lower()}"


def release_cover_target(release: Release, ext: str = ".jpg") -> Path:
    return _target(config.ART_DIR, release_stem(release), ext)


def artist_image_target(artist: Artist, ext: str = ".jpg") -> Path:
    return _target(config.ARTIST_IMG_DIR, artist_stem(artist), ext)


def _ext_for(data: bytes, hint: Optional[str] = None) -> str:
    hint = (hint or "").lower()
    if hint in config.IMAGE_EXTENSIONS:
        return ".jpg" if hint == ".jpeg" else hint
    return ".png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"


def _set_aside(path: Path, why: str = SUPERSEDED_DIR) -> Optional[Path]:
    """Move `path` into `<its folder>\\_superseded\\` (never delete)."""
    if not path.exists():
        return None
    dest_dir = path.parent / why
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        dest = dest_dir / f"{path.stem} ({dt.datetime.now():%Y%m%d-%H%M%S}){path.suffix}"
    shutil.move(str(path), str(dest))
    _INDEX.forget(path.parent)
    return dest


def _write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    _INDEX.forget(dest.parent)


def _save(folder: Path, stem: str, data: bytes, ext_hint: Optional[str], overwrite: bool) -> Optional[Path]:
    if not data:
        return None
    config.ensure_dirs()
    folder.mkdir(parents=True, exist_ok=True)
    existing = find_image(folder, stem)
    if existing is not None and not overwrite:
        return existing
    ext = _ext_for(data, ext_hint)
    dest = _target(folder, stem, ext)
    if existing is not None and existing != dest:
        # same picture name, different extension (old .png, new .jpg)
        _set_aside(existing)
    if dest.exists():
        try:
            if dest.read_bytes() == data:
                return dest
        except OSError:
            pass
    _write(dest, data)
    return dest


def save_release_cover(release: Release, data: bytes, *, ext_hint: Optional[str] = None,
                       overwrite: bool = True) -> Optional[str]:
    """Save `data` as this release's cover and return the path to store in
    cover_path. `overwrite=False` (the scanner) keeps a picture that's
    already there - a downloaded or hand-picked cover is never replaced by
    a rescan's embedded one."""
    try:
        path = _save(config.ART_DIR, release_stem(release), data, ext_hint, overwrite)
        return str(path) if path else None
    except Exception as exc:  # pragma: no cover - disk trouble
        log.warning("cover save failed for release %s: %s", release.id, exc)
        return None


def save_artist_image(artist: Artist, source: Path) -> str:
    """Copy an image file in as this artist's photo (always replaces)."""
    data = Path(source).read_bytes()
    path = _save(config.ARTIST_IMG_DIR, artist_stem(artist), data, Path(source).suffix, True)
    return str(path)


# --------------------------------------------------------------------------
# relink (after a sync brings image files in)
# --------------------------------------------------------------------------


@dataclass
class RelinkResult:
    covers: int = 0
    artist_images: int = 0

    def summary(self) -> str:
        if not (self.covers or self.artist_images):
            return "No new pictures to link"
        return f"Linked {self.covers:,} covers and {self.artist_images:,} artist photos"


def _missing(path: Optional[str]) -> bool:
    return not path or not Path(path).exists()


_OLD_NAME = re.compile(r"^(release_\d+_[0-9a-f]+|\d+_.+)\.\w+$", re.IGNORECASE)


def _is_old_name(path: str) -> bool:
    """An id-named file from before name-based artwork
    (release_6357_8c79….jpg, 1003_The_Weeknd.jpg)."""
    return bool(_OLD_NAME.match(re.split(r"[\\/]", path)[-1]))


def relink(session: Session, *, refresh_existing: bool = True) -> RelinkResult:
    """Point every release/artist at its named picture when one exists.

    Rows with no picture (or a broken path) get linked. With
    `refresh_existing`, rows still pointing at an old id-named file are
    moved over to the named one too. A deliberately different name (a
    " (1998)" twin from `migrate`) is left alone."""
    result = RelinkResult()
    for release in session.scalars(select(Release)):
        found = find_image(config.ART_DIR, release_stem(release))
        if found is None:
            continue
        current = release.cover_path
        if _missing(current) or (refresh_existing and _is_old_name(current)):
            if current != str(found):
                release.cover_path = str(found)
                result.covers += 1
    for artist in session.scalars(select(Artist)):
        found = find_image(config.ARTIST_IMG_DIR, artist_stem(artist))
        if found is None:
            continue
        current = artist.image_path
        if _missing(current) or (refresh_existing and _is_old_name(current)):
            if current != str(found):
                artist.image_path = str(found)
                result.artist_images += 1
    session.flush()
    return result


# --------------------------------------------------------------------------
# one-time migration from id-named files
# --------------------------------------------------------------------------


def naming_done(session: Session) -> bool:
    row = session.get(Setting, PREF_NAMING)
    return row is not None and row.value == NAMING_VERSION


def _mark_done(session: Session) -> None:
    row = session.get(Setting, PREF_NAMING)
    if row is None:
        session.add(Setting(key=PREF_NAMING, value=NAMING_VERSION))
    else:
        row.value = NAMING_VERSION
    session.flush()


@dataclass
class MigrationResult:
    renamed: int = 0
    copied_in: int = 0
    already_named: int = 0
    shared: int = 0
    superseded: list[str] = field(default_factory=list)
    unused: int = 0
    missing: int = 0
    backup: Optional[Path] = None
    log_path: Optional[Path] = None
    lines: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.renamed:,} renamed, {self.copied_in:,} copied in, "
            f"{self.already_named:,} already named, {len(self.superseded):,} set aside as duplicates, "
            f"{self.unused:,} unused moved to _unused, {self.missing:,} missing"
        )


def _resolve_existing(path: Optional[str], folder: Path) -> Optional[Path]:
    """The file a stored path refers to - the path itself, or the same name
    in `folder` (a data folder moved from another PC)."""
    if not path:
        return None
    p = Path(path)
    if p.is_file():
        return p
    alt = folder / re.split(r"[\\/]", str(path))[-1]   # a Windows path read on Linux too
    return alt if alt.is_file() else None


def _inside(path: Path, folder: Path) -> bool:
    try:
        return os.path.normcase(str(path.resolve().parent)) == os.path.normcase(str(folder.resolve()))
    except OSError:
        return False


def backup_database(db_path: Path, backup_dir: Path, name: str) -> Optional[Path]:
    """Online SQLite backup (includes anything still in the -wal)."""
    if not db_path.is_file():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / name
    if dest.exists():
        dest = backup_dir / f"{dest.stem}-{dt.datetime.now():%Y%m%d-%H%M%S}{dest.suffix}"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def _migrate_kind(
    rows: list,                     # releases or artists
    attr: str,                      # "cover_path" / "image_path"
    folder: Path,
    stem_of: Callable,
    result: MigrationResult,
) -> set[str]:
    """Rename one folder's files. Returns the file names now in use."""
    folder.mkdir(parents=True, exist_ok=True)
    # group rows by their target name
    groups: dict[str, list] = {}
    sources: dict[int, Optional[Path]] = {}
    for row in rows:
        src = _resolve_existing(getattr(row, attr), folder)
        if getattr(row, attr) and src is None:
            result.missing += 1
            result.lines.append(f"missing: {getattr(row, attr)}")
        sources[id(row)] = src
        if src is not None:
            groups.setdefault(lookup_key(stem_of(row)), []).append(row)

    # Split each name into one entry per distinct picture. Two different
    # albums can share a name ("Various Artists - Greatest Hits"): the
    # newest picture keeps the plain name, the others get " (year)" or
    # " (2)" so no album loses its cover.
    targets: list[tuple[str, list]] = []
    for key, members in groups.items():
        by_content: dict[str, list] = {}
        paths = {os.path.normcase(str(sources[id(r)])) for r in members}
        for r in members:
            src = sources[id(r)]
            ident = _content_id(src) if len(paths) > 1 else "one"
            by_content.setdefault(ident, []).append(r)
        subgroups = sorted(
            by_content.values(),
            key=lambda rows: max(sources[id(r)].stat().st_mtime for r in rows),
            reverse=True,
        )
        base = _group_stem(members, sources, stem_of, key, folder)
        taken = {lookup_key(base)}
        for n, rows in enumerate(subgroups):
            stem = base
            if n:
                years = {getattr(r, "year", None) for r in rows}
                year = years.pop() if len(years) == 1 else None
                stem = f"{base} ({year})" if year else f"{base} ({n + 1})"
                if lookup_key(stem) in taken:
                    stem = f"{base} ({n + 1})"
                result.lines.append(f"different picture, same name: {base} -> {stem}")
            taken.add(lookup_key(stem))
            targets.append((stem, rows))

    # how many targets use each source file (a shared file is copied, not
    # moved, so the other target still has it)
    uses: dict[str, int] = {}
    for stem, members in targets:
        for s in {os.path.normcase(str(sources[id(r)])) for r in members}:
            uses[s] = uses.get(s, 0) + 1

    in_use: set[str] = set()
    for stem, members in targets:
        # the winner: newest file among this target's sources (all the same
        # picture, possibly several copies)
        distinct: dict[str, Path] = {}
        for r in members:
            src = sources[id(r)]
            distinct.setdefault(os.path.normcase(str(src)), src)
        winner = max(distinct.values(), key=lambda p: p.stat().st_mtime)
        ext = winner.suffix.lower()
        if ext == ".jpeg":
            ext = ".jpg"
        dest = folder / f"{stem}{ext}"
        if os.path.normcase(str(winner)) == os.path.normcase(str(dest)):
            result.already_named += 1
        elif dest.exists() and not _same_file(dest, winner):
            # a name-based file is already there (e.g. from MusicBee/USB) and
            # differs: keep the newer one, set the other aside
            if dest.stat().st_mtime >= winner.stat().st_mtime:
                winner = dest
                result.already_named += 1
            else:
                result.superseded.append(str(_set_aside(dest)))
                _move_or_copy(winner, dest, folder, uses, result)
                winner = dest
        elif dest.exists():
            winner = dest
            result.already_named += 1
        else:
            _move_or_copy(winner, dest, folder, uses, result)
            winner = dest
        if len(members) > 1:
            result.shared += len(members) - 1
        for r in members:
            setattr(r, attr, str(winner))
        in_use.add(winner.name.casefold())
        result.lines.append(f"{stem}: {len(members)} row(s) -> {winner.name}")
    _INDEX.forget(folder)
    return in_use


def _group_stem(members: list, sources: dict, stem_of: Callable, key: str, folder: Path) -> str:
    """One spelling per name, the same on every run: rows that differ only
    in case or punctuation ("Bob Seger & The Silver Bullet Band" / "...the
    ...") share a file. Keep a spelling that's already a file; otherwise
    take the first alphabetically."""
    for r in members:
        src = Path(sources[id(r)])
        if lookup_key(src.stem) == key and _inside(src, folder):
            return src.stem
    return min(stem_of(r) for r in members)


def _content_id(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_file(a: Path, b: Path) -> bool:
    try:
        if os.path.samefile(a, b):
            return True
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _move_or_copy(src: Path, dest: Path, folder: Path, uses: dict[str, int], result: MigrationResult) -> None:
    key = os.path.normcase(str(src))
    if _inside(src, folder) and uses.get(key, 0) <= 1:
        if os.path.normcase(str(src)) == os.path.normcase(str(dest)):
            # case-only difference on Windows
            tmp = src.with_name(src.name + ".tmp")
            os.replace(src, tmp)
            os.replace(tmp, dest)
        else:
            os.replace(src, dest)
        result.renamed += 1
    else:
        # a file outside the data folder (an artist photo linked in place),
        # or one another name still needs: copy, never move
        shutil.copy2(src, dest)
        uses[key] = uses.get(key, 1) - 1
        result.copied_in += 1


def _set_aside_unused(folder: Path, in_use: set[str], wanted_keys: set[str],
                      result: MigrationResult) -> None:
    """Move image files nothing uses into `_unused\\`. A file named for a
    release/artist the library has (dropped in by hand, or copied from
    MusicBee) counts as used even if no row points at it yet - relink links
    it."""
    for entry in list(os.scandir(folder)):
        if not entry.is_file():
            continue
        if Path(entry.name).suffix.lower() not in config.IMAGE_EXTENSIONS:
            continue
        if entry.name.casefold() in in_use:
            continue
        if lookup_key(Path(entry.name).stem) in wanted_keys:
            continue
        _set_aside(Path(entry.path), UNUSED_DIR)
        result.unused += 1


def migrate(
    session: Session,
    *,
    db_path: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
    set_aside_unused: bool = True,
) -> MigrationResult:
    """Rename this data folder's artwork to name-based files, once.

    1. Back up library.db (SQLite backup API).
    2. Releases: each cover goes to `artwork\\<Album Artist> - <Title>.ext`.
       Releases that share a name share one file; if their pictures differ,
       the newest wins and the others go to `artwork\\_superseded\\`.
    3. Artists: `artists\\<Artist>.ext`. A photo linked from outside the
       data folder is copied in, never moved.
    4. Image files nothing uses any more go to `_unused\\` (never deleted).
    5. Mark the data folder as migrated and write a log.

    Safe to run again: files already named correctly are left alone."""
    result = MigrationResult()
    if db_path is not None:
        result.backup = backup_database(
            db_path, backup_dir or config.DB_BACKUP_DIR, "library-before-artwork-rename.db"
        )
    releases = list(session.scalars(select(Release).where(Release.cover_path.is_not(None))))
    artists = list(session.scalars(select(Artist).where(Artist.image_path.is_not(None))))
    used_art = _migrate_kind(releases, "cover_path", config.ART_DIR, release_stem, result)
    used_artists = _migrate_kind(artists, "image_path", config.ARTIST_IMG_DIR, artist_stem, result)
    session.flush()
    if set_aside_unused:
        release_keys = {lookup_key(release_stem(r)) for r in session.scalars(select(Release))}
        artist_keys = {lookup_key(artist_stem(a)) for a in session.scalars(select(Artist))}
        _set_aside_unused(config.ART_DIR, used_art, release_keys, result)
        _set_aside_unused(config.ARTIST_IMG_DIR, used_artists, artist_keys, result)
    linked = relink(session, refresh_existing=False)
    result.lines.append(linked.summary())
    _mark_done(session)
    try:
        log_path = config.DATA_DIR / f"artwork-rename-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
        log_path.write_text(
            result.summary() + "\n\n" + "\n".join(result.lines) + "\n\nSet aside:\n"
            + "\n".join(result.superseded) + "\n",
            encoding="utf-8",
        )
        result.log_path = log_path
    except OSError:
        pass
    log.info("artwork rename: %s", result.summary())
    return result


def migrate_if_needed(session: Session, db_path: Optional[Path] = None) -> Optional[MigrationResult]:
    """Called at startup: runs `migrate` the first time a data folder is
    opened by a version with name-based artwork. Never raises."""
    try:
        if naming_done(session):
            return None
        return migrate(session, db_path=db_path)
    except Exception:  # pragma: no cover - logged, retried next start
        log.exception("artwork rename failed; will retry next start")
        session.rollback()
        return None


def image_folders_for_sync() -> Iterable[tuple[Path, str]]:
    """(local folder, path on the USB drive) for the artwork sync pairs."""
    yield config.ART_DIR, "MusicMgr/artwork"
    yield config.ARTIST_IMG_DIR, "MusicMgr/artists"
