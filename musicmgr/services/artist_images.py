"""Import artist portraits from a folder of images.

Two layouts are understood, because both are common in the wild:

    Artists/AC-DC.jpg                 one image per artist, named after them
    Artists/AC-DC/folder.jpg          one folder per artist, image inside

Matching runs through the same normalisation the rest of the app uses, so
"AC-DC.jpg", "acdc.png" and "AC_DC.jpeg" all reach the artist stored as
"AC/DC" - a filename can never contain a slash, so an exact string compare
would miss most of the interesting cases. Anything that does not match exactly
falls back to a fuzzy pass with a deliberately high threshold, and whatever is
still unmatched is reported rather than guessed at.

Images are copied into the app's own data folder so the library stays
self-contained and portable; your source folder is only read.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import Artist, ArtistAlias, Credit
from .matching import normalize, similarity

log = logging.getLogger(__name__)

#: below this a fuzzy name match is not trustworthy enough to assign silently
FUZZY_THRESHOLD = 0.86

#: filenames that mean "the image for this folder" rather than an artist name
GENERIC_NAMES = {
    "folder", "artist", "cover", "front", "photo", "image", "thumb",
    "poster", "banner", "fanart", "background", "logo",
}


@dataclass
class ArtistImageResult:
    matched: int = 0
    updated: int = 0
    skipped_existing: int = 0
    unmatched: list[str] = field(default_factory=list)
    fuzzy: list[tuple[str, str, float]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.matched} artist image(s) matched"]
        if self.updated:
            parts.append(f"{self.updated} updated")
        if self.skipped_existing:
            parts.append(f"{self.skipped_existing} already had one")
        if self.unmatched:
            parts.append(f"{len(self.unmatched)} unmatched")
        return " · ".join(parts)


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in config.IMAGE_EXTENSIONS


def iter_candidates(folder: Path) -> Iterable[tuple[str, Path]]:
    """Yield (artist name as written, image path) for both layouts."""
    folder = Path(folder)
    if not folder.is_dir():
        return
    for entry in sorted(folder.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_file() and _is_image(entry):
            yield entry.stem, entry
        elif entry.is_dir():
            images = sorted(p for p in entry.iterdir() if p.is_file() and _is_image(p))
            if not images:
                continue
            # prefer a conventionally named image, else just the first
            preferred = next(
                (p for p in images if p.stem.lower() in GENERIC_NAMES), images[0]
            )
            yield entry.name, preferred


def _clean_name(raw: str) -> str:
    """Strip the decoration people put in filenames: '01 - AC-DC (1980)'."""
    name = re.sub(r"^\d+\s*[-_.]\s*", "", raw)
    name = re.sub(r"\s*\(\d{4}\)\s*$", "", name)
    return name.strip(" -_")


def _artist_index(session: Session) -> dict[str, Artist]:
    """Every credited artist keyed by normalised name, aliases included."""
    index: dict[str, Artist] = {}
    stmt = (
        select(Artist)
        .join(Credit, Credit.artist_id == Artist.id)
        .where(Credit.track_id.is_not(None))
        .group_by(Artist.id)
    )
    for artist in session.scalars(stmt):
        index.setdefault(artist.name_key, artist)
    for alias in session.scalars(select(ArtistAlias)):
        artist = session.get(Artist, alias.artist_id)
        if artist is not None:
            index.setdefault(alias.alias_name_key, artist)
    return index


def _best_fuzzy(key: str, index: dict[str, Artist]) -> Optional[tuple[Artist, float]]:
    best: Optional[tuple[Artist, float]] = None
    for candidate_key, artist in index.items():
        score = similarity(key, candidate_key)
        if score >= FUZZY_THRESHOLD and (best is None or score > best[1]):
            best = (artist, score)
    return best


def _store(source: Path, artist: Artist) -> str:
    """Copy the image into the app's data folder under a stable name."""
    config.ensure_dirs()
    safe = re.sub(r"[^\w\-]+", "_", artist.name).strip("_")[:60] or "artist"
    dest = config.ARTIST_IMG_DIR / f"{artist.id}_{safe}{source.suffix.lower()}"
    if dest.resolve() != source.resolve():
        shutil.copy2(source, dest)
    return str(dest)


def import_artist_images(
    session: Session,
    folder: Path | str,
    overwrite: bool = True,
    copy: bool = True,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> ArtistImageResult:
    """Match every image in `folder` to an artist and record it.

    `overwrite=False` leaves artists that already have a portrait alone.

    2026-09-22 - James: "does the progress bar also work on the import of
    artist images" (it didn't - this ran entirely on the UI thread with no
    feedback at all, same class of problem the "Missing metadata"
    dashboard had before it got a progress bar). `progress`, when given, is
    called as `(done, total, name)` once per candidate image - `iter_candidates`
    is materialized into a list up front rather than iterated lazily so
    `total` is known from the start; a folder of artist portraits is small
    enough (nowhere near the scanner's whole-library scale) that this
    costs nothing worth avoiding."""
    result = ArtistImageResult()
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        result.errors.append(f"{folder} is not a folder")
        return result

    index = _artist_index(session)
    if not index:
        result.errors.append("no artists in the library yet - scan your music first")
        return result

    candidates = list(iter_candidates(folder))
    total = len(candidates)
    for i, (raw_name, path) in enumerate(candidates, start=1):
        if progress:
            progress(i, total, path.name)
        try:
            name = _clean_name(raw_name)
            key = normalize(name)
            if not key:
                result.unmatched.append(path.name)
                continue

            artist = index.get(key)
            score = 1.0
            if artist is None:
                hit = _best_fuzzy(key, index)
                if hit is None:
                    result.unmatched.append(path.name)
                    continue
                artist, score = hit
                result.fuzzy.append((path.name, artist.name, round(score, 3)))

            if artist.image_path and not overwrite:
                result.skipped_existing += 1
                continue

            had_one = bool(artist.image_path)
            artist.image_path = _store(path, artist) if copy else str(path)
            result.matched += 1
            if had_one:
                result.updated += 1
        except Exception as exc:  # one bad file must not stop the import
            log.exception("artist image import failed for %s", path)
            result.errors.append(f"{path.name}: {exc}")

    session.flush()
    return result


def artists_without_images(session: Session, limit: int = 50) -> list[str]:
    """Credited artists that still have no portrait - i.e. what to go and find."""
    stmt = (
        select(Artist)
        .join(Credit, Credit.artist_id == Artist.id)
        .where(Credit.track_id.is_not(None), Artist.image_path.is_(None))
        .group_by(Artist.id)
        .order_by(Artist.name)
        .limit(limit)
    )
    return [a.name for a in session.scalars(stmt)]


def clear_artist_images(session: Session) -> int:
    count = 0
    for artist in session.scalars(select(Artist).where(Artist.image_path.is_not(None))):
        artist.image_path = None
        count += 1
    session.flush()
    return count
