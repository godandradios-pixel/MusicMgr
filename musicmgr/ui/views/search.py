"""Single search box across artists, releases and tracks."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...services import library as lib
from ...services.library import format_duration
from ..context import AppContext
from ..theme import COLORS
from ..widgets.common import SearchBar, TouchButton, TouchList, dim_label
from .base import BaseView


class SearchView(BaseView):
    title_text = "Search"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        self._tracks: list = []

        self.search_bar = SearchBar("Search artists, albums and tracks")
        self.search_bar.textChanged.connect(self._schedule)
        self.body().addWidget(self.search_bar)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(220)
        self._timer.timeout.connect(self._run_search)

        columns = QHBoxLayout()
        columns.setSpacing(16)

        self.artist_list = TouchList()
        self.artist_list.itemActivatedPayload.connect(self._play_artist)
        columns.addLayout(self._column("Artists", self.artist_list, "artist_head"), 2)

        self.release_list = TouchList()
        self.release_list.itemActivatedPayload.connect(self._play_release)
        columns.addLayout(self._column("Releases", self.release_list, "release_head"), 3)

        self.track_list = TouchList()
        self.track_list.itemActivatedPayload.connect(self._play_track)
        columns.addLayout(self._column("Tracks", self.track_list, "track_head"), 4)

        self.body().addLayout(columns, 1)

        hint = dim_label("Tap any result to play it. Tracks queue the whole result set.")
        self.body().addWidget(hint)

    def _column(self, heading: str, widget: QWidget, attr: str) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(8)
        label = QLabel(heading)
        label.setObjectName("Crumb")
        setattr(self, attr, label)
        layout.addWidget(label)
        layout.addWidget(widget, 1)
        return layout

    def refresh(self) -> None:
        if self.search_bar.text():
            self._run_search()

    def focus_input(self) -> None:
        self.search_bar.edit.setFocus()

    def _schedule(self, _text: str) -> None:
        self._timer.start()

    def _run_search(self) -> None:
        query = self.search_bar.text().strip()
        if len(query) < 2:
            self.artist_list.set_rows([])
            self.release_list.set_rows([])
            self.track_list.set_rows([])
            self._tracks = []
            return
        with self.ctx.session() as session:
            results = lib.search(session, query, limit=150)
            artist_rows = [
                {
                    "primary": a.name,
                    "secondary": f"{lib.artist_track_count(session, a.id)} tracks",
                    "key": a.id,
                }
                for a in results["artists"]
            ]
            release_rows = [
                {
                    "primary": r.title,
                    "secondary": f"{r.year or '—'} · {r.artist_display or ''}",
                    "key": r.id,
                }
                for r in results["releases"]
            ]
            self._tracks = list(results["tracks"])
            track_rows = []
            for track in self._tracks:
                mf = track.primary_file
                track_rows.append({
                    "primary": track.title,
                    "secondary": track.artist_display or "",
                    "trail": format_duration(track.duration_ms),
                    "color": COLORS["text_dim"] if (mf is None or mf.is_missing)
                    else COLORS["text"],
                    "key": track.id,
                })
        self.artist_head.setText(f"Artists ({len(artist_rows)})")
        self.release_head.setText(f"Releases ({len(release_rows)})")
        self.track_head.setText(f"Tracks ({len(track_rows)})")
        self.artist_list.set_rows(artist_rows)
        self.release_list.set_rows(release_rows)
        self.track_list.set_rows(track_rows)

    # -- actions -------------------------------------------------------------

    def _play_artist(self, payload: Optional[dict]) -> None:
        if not payload:
            return
        with self.ctx.session() as session:
            tracks = lib.list_tracks(session, artist_id=payload["key"])
            self.ctx.play_tracks(tracks, source="search")

    def _play_release(self, payload: Optional[dict]) -> None:
        if not payload:
            return
        with self.ctx.session() as session:
            tracks = lib.list_tracks(session, release_id=payload["key"])
            self.ctx.play_tracks(tracks, source="search")

    def _play_track(self, payload: Optional[dict]) -> None:
        if not payload:
            return
        for idx, track in enumerate(self._tracks):
            if track.id == payload.get("key"):
                self.ctx.play_tracks(self._tracks, start=idx, source="search")
                return
