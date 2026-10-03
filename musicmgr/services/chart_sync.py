"""Charts through the USB drive (2026-09-30, USB sync step 3's fifth box).

James: "I would like to have Charts be added to the SETTINGS Sync to USB".

Rides on library_state.sync_library_state as the "Charts" checkbox, but
keeps its own files, because a chart can be big (the Billboard Hot 100
Weekly chart is 3,393 editions, about 340,000 rows) and the main
library-state file is read and written on every sync:

- ``MusicMgr/sync/charts/index.json.gz`` - one small entry per chart, keyed
  by its folder path and name (``Billboard/Pop/Year End``): name, folder,
  size, source and description, plus ``sig`` - a hash of everything in
  the chart - and ``changed_at``.
- ``MusicMgr/sync/charts/<sig>.json.gz`` - a chart's editions and rows.
  Named by content, so a chart that hasn't changed is never written twice,
  and one that was only moved or renamed isn't copied at all.
- ``data/sync/charts-base-<drive>.json.gz`` - this PC's copy of the index
  as of its last sync (the *base*).

The index merges three-way exactly like playlists (see library_state):
changed only here -> ours, only on the drive -> theirs, both -> the newer
``changed_at``; deleted on one side and untouched on the other -> deleted.
A chart moved to another folder or renamed shows up as one key gone and
another new with the same ``sig``; the chart is then moved here rather
than deleted and rebuilt.

The first sync after the Source > Genre > Type reorganization
(tools/organize_charts_by_source.py) is the awkward case: one PC has
``Billboard/Pop/Year End``, the other still ``Pop/Billboard YE``, and with
no base yet neither looks deleted. A chart this PC has never synced whose
contents match one the drive has under a different name, which this PC
has never synced either, is therefore taken to be the same chart: it's
moved and renamed to match instead of both copies being kept.

What travels per row: rank, title, artist, last week, peak, weeks on
chart, and a match fixed by hand ("Fix match…", ``match_locked``) as the
track's portable key. Automatic matches don't travel - each PC matches
rows against its own library when a chart arrives (reusing any match it
already had for the same title and artist, so only new songs cost a
fuzzy lookup).
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from .. import config
from ..db.models import Chart, ChartEntry, ChartFolder, ChartIssue, Setting
from . import charts as chart_svc
from .matching import best_match, normalize

log = logging.getLogger(__name__)

CHARTS_DIR = "charts"
INDEX_NAME = "index.json.gz"
FORMAT = 1
#: {chart id: [local fingerprint, sig, changed_at]} - saves hashing every
#: chart's rows on every sync; see _fingerprints
PREF_CACHE = "chart_sync_cache"
EPOCH = "1970-01-01T00:00:00+00:00"
MATCH_THRESHOLD = 0.72


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def usb_dir(drive) -> Path:
    return drive.sync_dir / CHARTS_DIR


def usb_index_path(drive) -> Path:
    return usb_dir(drive) / INDEX_NAME


def base_index_path(drive) -> Path:
    return config.DATA_DIR / "sync" / f"charts-base-{drive.drive_id}.json.gz"


def content_path(drive, sig: str) -> Path:
    return usb_dir(drive) / f"{sig}.json.gz"


def load_index(path: Path) -> Optional[dict]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError) as exc:
        log.warning("can't read %s: %s", path, exc)
        return None
    charts = data.get("charts")
    return charts if isinstance(charts, dict) else {}


def _write_gz(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".mmsync-tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, path)


def save_index(path: Path, charts: dict, pc_id: str) -> None:
    _write_gz(path, {"format": FORMAT, "charts": charts, "saved": _now_iso(), "saved_by": pc_id})


def load_content(path: Path) -> Optional[dict]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError) as exc:
        log.warning("can't read %s: %s", path, exc)
        return None


# --------------------------------------------------------------------------
# chart folders by path
# --------------------------------------------------------------------------


class _Folders:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.rows: dict[int, tuple[str, Optional[int]]] = {
            fid: (name, parent) for fid, name, parent in db.execute(
                select(ChartFolder.id, ChartFolder.name, ChartFolder.parent_id))
        }

    def path(self, fid: Optional[int]) -> str:
        parts, seen = [], set()
        while fid is not None and fid in self.rows and fid not in seen:
            seen.add(fid)
            parts.append(self.rows[fid][0])
            fid = self.rows[fid][1]
        return "/".join(reversed(parts))

    def ensure(self, path: str) -> Optional[int]:
        parent: Optional[int] = None
        for name in [p for p in (path or "").split("/") if p]:
            found = next((fid for fid, (n, p) in self.rows.items()
                          if p == parent and n == name), None)
            if found is None:
                folder = ChartFolder(name=name, parent_id=parent)
                self.db.add(folder)
                self.db.flush()
                found = folder.id
                self.rows[found] = (name, parent)
            parent = found
        return parent

    def prune(self, fids: set[int]) -> list[str]:
        """Remove the given folders - and then their parents - once they hold
        no chart and no subfolder. Returns the paths removed."""
        removed = []
        todo = set(f for f in fids if f is not None)
        while todo:
            fid = todo.pop()
            if fid not in self.rows:
                continue
            if any(p == fid for (_n, p) in self.rows.values()):
                continue
            if self.db.scalar(select(func.count()).select_from(Chart)
                              .where(Chart.folder_id == fid)):
                continue
            removed.append(self.path(fid))
            parent = self.rows[fid][1]
            folder = self.db.get(ChartFolder, fid)
            if folder is not None:
                self.db.delete(folder)
                self.db.flush()
            del self.rows[fid]
            if parent is not None:
                todo.add(parent)
        return removed


def _key(folder_path: str, name: str) -> str:
    return f"{folder_path}/{name}" if folder_path else name


def keyed_charts(db: Session, folders: Optional[_Folders] = None) -> dict[str, int]:
    """{portable key: chart id}. Same folder and name twice get " #2"."""
    folders = folders or _Folders(db)
    keyed: dict[str, int] = {}
    for cid, name, fid in db.execute(
        select(Chart.id, Chart.name, Chart.folder_id).order_by(Chart.id)
    ):
        key = base = _key(folders.path(fid), name)
        n = 2
        while key in keyed:
            key = f"{base} #{n}"
            n += 1
        keyed[key] = cid
    return keyed


# --------------------------------------------------------------------------
# reading a chart
# --------------------------------------------------------------------------


def _fingerprints(db: Session) -> dict[int, str]:
    """A cheap per-chart summary that changes whenever its rows do (an
    import, a deleted edition, a fixed match). Local only - it uses ids."""
    rows = db.execute(text(
        "SELECT i.chart_id, count(DISTINCT i.id), count(e.id), max(i.id), max(e.id), "
        "total(e.match_locked), total(CASE WHEN e.match_locked THEN ifnull(e.track_id, -1) END), "
        "total(length(e.title) + length(e.artist_name)), "
        "total(ifnull(e.last_week, 0) + 7 * ifnull(e.peak_pos, 0) "
        "      + 13 * ifnull(e.weeks_on_chart, 0) + 31 * e.rank) "
        "FROM chart_issues i LEFT JOIN chart_entries e ON e.issue_id = i.id "
        "GROUP BY i.chart_id"
    )).all()
    return {r[0]: "|".join(str(v) for v in r[1:]) for r in rows}


def read_content(db: Session, chart_id: int, idx) -> dict:
    """A chart's editions and rows as they travel (see the module docstring).
    `lock`: None = matched automatically (or not at all), "" = fixed by hand
    to "no match", otherwise the fixed track's portable key."""
    issues = db.execute(
        select(ChartIssue.id, ChartIssue.chart_date, ChartIssue.title)
        .where(ChartIssue.chart_id == chart_id).order_by(ChartIssue.chart_date, ChartIssue.id)
    ).all()
    rows_by_issue: dict[int, list] = {i[0]: [] for i in issues}
    for (issue_id, rank, title, artist, lw, peak, weeks, locked, track_id) in db.execute(
        select(ChartEntry.issue_id, ChartEntry.rank, ChartEntry.title, ChartEntry.artist_name,
               ChartEntry.last_week, ChartEntry.peak_pos, ChartEntry.weeks_on_chart,
               ChartEntry.match_locked, ChartEntry.track_id)
        .join(ChartIssue, ChartIssue.id == ChartEntry.issue_id)
        .where(ChartIssue.chart_id == chart_id)
        .order_by(ChartEntry.issue_id, ChartEntry.rank, ChartEntry.id)
    ):
        lock = None
        if locked:
            lock = (idx.key_of.get(track_id) or "") if track_id else ""
        rows_by_issue[issue_id].append([rank, title, artist, lw, peak, weeks, lock])
    return {"issues": [[d.isoformat() if d else None, t, rows_by_issue[i]]
                       for i, d, t in issues]}


