"""Radio page (2026-10-01): an antique multi-band radio for old-time radio
shows and internet stations.

James's request: "Internet radio and old-time radio shows. A tuner page for
streaming stations, styled as an antique radio dial to match the
turntable. It could include old-time radio show archives alongside regular
stations. I have a whole set of old radio programs." His choices: shows
resume in air-date order, an On-Air "broadcast night" station, stations
from presets plus a directory search, and (after a round dial was tried
and dropped) one slide-rule glass with several bands, like a 1950s
European set.

The set is ui/widgets/radio_dial.py; what tuning does is services/tuner.py
(shows: services/otr.py, stations: services/stations.py).

Bands, "by type of program" (guessed from folder names, changeable with a
show's or station's Band... button): FM internet stations, AM drama and
comedy, POLICE crime and mystery, SW1 WWII and world news, SW2 history,
speeches and Paul Harvey, LW On the Air and commercials.

Below the set, a card describes what's tuned in (or what the pointer rests
on), with the actions that go with it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ... import config
from ...db.models import RadioEpisode, RadioShow, RadioStation, Setting
from ...db.session import new_session
from ...services import otr, stations
from ...services.library import format_duration
from ..context import AppContext
from ..theme import COLORS, make_compact
from ..widgets.common import ChipButton, CoverArt, SearchBar, TouchButton, TouchList, dim_label
from ..widgets.radio_dial import BAND_KEYS, BANDS, BY_KEY, RadioSet
from .base import BaseView

log = logging.getLogger(__name__)

STATIC_KEY = "tuner_static"
BAND_KEY = "tuner_band"
ONAIR_LABEL = "On the Air"
BAND_BLURBS = {
    "fm": "Internet stations",
    "am": "Drama and comedy",
    "police": "Crime and mystery",
    "sw1": "WWII and world news",
    "sw2": "History, speeches and Paul Harvey",
    "lw": "On the Air and commercials",
}


def _get_setting(ctx: AppContext, key: str, default: str) -> str:
    with ctx.session() as s:
        row = s.get(Setting, key)
        return row.value if row and row.value else default


def _set_setting(ctx: AppContext, key: str, value: str) -> None:
    with ctx.session() as s:
        row = s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value
        s.commit()


# --------------------------------------------------------------------------
# background work
# --------------------------------------------------------------------------


class OtrScanThread(QThread):
    progress = Signal(int, int, str)
    finished_with = Signal(object)

    def __init__(self, root: str, parent=None) -> None:
        super().__init__(parent)
        self.root = root

    def run(self) -> None:  # pragma: no cover - exercised interactively
        try:
            result = otr.scan(
                new_session, self.root,
                progress=lambda d, t, n: self.progress.emit(d, t, n),
                cancelled=self.isInterruptionRequested,
            )
        except Exception as exc:
            log.warning("old-time radio scan failed: %s", exc)
            result = None
        self.finished_with.emit(result)


class StationSearchThread(QThread):
    finished_with = Signal(object, str)  # list[Found], error text

    def __init__(self, text: str, params: Optional[dict], parent=None) -> None:
        super().__init__(parent)
        self.text = text
        self.params = params

    def run(self) -> None:  # pragma: no cover - network
        try:
            self.finished_with.emit(stations.search(self.text, self.params), "")
        except Exception as exc:
            self.finished_with.emit([], str(exc))


class LogoThread(QThread):
    def __init__(self, station_id: int, url: str, parent=None) -> None:
        super().__init__(parent)
        self.station_id = station_id
        self.url = url

    def run(self) -> None:  # pragma: no cover - network
        path = stations.fetch_logo(self.station_id, self.url)
        if path:
            from ...db.session import session_scope

            with session_scope() as s:
                st = s.get(RadioStation, self.station_id)
                if st is not None:
                    st.favicon_path = path


# --------------------------------------------------------------------------
# dialogs
# --------------------------------------------------------------------------


class EpisodesDialog(QDialog):
    """Every broadcast of a show in air-date order: tap one to play from
    there; mark played/unplayed."""

    episodeChosen = Signal(int)

    def __init__(self, ctx: AppContext, show_id: int, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.show_id = show_id
        self.setMinimumSize(720, 640)
        layout = QVBoxLayout(self)
        self.header = QLabel("")
        self.header.setObjectName("Title")
        layout.addWidget(self.header)
        self.sub = dim_label("")
        layout.addWidget(self.sub)
        self.list = TouchList()
        self.list.itemActivatedPayload.connect(self._chosen)
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.toggle_btn = TouchButton("Mark played")
        self.toggle_btn.clicked.connect(self._toggle_played)
        row.addWidget(self.toggle_btn)
        row.addStretch(1)
        close = TouchButton("Close", primary=True)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        self.list.currentItemChanged.connect(lambda *_: self._sync_toggle())
        self.refresh()

    def refresh(self) -> None:
        with self.ctx.session() as s:
            show = s.get(RadioShow, self.show_id)
            if show is None:
                return
            self.setWindowTitle(show.name)
            self.header.setText(show.name)
            eps = otr.episodes(s, self.show_id)
            current = otr.current_episode(s, show)
            heard = sum(1 for e in eps if e.played)
            self.sub.setText(f"{len(eps):,} broadcasts · {heard:,} heard")
            rows = []
            current_row = 0
            for i, e in enumerate(eps):
                is_current = current is not None and e.id == current.id
                if is_current:
                    current_row = i
                lead = "▶" if is_current else ("✓" if e.played else ("◐" if e.position_ms else ""))
                date = otr.format_air_date(e.air_date)
                bits = [date] if date else []
                if e.episode_no:
                    bits.append(f"#{e.episode_no}")
                rows.append({
                    "lead": lead,
                    "lead_color": COLORS["jukebox_key_hi"] if is_current else COLORS["text_dim"],
                    "primary": e.title,
                    "secondary": " · ".join(bits),
                    "trail": format_duration(e.duration_ms),
                    "color": COLORS["text_dim"] if e.played and not is_current else COLORS["text"],
                    "bold": is_current,
                    "episode_id": e.id,
                    "played": e.played,
                })
        self.list.set_rows(rows)
        if rows:
            self.list.setCurrentRow(current_row)
            self.list.scrollToItem(self.list.item(current_row), QAbstractItemView.PositionAtCenter)
        self._sync_toggle()

    def _sync_toggle(self) -> None:
        payload = self.list.current_payload() or {}
        self.toggle_btn.setEnabled(bool(payload))
        self.toggle_btn.setText("Mark unplayed" if payload.get("played") else "Mark played")

    def _toggle_played(self) -> None:
        payload = self.list.current_payload() or {}
        ep_id = payload.get("episode_id")
        if ep_id is None:
            return
        with self.ctx.session() as s:
            ep = s.get(RadioEpisode, ep_id)
            if ep is not None:
                otr.mark_played(s, ep, not ep.played)
                s.commit()
        row = self.list.currentRow()
        self.refresh()
        self.list.setCurrentRow(row)

    def _chosen(self, payload) -> None:
        if payload and payload.get("episode_id") is not None:
            self.episodeChosen.emit(payload["episode_id"])
            self.accept()


class StationSearchDialog(QDialog):
    """Find internet stations in the Radio Browser directory and put them on
    the dial (or take them off)."""

    changed = Signal()
    listenRequested = Signal(int)

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("Find stations")
        self.setMinimumSize(820, 680)
        self._thread: Optional[StationSearchThread] = None
        self._results: list[stations.Found] = []
        layout = QVBoxLayout(self)
        head = QLabel("Find stations")
        head.setObjectName("Title")
        layout.addWidget(head)
        layout.addWidget(dim_label(
            "From the free Radio Browser directory. Tap a station, then add it to the dial."))
        self.search = SearchBar("Station name, town or style")
        self.search.submitted.connect(lambda text: self._run(text, None))
        layout.addWidget(self.search)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        for label, params in stations.CATEGORIES:
            chip = ChipButton(label)
            chip.setCheckable(False)
            chip.clicked.connect(lambda _=False, p=params: self._run("", p))
            chips.addWidget(chip)
        chips.addStretch(1)
        layout.addLayout(chips)
        self.status = dim_label("")
        layout.addWidget(self.status)
        self.list = TouchList()
        self.list.currentItemChanged.connect(lambda *_: self._sync())
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.listen_btn = TouchButton("Listen")
        self.listen_btn.clicked.connect(self._listen)
        self.add_btn = TouchButton("Add to dial", primary=True)
        self.add_btn.clicked.connect(self._toggle)
        url_btn = TouchButton("Add by address…")
        url_btn.clicked.connect(self._add_url)
        row.addWidget(self.listen_btn)
        row.addWidget(self.add_btn)
        row.addWidget(url_btn)
        row.addStretch(1)
        close = TouchButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)
        self._sync()
        self._run("", stations.CATEGORIES[0][1])

    def _run(self, text: str, params: Optional[dict]) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        self.status.setText("Searching…")
        self._thread = StationSearchThread(text, params, self)
        self._thread.finished_with.connect(self._on_results)
        self._thread.start()

    def _on_results(self, results, error: str) -> None:
        if error:
            self.status.setText(f"Couldn't reach the station directory ({error}).")
            return
        self._results = results
        self.status.setText(f"{len(results)} stations" if results else "No stations found")
        self._refill()

    def _refill(self) -> None:
        row = self.list.currentRow()
        rows = []
        with self.ctx.session() as s:
            for i, f in enumerate(self._results):
                on = stations.on_dial(s, f) is not None
                rows.append({
                    "lead": "✓" if on else "",
                    "lead_color": COLORS["jukebox_key_hi"],
                    "primary": f.name,
                    "secondary": " · ".join(x for x in (f.detail, f.tags[:60]) if x),
                    "trail": "On the dial" if on else "",
                    "index": i,
                    "on": on,
                })
        self.list.set_rows(rows)
        if 0 <= row < len(rows):
            self.list.setCurrentRow(row)
        self._sync()

    def _current(self) -> Optional[stations.Found]:
        payload = self.list.current_payload() or {}
        i = payload.get("index")
        return self._results[i] if i is not None and i < len(self._results) else None

    def _sync(self) -> None:
        payload = self.list.current_payload() or {}
        has = bool(payload)
        self.listen_btn.setEnabled(has)
        self.add_btn.setEnabled(has)
        self.add_btn.setText("Remove from dial" if payload.get("on") else "Add to dial")

    def _toggle(self) -> None:
        f = self._current()
        if f is None:
            return
        with self.ctx.session() as s:
            st = stations.on_dial(s, f)
            if st is not None:
                stations.remove(s, st.id)
                new_id = None
            else:
                st = stations.add(s, f)
                new_id = st.id
            s.commit()
        if new_id and f.favicon:
            LogoThread(new_id, f.favicon, self.parent()).start()
        self.changed.emit()
        self._refill()

    def _listen(self) -> None:
        f = self._current()
        if f is None:
            return
        with self.ctx.session() as s:
            st = stations.on_dial(s, f) or stations.add(s, f)
            s.commit()
            sid = st.id
        self.changed.emit()
        self._refill()
        self.listenRequested.emit(sid)

    def _add_url(self) -> None:
        url, ok = QInputDialog.getText(self, "Add a station", "Stream address (http://…):")
        if not ok or not url.strip().lower().startswith(("http://", "https://")):
            return
        name, ok = QInputDialog.getText(self, "Add a station", "Name for the dial:")
        if not ok:
            return
        with self.ctx.session() as s:
            stations.add_manual(s, name or url, url)
            s.commit()
        self.changed.emit()
        self.ctx.notify(f"Added {name or url} to the dial")


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------


class TunerView(BaseView):
    title_text = "Radio"

    def __init__(self, ctx: AppContext, parent=None) -> None:
        super().__init__(ctx, parent)
        #: band -> [(kind, id, dial label)]
        self._entries: dict[str, list[tuple[str, int, str]]] = {k: [] for k in BAND_KEYS}
        self._scan_thread: Optional[OtrScanThread] = None
        self._stream_title = ""
        self._initialised = False
        self._live = False

        self.find_btn = make_compact(TouchButton("Find stations…"))
        self.find_btn.clicked.connect(self.find_stations)
        self.rescan_btn = make_compact(TouchButton("Rescan shows"))
        self.rescan_btn.clicked.connect(self.rescan)
        self.header.addWidget(self.find_btn)
        self.header.addWidget(self.rescan_btn)

        self.radio = RadioSet()
        self.radio.set_static_path(config.DATA_DIR / "tuner_static.wav")
        self.radio.tuneRequested.connect(self._on_tune_requested)
        self.radio.bandRequested.connect(self.set_band)
        self.radio.volumeRequested.connect(ctx.player.set_volume)
        self.radio.contextRequested.connect(self._dial_menu)
        self.radio.set_volume(ctx.player.volume)
        self.body().addWidget(self.radio, 1)

        # info card
        card = QFrame()
        card.setObjectName("Card")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(16)
        self.cover = CoverArt(112)
        cl.addWidget(self.cover, 0, Qt.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(3)
        self.info_name = QLabel("")
        self.info_name.setStyleSheet("font-size: 22px; font-weight: 700;")
        self.info_title = QLabel("")
        self.info_title.setStyleSheet("font-size: 17px;")
        self.info_title.setWordWrap(True)
        self.info_sub = dim_label("")
        self.info_sub.setWordWrap(True)
        self.info_extra = dim_label("")
        for w in (self.info_name, self.info_title, self.info_sub, self.info_extra):
            text.addWidget(w)
        self.progress = QProgressBar()
        self.progress.setObjectName("ProgressWarm")
        self.progress.setVisible(False)
        self.progress.setMaximumHeight(8)
        self.progress.setTextVisible(False)
        text.addWidget(self.progress)
        text.addStretch(1)
        cl.addLayout(text, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        actions_box = QWidget()
        actions_box.setStyleSheet("background: transparent;")
        actions_box.setLayout(self.actions)
        cl.addWidget(actions_box, 0, Qt.AlignBottom)
        self.body().addWidget(card, 0)

        self.btn_listen = make_compact(TouchButton("Listen ▶", primary=True))
        self.btn_listen.clicked.connect(self._listen_pointed)
        self.btn_episodes = make_compact(TouchButton("Episodes…"))
        self.btn_episodes.clicked.connect(self._open_episodes)
        self.btn_prev = make_compact(TouchButton("◀ Previous"))
        self.btn_prev.clicked.connect(self._previous_episode)
        self.btn_next = make_compact(TouchButton("Next ▶"))
        self.btn_next.clicked.connect(lambda: ctx.player.next(user_initiated=True))
        self.btn_new_evening = make_compact(TouchButton("New evening"))
        self.btn_new_evening.clicked.connect(ctx.tuner.tune_onair)
        self.btn_band = make_compact(TouchButton("Band…"))
        self.btn_band.setToolTip("Move to another band of the dial")
        self.btn_band.clicked.connect(self._band_menu)
        self.btn_move_left = make_compact(TouchButton("◀ Move"))
        self.btn_move_left.clicked.connect(lambda: self._move_station(-1))
        self.btn_move_right = make_compact(TouchButton("Move ▶"))
        self.btn_move_right.clicked.connect(lambda: self._move_station(1))
        self.btn_remove = make_compact(TouchButton("Remove"))
        self.btn_remove.setToolTip("Take this station off the dial")
        self.btn_remove.clicked.connect(self._remove_station)
        self.btn_folder = make_compact(TouchButton("Choose folder…", primary=True))
        self.btn_folder.clicked.connect(self.choose_folder)
        self.btn_starters = make_compact(TouchButton("Add old-time radio stations", primary=True))
        self.btn_starters.clicked.connect(self._add_starters)
        self.btn_find = make_compact(TouchButton("Find stations…"))
        self.btn_find.clicked.connect(self.find_stations)
        for b in self._all_buttons():
            self.actions.addWidget(b)
            b.setVisible(False)

        ctx.tuner.tunedChanged.connect(self._on_tuned)
        ctx.tuner.onAirYearChanged.connect(lambda _y: self._update_info())
        ctx.player.trackChanged.connect(lambda _i: self._update_info())
        ctx.player.streamTitleChanged.connect(self._on_stream_title)
        ctx.player.playbackStateChanged.connect(self._on_state)
        ctx.player.volumeChanged.connect(self.radio.set_volume)

    # -- setup -------------------------------------------------------------

    def refresh(self) -> None:
        self._rebuild()
        if not self._initialised:
            self._initialised = True
            self.radio.set_static_enabled(_get_setting(self.ctx, STATIC_KEY, "1") == "1")
            kind, ident = self.ctx.tuner.tuned
            if not kind:
                kind, ident = self.ctx.tuner.last_tuned()
            if not self._point_at(kind, ident, animate=False):
                self.radio.set_band(_get_setting(self.ctx, BAND_KEY, "am"), animate=False)
        self._update_info()

    def set_band(self, band: str) -> None:
        if band == self.radio.band:
            return
        self.radio.set_band(band)
        _set_setting(self.ctx, BAND_KEY, band)
        self._update_info()

    def _rebuild(self) -> None:
        entries: dict[str, list[tuple[str, int, str]]] = {k: [] for k in BAND_KEYS}
        with self.ctx.session() as s:
            shows = otr.list_shows(s)
            if shows:
                entries["lw"].append(("onair", 0, ONAIR_LABEL))
            for sh in shows:
                band = sh.band if sh.band in entries else "am"
                entries[band].append(("show", sh.id, otr.dial_label(sh.name)))
            for st in stations.dial(s):
                band = st.band if st.band in entries else "fm"
                entries[band].append(("station", st.id, st.name))
        self._entries = entries
        for band, items in entries.items():
            self.radio.set_entries(band, [label for _, _, label in items])

    def _locate(self, kind: str, ident: int) -> tuple[str, int]:
        """(band, index) of a show/station on the dial, or ("", -1)."""
        for band, items in self._entries.items():
            for i, (k, n, _) in enumerate(items):
                if k == kind and n == ident:
                    return band, i
        return "", -1

    def _point_at(self, kind: str, ident: int, animate: bool) -> bool:
        band, idx = self._locate(kind, ident)
        if idx >= 0:
            self.radio.set_tuned(band, idx, animate=animate)
            return True
        return False

    # -- tuning ------------------------------------------------------------

    def _on_tune_requested(self, band: str, index: int) -> None:
        entries = self._entries.get(band, [])
        if not (0 <= index < len(entries)):
            return
        kind, ident, _ = entries[index]
        if self.ctx.tuner.tuned == (kind, ident) and self.ctx.player.is_playing():
            self._update_info()
            return
        self._tune(kind, ident)

    def _tune(self, kind: str, ident: int) -> None:
        if kind == "onair":
            self.ctx.tuner.tune_onair()
        elif kind == "show":
            self.ctx.tuner.tune_show(ident)
        elif kind == "station":
            self._stream_title = ""
            self.ctx.tuner.tune_station(ident)

    def _on_tuned(self, kind: str, ident: int) -> None:
        if kind:
            self._point_at(kind, ident, animate=True)
        self._update_info()

    def _on_stream_title(self, title: str) -> None:
        self._stream_title = title
        self._update_info()

    def _on_state(self, state: str) -> None:
        self.radio.set_playing(state == "playing" and bool(self.ctx.tuner.tuned[0]))

    # -- info card ---------------------------------------------------------

    def _all_buttons(self):
        return (self.btn_listen, self.btn_episodes, self.btn_prev, self.btn_next,
                self.btn_new_evening, self.btn_band, self.btn_move_left, self.btn_move_right,
                self.btn_remove, self.btn_folder, self.btn_starters, self.btn_find)

    def _show_buttons(self, *buttons) -> None:
        for b in self._all_buttons():
            b.setVisible(b in buttons)

    def _pointed(self) -> tuple[str, int, bool]:
        """What the card describes: what's tuned in on the selected band
        (live=True), else the station the pointer rests on there."""
        band = self.radio.band
        entries = self._entries.get(band, [])
        kind, ident = self.ctx.tuner.tuned
        if kind and self._locate(kind, ident)[0] == band:
            return kind, ident, True
        idx = self.radio.tuned_index
        if 0 <= idx < len(entries):
            k, n, _ = entries[idx]
            return k, n, False
        return "", 0, False

    def _listen_pointed(self) -> None:
        kind, ident, _ = self._pointed()
        if kind:
            self._tune(kind, ident)

    def _update_info(self) -> None:
        kind, ident, live = self._pointed()
        self._live = live
        band = self.radio.band
        self.progress.setVisible(False)
        if not self._entries.get(band):
            self._empty_band(band)
        elif kind == "show":
            self._info_show(ident)
        elif kind == "onair":
            self._info_onair()
        elif kind == "station":
            self._info_station(ident)
        else:
            self.cover.set_source(None, "Radio")
            self.info_name.setText(f"{BY_KEY[band].label} band")
            self.info_title.setText("Tap a name on the lit band, drag the pointer, or turn the "
                                    "TUNING knob.")
            self.info_sub.setText(BAND_BLURBS.get(band, ""))
            self.info_extra.setText("")
            self._show_buttons()
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self.progress.setVisible(True)

    def _empty_band(self, band: str) -> None:
        self.cover.set_source(None, BY_KEY[band].label)
        self.info_extra.setText("")
        with self.ctx.session() as s:
            root = otr.get_root(s)
            any_shows = bool(otr.list_shows(s))
        if band == "fm":
            self.info_name.setText("No stations on the FM band yet")
            self.info_title.setText("Find internet stations in the free Radio Browser directory, "
                                    "or start with a few old-time radio stations.")
            self.info_sub.setText(BAND_BLURBS["fm"])
            self._show_buttons(self.btn_starters, self.btn_find)
            return
        if not any_shows:
            self.info_name.setText("Old-time radio")
            if root:
                self.info_title.setText(f"No shows found in {root}")
                self.info_sub.setText("Each show should be a folder of episodes inside it.")
            else:
                self.info_title.setText("Choose the folder that holds your radio programs")
                self.info_sub.setText("One folder per show (The Shadow, Lone Ranger, …).")
            self._show_buttons(self.btn_folder)
            return
        self.info_name.setText(f"Nothing on {BY_KEY[band].label} yet")
        self.info_title.setText(BAND_BLURBS.get(band, ""))
        self.info_sub.setText("Move a show or station here with its Band… button.")
        self._show_buttons()

    def _info_show(self, show_id: int) -> None:
        item = self.ctx.player.current
        with self.ctx.session() as s:
            show = s.get(RadioShow, show_id)
            if show is None:
                return
            heard, total = otr.progress_counts(s, show_id)
            ep = None
            if self._live and item is not None and item.kind == "episode" and item.episode_id:
                ep = s.get(RadioEpisode, item.episode_id)
            if ep is None:
                ep = otr.current_episode(s, show)
            self.cover.set_source(show.cover_path, show.name)
            self.info_name.setText(show.name)
            self.info_title.setText(ep.title if ep else "")
            bits = []
            if ep is not None:
                if ep.air_date:
                    bits.append(f"Broadcast {otr.format_air_date(ep.air_date)}")
                if ep.episode_no:
                    bits.append(f"Episode {ep.episode_no}")
            self.info_sub.setText(" · ".join(bits))
            resume = ""
            if not self._live and ep is not None and ep.position_ms:
                resume = f" · picks up at {format_duration(ep.position_ms)}"
            self.info_extra.setText(f"{heard:,} of {total:,} broadcasts heard{resume}")
            self.progress.setVisible(total > 0)
            self.progress.setRange(0, max(1, total))
            self.progress.setValue(heard)
        if self._live:
            self._show_buttons(self.btn_episodes, self.btn_prev, self.btn_next, self.btn_band)
        else:
            self._show_buttons(self.btn_listen, self.btn_episodes, self.btn_band)

    def _info_onair(self) -> None:
        item = self.ctx.player.current
        year = self.ctx.tuner.onair_year
        self.info_extra.setText("Programs from your collection, with commercials and news "
                                "bulletins between them")
        if not self._live:
            self.cover.set_source(None, "On the Air")
            self.info_name.setText("On the Air")
            self.info_title.setText("A night of radio from your collection")
            self.info_sub.setText("")
            self._show_buttons(self.btn_listen)
            return
        self.cover.set_source(item.cover_path if item else None, "On the Air")
        self.info_name.setText(f"On the Air — an evening in {year}" if year else "On the Air")
        if item is not None and item.kind == "episode":
            self.info_title.setText(f"{item.artist}: {item.title}")
            self.info_sub.setText(item.album)
        else:
            self.info_title.setText("")
            self.info_sub.setText("")
        self._show_buttons(self.btn_next, self.btn_new_evening)

    def _info_station(self, station_id: int) -> None:
        with self.ctx.session() as s:
            st = s.get(RadioStation, station_id)
            if st is None:
                return
            self.cover.set_source(st.favicon_path, st.name)
            self.info_name.setText(st.name)
            self.info_title.setText(self._stream_title if self._live else "")
            self.info_sub.setText(" · ".join(x for x in (
                st.country, (st.codec or "").upper(),
                f"{st.bitrate} kbps" if st.bitrate else "") if x))
            self.info_extra.setText(st.homepage or "")
        buttons = [self.btn_band, self.btn_move_left, self.btn_move_right, self.btn_remove]
        if not self._live:
            buttons.insert(0, self.btn_listen)
        self._show_buttons(*buttons)

    # -- actions -----------------------------------------------------------

    def choose_folder(self) -> None:  # pragma: no cover - file dialog
        with self.ctx.session() as s:
            start = otr.get_root(s) or str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Old-time radio folder", start)
        if not folder:
            return
        with self.ctx.session() as s:
            otr.set_root(s, folder)
            s.commit()
        self.rescan()

    def rescan(self) -> None:
        with self.ctx.session() as s:
            root = otr.get_root(s)
        if not root:
            self.choose_folder()
            return
        if self._scan_thread is not None and self._scan_thread.isRunning():
            return
        self.info_extra.setText("Reading your radio programs…")
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self._scan_thread = OtrScanThread(root, self)
        self._scan_thread.progress.connect(self._on_scan_progress)
        self._scan_thread.finished_with.connect(self._on_scan_done)
        self._scan_thread.start()

    def _on_scan_progress(self, done: int, total: int, name: str) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        self.info_extra.setText(f"Reading {name}… {done:,} of {total:,}")

    def _on_scan_done(self, result) -> None:
        self.progress.setVisible(False)
        self.ctx.notify(f"Old-time radio: {result.summary()}" if result else
                        "Couldn't read the old-time radio folder")
        self._changed()

    def _changed(self) -> None:
        self._rebuild()
        kind, ident = self.ctx.tuner.tuned
        self._point_at(kind, ident, animate=False)
        self._update_info()

    def find_stations(self) -> None:  # pragma: no cover - dialog
        dialog = StationSearchDialog(self.ctx, self)
        dialog.changed.connect(self._changed)
        dialog.listenRequested.connect(self.ctx.tuner.tune_station)
        dialog.exec()

    def _add_starters(self) -> None:
        self.ctx.notify("Looking up old-time radio stations…")
        thread = StationSearchThread("", stations.STARTER_SEARCH, self)

        def done(results, error: str) -> None:
            if error or not results:
                self.ctx.notify("Couldn't reach the station directory — try Find stations later")
                return
            with self.ctx.session() as s:
                added = [stations.add(s, f) for f in results[: stations.STARTER_COUNT]]
                s.commit()
                logos = [(st.id, st.favicon_url) for st in added if st.favicon_url]
            for sid, url in logos:
                LogoThread(sid, url, self).start()
            self._changed()
            self.ctx.notify(f"{len(added)} stations added to the FM band")

        thread.finished_with.connect(done)
        thread.start()
        self._starter_thread = thread

    def _open_episodes(self) -> None:  # pragma: no cover - dialog
        kind, ident, _ = self._pointed()
        if kind != "show":
            return
        dialog = EpisodesDialog(self.ctx, ident, self)
        dialog.episodeChosen.connect(lambda ep: self.ctx.tuner.tune_show(ident, episode_id=ep))
        dialog.exec()
        self._update_info()

    def _previous_episode(self) -> None:
        player = self.ctx.player
        if player.position() > 4000:
            player.seek(0)
            return
        kind, ident = self.ctx.tuner.tuned
        item = player.current
        if kind != "show" or item is None or item.episode_id is None:
            return
        with self.ctx.session() as s:
            eps = otr.episodes(s, ident)
            idx = next((i for i, e in enumerate(eps) if e.id == item.episode_id), None)
            prev_id = eps[idx - 1].id if idx else None
        if prev_id is not None:
            self.ctx.tuner.tune_show(ident, episode_id=prev_id)

    def move_to_band(self, band: str) -> None:
        kind, ident, _ = self._pointed()
        with self.ctx.session() as s:
            if kind == "show":
                otr.set_band(s, ident, band)
            elif kind == "station":
                stations.set_band(s, ident, band)
            else:
                return
            s.commit()
        self._rebuild()
        b, idx = self._locate(kind, ident)
        if idx >= 0:
            self.radio.set_tuned(b, idx, animate=True)
        self._update_info()

    def _band_menu(self) -> None:  # pragma: no cover - menu
        menu = QMenu(self)
        for band in BANDS:
            action = menu.addAction(f"{band.label} — {BAND_BLURBS.get(band.key, '')}")
            action.setCheckable(True)
            action.setChecked(band.key == self.radio.band)
            action.triggered.connect(lambda _=False, k=band.key: self.move_to_band(k))
        menu.exec(self.btn_band.mapToGlobal(self.btn_band.rect().topLeft()))

    def _move_station(self, delta: int) -> None:
        kind, ident, _ = self._pointed()
        if kind != "station":
            return
        with self.ctx.session() as s:
            stations.move(s, ident, delta)
            s.commit()
        self._changed()

    def _remove_station(self) -> None:
        kind, ident, live = self._pointed()
        if kind != "station":
            return
        with self.ctx.session() as s:
            st = s.get(RadioStation, ident)
            name = st.name if st else "station"
            stations.remove(s, ident)
            s.commit()
        if live:
            self.ctx.player.stop()
        self.ctx.notify(f"Took {name} off the dial")
        self._changed()

    def _dial_menu(self, global_pos) -> None:  # pragma: no cover - menu
        menu = QMenu(self)
        static = menu.addAction("Tuning static")
        static.setCheckable(True)
        static.setChecked(_get_setting(self.ctx, STATIC_KEY, "1") == "1")

        def toggle_static(on: bool) -> None:
            _set_setting(self.ctx, STATIC_KEY, "1" if on else "0")
            self.radio.set_static_enabled(on)

        static.toggled.connect(toggle_static)
        menu.addSeparator()
        menu.addAction("Old-time radio folder…").triggered.connect(self.choose_folder)
        menu.addAction("Rescan shows").triggered.connect(self.rescan)
        menu.addAction("Find stations…").triggered.connect(self.find_stations)
        menu.exec(global_pos)

    def shutdown(self) -> None:
        if self._scan_thread is not None and self._scan_thread.isRunning():
            self._scan_thread.requestInterruption()
            self._scan_thread.wait(5000)
