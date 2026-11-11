"""The Radio page's podcast dialogs and background work (services/podcasts.py):

- PodcastSearchDialog - find podcasts in Apple's free directory, or paste a
  feed / Apple Podcasts link, and subscribe (they go on the PODCAST band);
- PodcastEpisodesDialog - a podcast's episodes, newest first, with show
  notes: play one, mark it heard, download it to the Podcast folder or
  delete the download;
- the threads that do the network work, so the page never waits on it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
)

from ...db.models import PodcastEpisode, PodcastFeed
from ...db.session import new_session
from ...services import podcasts
from ...services.library import format_duration
from ..context import AppContext
from ..theme import COLORS
from ..widgets.common import SearchBar, TouchButton, TouchList, dim_label

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# background work
# --------------------------------------------------------------------------


class PodcastSearchThread(QThread):
    finished_with = Signal(object, str)  # list[Found], error text

    def __init__(self, term: str, parent=None) -> None:
        super().__init__(parent)
        self.term = term

    def run(self) -> None:  # pragma: no cover - network
        try:
            self.finished_with.emit(podcasts.search(self.term), "")
        except Exception as exc:
            self.finished_with.emit([], str(exc))


class SubscribeThread(QThread):
    finished_with = Signal(int, str)  # feed id (0 = failed), error text

    def __init__(self, link: str, title: str = "", parent=None) -> None:
        super().__init__(parent)
        self.link, self.title = link, title

    def run(self) -> None:  # pragma: no cover - network
        try:
            self.finished_with.emit(podcasts.subscribe(new_session, self.link, self.title), "")
        except Exception as exc:
            self.finished_with.emit(0, str(exc))


class PodcastCheckThread(QThread):
    """Look for new episodes (all podcasts, or `feed_ids`). First links
    episodes to copies already in the Podcast folder (from the USB drive)."""

    finished_with = Signal(object)  # podcasts.CheckResult

    def __init__(self, feed_ids: Optional[list[int]] = None, only_due: bool = False,
                 parent=None) -> None:
        super().__init__(parent)
        self.feed_ids, self.only_due = feed_ids, only_due

    def run(self) -> None:  # pragma: no cover - network
        try:
            with new_session() as s:
                if podcasts.link_downloads(s):
                    s.commit()
        except Exception:
            log.debug("couldn't link downloaded podcast episodes", exc_info=True)
        try:
            result = podcasts.check(new_session, self.feed_ids, only_due=self.only_due,
                                    cancelled=self.isInterruptionRequested)
        except Exception as exc:
            log.warning("podcast check failed: %s", exc)
            result = podcasts.CheckResult(failed=[str(exc)])
        self.finished_with.emit(result)


class DownloadThread(QThread):
    progress = Signal(int, int)       # bytes so far, total (0 = unknown)
    finished_with = Signal(int, str)  # episode id, error text ("" = done)

    def __init__(self, episode_id: int, url: str, dest: Path, parent=None) -> None:
        super().__init__(parent)
        self.episode_id, self.url, self.dest = episode_id, url, dest

    def run(self) -> None:  # pragma: no cover - network
        try:
            done = podcasts.download(self.url, self.dest,
                                     progress=lambda d, t: self.progress.emit(d, t),
                                     cancelled=self.isInterruptionRequested)
        except Exception as exc:
            self.finished_with.emit(self.episode_id, str(exc))
            return
        if done:
            with new_session() as s:
                ep = s.get(PodcastEpisode, self.episode_id)
                if ep is not None:
                    ep.local_path = str(self.dest)
                    s.commit()
        self.finished_with.emit(self.episode_id, "" if done else "cancelled")


# --------------------------------------------------------------------------
# find podcasts
# --------------------------------------------------------------------------


class PodcastSearchDialog(QDialog):
    """Search Apple's podcast directory (or paste a link) and subscribe."""

    #: subscriptions changed - the page redraws the band
    changed = Signal()
    #: subscribed and asked to listen straight away
    listenRequested = Signal(int)

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Find podcasts — PODCAST band")
        self.setMinimumSize(820, 680)
        self._results: list[podcasts.Found] = []
        self._thread: Optional[PodcastSearchThread] = None
        self._sub_thread: Optional[SubscribeThread] = None
        layout = QVBoxLayout(self)
        head = QLabel("Find podcasts")
        head.setObjectName("Title")
        layout.addWidget(head)
        intro = dim_label(
            "From Apple's free podcast directory - no account needed. Or paste a podcast's "
            "RSS feed link, or its Apple Podcasts page link.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.search = SearchBar("Podcast name or subject - or paste a link")
        self.search.submitted.connect(self.run)
        layout.addWidget(self.search)
        self.status = dim_label("")
        layout.addWidget(self.status)
        self.list = TouchList()
        self.list.currentItemChanged.connect(lambda *_: self._sync())
        self.list.itemActivatedPayload.connect(lambda _p: self._toggle())
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.add_btn = TouchButton("Subscribe", primary=True)
        self.add_btn.clicked.connect(self._toggle)
        self.listen_btn = TouchButton("Subscribe and listen")
        self.listen_btn.clicked.connect(lambda: self._toggle(listen=True))
        link_btn = TouchButton("Add by link…")
        link_btn.clicked.connect(self._ask_link)
        row.addWidget(self.add_btn)
        row.addWidget(self.listen_btn)
        row.addWidget(link_btn)
        row.addStretch(1)
        close = TouchButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        self._sync()

    def run(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if podcasts.looks_like_link(text):
            self.subscribe_link(text)
            return
        if self._thread is not None and self._thread.isRunning():
            return
        self.status.setText("Searching…")
        self._thread = PodcastSearchThread(text, self)
        self._thread.finished_with.connect(self.show_results)
        self._thread.start()

    def show_results(self, results, error: str = "") -> None:
        if error:
            self.status.setText(error)
            return
        self._results = list(results)
        self.status.setText(f"{len(self._results)} podcasts" if self._results
                            else "No podcasts found")
        self._refill()

    def _refill(self) -> None:
        row = self.list.currentRow()
        rows = []
        with self.ctx.session() as s:
            for i, f in enumerate(self._results):
                on = podcasts.by_url(s, f.feed_url) is not None
                rows.append({
                    "lead": "✓" if on else "",
                    "lead_color": COLORS["jukebox_key_hi"],
                    "primary": f.title,
                    "secondary": f.detail,
                    "trail": "On the dial" if on else "",
                    "index": i,
                    "on": on,
                })
        self.list.set_rows(rows)
        if 0 <= row < len(rows):
            self.list.setCurrentRow(row)
        self._sync()

    def _current(self) -> Optional[podcasts.Found]:
        payload = self.list.current_payload() or {}
        i = payload.get("index")
        return self._results[i] if i is not None and i < len(self._results) else None

    def _busy(self) -> bool:
        return self._sub_thread is not None and self._sub_thread.isRunning()

    def _sync(self) -> None:
        payload = self.list.current_payload() or {}
        has = bool(payload) and not self._busy()
        self.add_btn.setEnabled(has)
        self.listen_btn.setEnabled(has and not payload.get("on"))
        self.add_btn.setText("Unsubscribe" if payload.get("on") else "Subscribe")

    def _toggle(self, listen: bool = False) -> None:
        f = self._current()
        if f is None or self._busy():
            return
        with self.ctx.session() as s:
            feed = podcasts.by_url(s, f.feed_url)
            if feed is not None:
                if listen:
                    self.listenRequested.emit(feed.id)
                    return
                title = feed.title
                podcasts.remove_feed(s, feed.id)
                s.commit()
                self.ctx.notify(f"Unsubscribed from {title}")
                self.changed.emit()
                self._refill()
                return
        self.subscribe_link(f.feed_url, f.title, listen=listen)

    def subscribe_link(self, link: str, title: str = "", listen: bool = False) -> None:
        if self._busy():
            return
        self.status.setText(f"Reading {title or 'the feed'}…")
        self._listen_after = listen
        self._sub_thread = SubscribeThread(link, title, self)
        self._sub_thread.finished_with.connect(self.subscribed)
        self._sub_thread.start()
        self._sync()

    def subscribed(self, feed_id: int, error: str) -> None:
        if not feed_id:
            self.status.setText(error or "Couldn't subscribe")
            self._sync()
            return
        with self.ctx.session() as s:
            feed = s.get(PodcastFeed, feed_id)
            title = feed.title if feed else "the podcast"
            count = len(feed.episodes) if feed else 0
        self.status.setText(f"Subscribed to {title} - {count:,} episodes")
        self.ctx.notify(f"{title} is on the PODCAST band")
        self.changed.emit()
        self._refill()
        if getattr(self, "_listen_after", False):
            self.listenRequested.emit(feed_id)

    def _ask_link(self) -> None:  # pragma: no cover - dialog
        link, ok = QInputDialog.getText(
            self, "Add a podcast", "The podcast's RSS feed link, or its Apple Podcasts page link:")
        if ok and link.strip():
            self.subscribe_link(link.strip())


# --------------------------------------------------------------------------
# episodes
# --------------------------------------------------------------------------


class PodcastEpisodesDialog(QDialog):
    """A podcast's episodes, newest first: play, mark heard, download."""

    episodeChosen = Signal(int)

    def __init__(self, ctx: AppContext, feed_id: int, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.feed_id = feed_id
        self._download: Optional[DownloadThread] = None
        self.setMinimumSize(760, 720)
        layout = QVBoxLayout(self)
        self.header = QLabel("")
        self.header.setObjectName("Title")
        layout.addWidget(self.header)
        self.sub = dim_label("")
        layout.addWidget(self.sub)
        self.list = TouchList()
        self.list.itemActivatedPayload.connect(self._chosen)
        layout.addWidget(self.list, 3)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText("Show notes")
        self.notes.setMaximumHeight(170)
        layout.addWidget(self.notes, 1)
        self.status = dim_label("")
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.play_btn = TouchButton("Play ▶", primary=True)
        self.play_btn.clicked.connect(lambda: self._chosen(self.list.current_payload()))
        self.toggle_btn = TouchButton("Mark heard")
        self.toggle_btn.clicked.connect(self._toggle_played)
        self.download_btn = TouchButton("Download")
        self.download_btn.setToolTip("Keep a copy in the Podcast folder - it plays from there, "
                                     "and can go on the USB drive")
        self.download_btn.clicked.connect(self._toggle_download)
        for b in (self.play_btn, self.toggle_btn, self.download_btn):
            row.addWidget(b)
        row.addStretch(1)
        close = TouchButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        self.list.currentItemChanged.connect(lambda *_: self._sync())
        self.refresh()

    def refresh(self) -> None:
        with self.ctx.session() as s:
            feed = s.get(PodcastFeed, self.feed_id)
            if feed is None:
                return
            self.setWindowTitle(feed.title)
            self.header.setText(feed.title)
            eps = podcasts.episodes(s, self.feed_id)
            current = podcasts.current_episode(s, feed)
            heard = sum(1 for e in eps if e.played)
            new = sum(1 for e in eps if podcasts.is_new(e))
            saved = sum(1 for e in eps if e.local_path)
            bits = [f"{len(eps):,} episodes", f"{heard:,} heard"]
            if new:
                bits.append(f"{new:,} new")
            if saved:
                bits.append(f"{saved:,} downloaded")
            if feed.author:
                bits.insert(0, feed.author)
            self.sub.setText(" · ".join(bits))
            rows = []
            current_row = 0
            for i, e in enumerate(eps):
                is_current = current is not None and e.id == current.id
                if is_current:
                    current_row = i
                if is_current:
                    lead = "▶"
                elif e.played:
                    lead = "✓"
                elif e.position_ms:
                    lead = "◐"
                elif podcasts.is_new(e):
                    lead = "●"
                else:
                    lead = ""
                when = e.published.strftime("%Y-%m-%d") if e.published else ""
                secondary = [when] if when else []
                if e.position_ms and not e.played:
                    secondary.append(f"stopped at {format_duration(e.position_ms)}")
                if e.local_path:
                    secondary.append("downloaded")
                rows.append({
                    "lead": lead,
                    "lead_color": COLORS["jukebox_key_hi"] if (is_current or lead == "●")
                    else COLORS["text_dim"],
                    "primary": e.title,
                    "secondary": " · ".join(secondary),
                    "trail": format_duration(e.duration_ms) if e.duration_ms else "",
                    "color": COLORS["text_dim"] if e.played and not is_current else COLORS["text"],
                    "bold": is_current,
                    "episode_id": e.id,
                    "played": e.played,
                    "saved": bool(e.local_path),
                    "notes": e.description or "",
                })
        self.list.set_rows(rows)
        if rows:
            self.list.setCurrentRow(current_row)
            self.list.scrollToItem(self.list.item(current_row), QAbstractItemView.PositionAtCenter)
        self._sync()

    def _sync(self) -> None:
        payload = self.list.current_payload() or {}
        has = bool(payload)
        busy = self._download is not None and self._download.isRunning()
        self.play_btn.setEnabled(has)
        self.toggle_btn.setEnabled(has)
        self.toggle_btn.setText("Mark not heard" if payload.get("played") else "Mark heard")
        if busy:
            self.download_btn.setText("Cancel download")
            self.download_btn.setEnabled(True)
        else:
            self.download_btn.setText("Delete download" if payload.get("saved") else "Download")
            self.download_btn.setEnabled(has)
        self.notes.setPlainText(payload.get("notes") or "")

    def _toggle_played(self) -> None:
        payload = self.list.current_payload() or {}
        ep_id = payload.get("episode_id")
        if ep_id is None:
            return
        with self.ctx.session() as s:
            ep = s.get(PodcastEpisode, ep_id)
            if ep is not None:
                podcasts.mark_played(s, ep, not ep.played)
                s.commit()
        row = self.list.currentRow()
        self.refresh()
        self.list.setCurrentRow(row)

    def _toggle_download(self) -> None:
        if self._download is not None and self._download.isRunning():
            self._download.requestInterruption()
            return
        payload = self.list.current_payload() or {}
        ep_id = payload.get("episode_id")
        if ep_id is None:
            return
        with self.ctx.session() as s:
            ep = s.get(PodcastEpisode, ep_id)
            feed = s.get(PodcastFeed, self.feed_id)
            if ep is None or feed is None:
                return
            if ep.local_path:
                playing = self.ctx.player.current
                if playing is not None and playing.path == ep.local_path:
                    self.status.setText("That episode is playing from the download - "
                                        "stop it first")
                    return
                podcasts.delete_download(s, ep)
                s.commit()
                self.status.setText("Download deleted - it streams again")
                row = self.list.currentRow()
                self.refresh()
                self.list.setCurrentRow(row)
                return
            dest = podcasts.download_path(podcasts.get_root(s), feed, ep)
            url = ep.audio_url
        self.status.setText(f"Downloading to {dest.parent}…")
        self._download = DownloadThread(ep_id, url, dest, self)
        self._download.progress.connect(self._on_progress)
        self._download.finished_with.connect(self._on_downloaded)
        self._download.start()
        self._sync()

    def _on_progress(self, done: int, total: int) -> None:
        mb = done / 1_000_000
        if total:
            self.status.setText(f"Downloading… {mb:.1f} of {total / 1_000_000:.1f} MB")
        else:
            self.status.setText(f"Downloading… {mb:.1f} MB")

    def _on_downloaded(self, episode_id: int, error: str) -> None:
        if error == "cancelled":
            self.status.setText("Download cancelled")
        elif error:
            self.status.setText(error)
        else:
            self.status.setText("Downloaded - it plays from the Podcast folder now")
        row = self.list.currentRow()
        self.refresh()
        self.list.setCurrentRow(row)

    def _chosen(self, payload) -> None:
        if payload and payload.get("episode_id") is not None:
            self.episodeChosen.emit(payload["episode_id"])
            self.accept()

    def done(self, result: int) -> None:  # noqa: D102
        if self._download is not None and self._download.isRunning():
            self._download.requestInterruption()
            self._download.wait(5000)
        super().done(result)