def content_sig(content: dict) -> str:
    return hashlib.sha1(
        json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _load_cache(db: Session) -> dict[str, list]:
    row = db.get(Setting, PREF_CACHE)
    try:
        data = json.loads(row.value) if row and row.value else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _save_cache(db: Session, cache: dict) -> None:
    row = db.get(Setting, PREF_CACHE)
    value = json.dumps(cache)
    if row is None:
        db.add(Setting(key=PREF_CACHE, value=value))
    else:
        row.value = value
    db.flush()


class LocalCharts:
    """This library's charts as an index, with contents read on demand."""

    def __init__(self, db: Session, idx) -> None:
        self.db, self.idx = db, idx
        self.folders = _Folders(db)
        self.ids = keyed_charts(db, self.folders)
        self.cache = _load_cache(db)
        self._contents: dict[int, dict] = {}
        fps = _fingerprints(db)
        meta = {c.id: c for c in db.scalars(select(Chart))}
        self.index: dict[str, dict] = {}
        live = set()
        for key, cid in self.ids.items():
            live.add(str(cid))
            fp = fps.get(cid, "")
            cached = self.cache.get(str(cid))
            if cached and cached[0] == fp:
                sig, changed = cached[1], cached[2]
            else:
                sig = content_sig(self.content(cid))
                if cached is None:
                    changed = EPOCH             # never synced: not "just now"
                elif cached[1] != sig:
                    changed = _now_iso()        # first time this PC sees it so
                else:
                    changed = cached[2]
                self.cache[str(cid)] = [fp, sig, changed]
            chart = meta[cid]
            self.index[key] = {
                "name": chart.name,
                "folder": self.folders.path(chart.folder_id),
                "kind": chart.kind,
                "source": chart.source,
                "description": chart.description,
                "size": chart.size,
                "sig": sig,
                "changed_at": changed,
            }
        for stale in set(self.cache) - live:
            del self.cache[stale]

    def content(self, chart_id: int) -> dict:
        if chart_id not in self._contents:
            self._contents[chart_id] = read_content(self.db, chart_id, self.idx)
        return self._contents[chart_id]

    def remember(self, chart_id: int, sig: str, changed_at: str) -> None:
        fp = _fingerprints_one(self.db, chart_id)
        self.cache[str(chart_id)] = [fp, sig, changed_at]


def _fingerprints_one(db: Session, chart_id: int) -> str:
    db.flush()
    return _fingerprints(db).get(chart_id, "")


def mark_edited(db: Session, chart_id: int) -> None:
    """A position was added, edited or removed by hand (2026-10-03, the
    Charts page's Add/Edit/Remove song buttons). The fingerprint above is
    cheap but not exact - retyping a title to one of the same length
    leaves it unchanged - so forget this chart's cached fingerprint. The
    next sync then re-hashes it, sees the new ``sig`` and stamps it
    changed now, so the edit wins over the drive's older copy. A chart
    that has never synced has no cache entry and needs nothing."""
    cache = _load_cache(db)
    entry = cache.get(str(chart_id))
    if entry:
        entry[0] = ""
        _save_cache(db, cache)


# --------------------------------------------------------------------------
# writing a chart that arrived
# --------------------------------------------------------------------------


def _existing_matches(db: Session) -> dict[tuple[str, str], tuple[int, float]]:
    """(title key, artist key) -> the automatic match this library already
    made somewhere, so a chart arriving with the same songs needn't redo it."""
    known: dict[tuple[str, str], tuple[int, float]] = {}
    for tk, ak, tid, score in db.execute(
        select(ChartEntry.title_key, ChartEntry.artist_key, ChartEntry.track_id,
               ChartEntry.match_score)
        .where(ChartEntry.track_id.is_not(None), ChartEntry.match_locked.is_(False))
    ):
        known.setdefault((tk, ak), (tid, score))
    return known


class _Matcher:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.known: Optional[dict] = None
        self.misses: set[tuple[str, str]] = set()

    def match(self, title: str, artist: str, tk: str, ak: str) -> tuple[Optional[int], Optional[float]]:
        if self.known is None:
            self.known = _existing_matches(self.db)
        hit = self.known.get((tk, ak))
        if hit is not None:
            return hit
        if (tk, ak) in self.misses:
            return None, None
        found = best_match(title, artist, chart_svc._candidate_tracks(self.db, tk),
                           MATCH_THRESHOLD)
        if found:
            self.known[(tk, ak)] = (found[0], found[1])
            return found[0], found[1]
        self.misses.add((tk, ak))
        return None, None


def _clear_rows(db: Session, chart_id: int) -> None:
    db.flush()  # expire_all below would otherwise drop unflushed edits
    issue_ids = select(ChartIssue.id).where(ChartIssue.chart_id == chart_id)
    db.execute(delete(ChartEntry).where(ChartEntry.issue_id.in_(issue_ids))
               .execution_options(synchronize_session=False))
    db.execute(delete(ChartIssue).where(ChartIssue.chart_id == chart_id)
               .execution_options(synchronize_session=False))
    db.expire_all()


def _fill(db: Session, chart_id: int, content: dict, idx, matcher: _Matcher) -> None:
    issues = content.get("issues") or []
    db.execute(insert(ChartIssue), [
        {"chart_id": chart_id, "chart_date": dt.date.fromisoformat(d), "title": t}
        for d, t, _rows in issues if d
    ])
    ids = {d: i for i, d in db.execute(
        select(ChartIssue.id, ChartIssue.chart_date).where(ChartIssue.chart_id == chart_id))}
    batch: list[dict] = []
    for d, _t, rows in issues:
        if not d:
            continue
        issue_id = ids[dt.date.fromisoformat(d)]
        for rank, title, artist, lw, peak, weeks, lock in rows:
            tk, ak = normalize(title), normalize(artist)
            locked, tid, score = False, None, None
            if lock is not None:
                tid = idx.track(lock) if lock else None
                if lock == "" or tid is not None:
                    locked, score = True, (1.0 if tid else None)
            if not locked:
                tid, score = matcher.match(title, artist, tk, ak)
            batch.append({
                "issue_id": issue_id, "rank": rank, "title": title, "artist_name": artist,
                "title_key": tk, "artist_key": ak, "last_week": lw, "peak_pos": peak,
                "weeks_on_chart": weeks, "track_id": tid, "match_score": score,
                "match_locked": locked,
            })
            if len(batch) >= 5000:
                db.execute(insert(ChartEntry), batch)
                batch = []
    if batch:
        db.execute(insert(ChartEntry), batch)
    db.flush()


def _unique_slug(db: Session, key: str) -> str:
    base = chart_svc.slugify(key)[:110] or "chart"
    slug, n = base, 2
    while db.scalar(select(Chart.id).where(Chart.slug == slug)) is not None:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _delete_chart(db: Session, chart_id: int) -> None:
    _clear_rows(db, chart_id)
    chart = db.get(Chart, chart_id)
    if chart is not None:
        db.delete(chart)
    db.flush()


# --------------------------------------------------------------------------
# the step
# --------------------------------------------------------------------------


@dataclass
class ChartNotes:
    charts_in: list[str] = field(default_factory=list)
    charts_removed: list[str] = field(default_factory=list)
    charts_out: int = 0
    charts_total: int = 0
    #: arrived in the index, but their file wasn't on the drive (tried again
    #: next sync)
    charts_missing: list[str] = field(default_factory=list)
    folders_removed: list[str] = field(default_factory=list)


def _strip(value: dict) -> dict:
    return {k: v for k, v in value.items() if k != "changed_at"}


def _match_moves(local: dict, usb: dict, base: dict) -> dict[str, str]:
    """{local key: drive key} for charts neither side has synced before
    that hold the same rows under different names (see module docstring)."""
    by_sig: dict[str, list[str]] = {}
    for key, value in usb.items():
        if key not in local and key not in base:
            by_sig.setdefault(value.get("sig"), []).append(key)
    moves = {}
    for key in sorted(local):
        if key in usb or key in base:
            continue
        candidates = by_sig.get(local[key].get("sig"))
        if candidates:
            moves[key] = candidates.pop(0)
    return moves


def sync_charts(db: Session, drive, idx, pc_id: str) -> ChartNotes:
    from .library_state import _merge_dict, _parse

    notes = ChartNotes()
    here = LocalCharts(db, idx)
    local = {k: dict(v) for k, v in here.index.items()}
    usb = load_index(usb_index_path(drive)) or {}
    base = load_index(base_index_path(drive)) or {}

    # the same chart, filed differently on the two PCs before either synced
    ids = dict(here.ids)
    touched_folders: set[int] = set()
    for lk, uk in _match_moves(local, usb, base).items():
        value = usb[uk]
        chart = db.get(Chart, ids[lk])
        touched_folders.add(chart.folder_id)
        chart.name = value.get("name") or chart.name
        chart.folder_id = here.folders.ensure(value.get("folder") or "")
        ids[uk] = ids.pop(lk)
        local[uk] = dict(value)
        del local[lk]
        here.cache[str(chart.id)] = [here.cache[str(chart.id)][0], value.get("sig"),
                                     value.get("changed_at") or EPOCH]
        notes.charts_in.append(uk)
    db.flush()

    def usb_newer(key: str) -> bool:
        lt, ut = _parse(local[key].get("changed_at")), _parse(usb[key].get("changed_at"))
        return bool(lt and ut and ut > lt)

    kept, taken = _merge_dict({k: _strip(v) for k, v in local.items()},
                              {k: _strip(v) for k, v in usb.items()},
                              {k: _strip(v) for k, v in base.items()}, newer=usb_newer)
    merged = {k: (usb[k] if k in taken else local[k]) for k in kept}
    removed = [k for k in local if k not in merged]
    removed_by_sig: dict[str, list[str]] = {}
    for k in removed:
        removed_by_sig.setdefault(local[k].get("sig"), []).append(k)

    matcher = _Matcher(db)
    failed: set[str] = set()
    for key in sorted(taken):
        value = merged[key]
        sig = value.get("sig")
        cid = ids.get(key)
        same_rows = cid is not None and local.get(key, {}).get("sig") == sig
        if cid is None and removed_by_sig.get(sig):
            # moved or renamed on the other PC: move it here too
            old = removed_by_sig[sig].pop(0)
            removed.remove(old)
            cid = ids.pop(old)
            ids[key] = cid
            same_rows = True
        if not same_rows:
            content = load_content(content_path(drive, sig))
            if content is None:
                failed.add(key)
                notes.charts_missing.append(key)
                continue
            if cid is None:
                chart = Chart(name=value.get("name") or key.rsplit("/", 1)[-1],
                              slug=_unique_slug(db, key),
                              kind=value.get("kind") or Chart.KIND_EXTERNAL)
                db.add(chart)
                db.flush()
                cid = chart.id
                ids[key] = cid
            else:
                _clear_rows(db, cid)
            _fill(db, cid, content, idx, matcher)
        chart = db.get(Chart, cid)
        touched_folders.add(chart.folder_id)
        chart.name = value.get("name") or chart.name
        chart.kind = value.get("kind") or chart.kind
        chart.source = value.get("source")
        chart.description = value.get("description")
        chart.size = value.get("size")
        chart.folder_id = here.folders.ensure(value.get("folder") or "")
        db.flush()
        here.remember(cid, sig, value.get("changed_at") or EPOCH)
        notes.charts_in.append(key)

    for key in removed:
        cid = ids.pop(key, None)
        if cid is None:
            continue
        chart = db.get(Chart, cid)
        if chart is not None:
            touched_folders.add(chart.folder_id)
        _delete_chart(db, cid)
        here.cache.pop(str(cid), None)
        notes.charts_removed.append(key)
    db.flush()
    notes.folders_removed = here.folders.prune(touched_folders)

    # contents the drive doesn't have yet (this PC's new or changed charts)
    for key, value in merged.items():
        if key in taken or key in failed:
            continue
        path = content_path(drive, value.get("sig"))
        if not path.exists() and key in ids:
            _write_gz(path, here.content(ids[key]))

    notes.charts_out = sum(
        1 for k in set(merged) | set(usb)
        if (_strip(merged[k]) if k in merged else None) != (_strip(usb[k]) if k in usb else None)
    )
    notes.charts_total = len(merged)
    notes.charts_in = sorted(set(notes.charts_in))
    notes.charts_removed = sorted(notes.charts_removed)

    save_index(usb_index_path(drive), merged, pc_id)
    base_copy = {k: v for k, v in merged.items() if k not in failed}
    for k in failed:
        if k in base:
            base_copy[k] = base[k]
    save_index(base_index_path(drive), base_copy, pc_id)
    _save_cache(db, here.cache)

    # files no chart refers to any more
    wanted = {f"{v.get('sig')}.json.gz" for v in merged.values()}
    folder = usb_dir(drive)
    if folder.is_dir():
        for path in folder.glob("*.json.gz"):
            if path.name != INDEX_NAME and path.name not in wanted:
                try:
                    path.unlink()
                except OSError as exc:
                    log.warning("can't remove %s: %s", path, exc)
    return notes
