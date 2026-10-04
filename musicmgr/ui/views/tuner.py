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

Bands (guessed from folder names, changeable with a show's or station's
Band... button). First layout was "by type of program"; reorganised the
same day at James's request ("Let's leave FM to allow enter on URL
streams, Then use the AM for the same URL streams. For the Police band,
let's do the WWII NEWS & SOUNDS with ON THE AIR and AMERICAN HISTORY. On
the SW1 we can move what was in the AM band, On SW2 we can move what was
on the POLICE band"): FM and AM internet stations, POLICE On the Air, WWII
news and American history, SW1 drama and comedy, SW2 crime and mystery,
LW speeches, Paul Harvey and commercials. Shows already scanned move over
once (services/otr.py:migrate_band_layout).

The page header says, in one line, what's tuned in (or what the pointer
rests on) beyond what the player bar already shows, with the actions that
go with it beside it. (Until 2026-10-03 this was a card under the set.)

2026-10-03: FM/AM stations sit at the frequency in their name; Move ◀/▶
only applies to internet-only stations (the others are placed by their
frequency). The magic eye follows the stream's signal, and a station
that's gone off the air is faded on the glass with a "Find a new link…"
button in the header (ReplacementDialog).
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
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
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
from ..widgets.common import ChipButton, SearchBar, TouchButton, TouchList, dim_label
from ..widgets.player_bar import ElidingLabel
from ..widgets.radio_dial import BAND_KEYS, BANDS, BY_KEY, SIGNAL_NONE, RadioSet
from .base import BaseView

log = logging.getLogger(__name__)

STATIC_KEY = "tuner_static"
BAND_KEY = "tuner_band"
ONAIR_LABEL = "On the Air"
BAND_BLURBS = {
    "fm": "Internet stations",
    "am": "Internet stations",
    "police": "On the Air, WWII news and American history",
    "sw1": "Drama and comedy",
    "sw2": "Crime and mystery",
    "lw": "Speeches, Paul Harvey and commercials",
}
#: the band the page opens on when nothing has been tuned yet
START_BAND = "police"


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


class ReplacementSearchThread(QThread):
    finished_with = Signal(object, str)  # list[Found], error text

    def __init__(self, name: str, rb_uuid: Optional[str], url: str, parent=None) -> None:
        super().__init__(parent)
        self.name, self.rb_uuid, self.url = name, rb_uuid, url

    def run(self) -> None:  # pragma: no cover - network
        try:
            self.finished_with.emit(
                stations.find_replacements(self.name, self.rb_uuid, self.url), "")
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


class ReplacementDialog(QDialog):
    """A new stream link for a station that's gone off the air (2026-10-03):
    the same directory entry again if the station has moved its stream,
    then stations found by its call letters and name. Choosing one keeps
    the station's name, band, place and logo and tunes it in."""

    #: a link was chosen and saved - tune the station in
    replaced = Signal(int)
    #: tests switch the directory lookup off
    auto_search = True

    def __init__(self, ctx: AppContext, station_id: int, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.station_id = station_id
        with ctx.session() as s:
            st = s.get(RadioStation, station_id)
            self.name = st.name if st else ""
            self.url = st.stream_url if st else ""
            rb_uuid = st.rb_uuid if st else None
        self.setWindowTitle(f"New link — {self.name}")
        self.setMinimumSize(760, 560)
        self._results: list[stations.Found] = []
        layout = QVBoxLayout(self)
        head = QLabel(f"Find a new link for {self.name}")
        head.setObjectName("Title")
        layout.addWidget(head)
        layout.addWidget(dim_label(
            "Its stream stopped answering. These are the same station, or ones with the same "
            "call letters or name, from the free Radio Browser directory. The name, band and "
            "place on the dial stay as they are."))
        self.status = dim_label("Searching…")
        layout.addWidget(self.status)
        self.list = TouchList()
        self.list.currentItemChanged.connect(lambda *_: self._sync())
        self.list.itemActivatedPayload.connect(lambda _p: self._use())
        layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.use_btn = TouchButton("Use this link", primary=True)
        self.use_btn.clicked.connect(self._use)
        paste = TouchButton("Paste an address…")
        paste.clicked.connect(self._paste)
        row.addWidget(self.use_btn)
        row.addWidget(paste)
        row.addStretch(1)
        close = TouchButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self._sync()
        self._thread = ReplacementSearchThread(self.name, rb_uuid, self.url, self)
        self._thread.finished_with.connect(self.show_results)
        if self.auto_search:
            self._thread.start()

    def show_results(self, results, error: str = "") -> None:
        if error and not results:
            self.status.setText(f"Couldn't reach the station directory ({error}).")
            return
        self._results = list(results)
        self.status.setText(f"{len(self._results)} possible links" if self._results else
                            "Nothing found — paste the station's stream address instead.")
        self.list.set_rows([{
            "primary": f.name,
            "secondary": " · ".join(x for x in (f.detail, f.tags[:60]) if x),
            "trail": f"{f.votes:,} likes" if f.votes else "",
            "index": i,
        } for i, f in enumerate(self._results)])
        if self._results:
            self.list.setCurrentRow(0)
        self._sync()

    def _sync(self) -> None:
        self.use_btn.setEnabled(bool(self.list.current_payload()))

    def _use(self) -> None:
        payload = self.list.current_payload() or {}
        i = payload.get("index")
        if i is None or i >= len(self._results):
            return
        self.use(self._results[i])

    def use(self, found: "stations.Found") -> None:
        with self.ctx.session() as s:
            stations.replace_link(s, self.station_id, found)
            s.commit()
        self.replaced.emit(self.station_id)
        self.accept()

    def _paste(self) -> None:  # pragma: no cover - dialog
        url, ok = QInputDialog.getText(self, "New link", "Stream address (http://…):")
        if ok and url.strip().lower().startswith(("http://", "https://")):
            self.use(stations.Found(uuid="", name=self.name, url=url.strip()))

    def done(self, result: int) -> None:  # noqa: D102
        if self._thread.isRunning():
            self._thread.finished_with.disconnect()
            self._thread.wait(100)
        super().done(result)


class StationSearchDialog(QDialog):
    """Find internet stations in the Radio Browser directory and put them on
    the dial (or take them off)."""

    changed = Signal()
    listenRequested = Signal(int)

    def __init__(self, ctx: AppContext, parent=None, band: str = "fm") -> None:
        super().__init__(parent)
        self.ctx = ctx
        #: the band (FM or AM) stations added here go on
        self.band = band
        self.setWindowTitle(f"Find stations — {BY_KEY[band].label} band")
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
                st = stations.add(s, f, band=self.band)
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
            st = stations.on_dial(s, f) or stations.add(s, f, band=self.band)
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
            stations.add_manual(s, name or url, url, band=self.band)
            s.commit()
        self.changed.emit()
        self.ctx.notify(f"Added {name or url} to the {BY_KEY[self.band].label} band")


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
        self._off_air: set[int] = set()

        self.find_btn = make_compact(TouchButton("Find stations…"))
        self.find_btn.clicked.connect(self.find_stations)
        self.rescan_btn = make_compact(TouchButton("Rescan shows"))
        self.rescan_btn.clicked.connect(self.rescan)

        # 2026-10-03 - James: "Don't like the redundant 105.9 The X WXDX.
        # Maybe we move the Band Rename Remove to the top and just eliminate
        # that whole block to give more room for the radio dials". The info
        # card under the set is gone: what it said that the player bar
        # doesn't (the song a station is playing, an episode's broadcast
        # date, off the air, what to do on an empty band) is one line in the
        # page header, and its buttons sit beside it.
        while self.header.count() > 1:          # keep the title, drop the stretch
            self.header.takeAt(1)
        self.status = ElidingLabel("")
        self.status.setObjectName("Dim")
        self.status.setMinimumWidth(120)
        self.header.addWidget(self.status, 1)
        self.actions = QHBoxLayout()
        self.actions.setContentsMargins(0, 0, 0, 0)
        self.actions.setSpacing(8)
        actions_box = QWidget()
        actions_box.setStyleSheet("background: transparent;")
        actions_box.setLayout(self.actions)
        self.header.addWidget(actions_box)
        self.header.addSpacing(8)
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
        #: "Reading your radio programs…" while a rescan runs
        self._scan_text = ""

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
        self.btn_rename = make_compact(TouchButton("Rename…"))
        self.btn_rename.setToolTip("Change the name printed on the dial")
        self.btn_rename.clicked.connect(self._ask_rename_station)
        self.btn_move_left = make_compact(TouchButton("◀ Move"))
        self.btn_move_left.clicked.connect(lambda: self._move_station(-1))
        self.btn_move_right = make_compact(TouchButton("Move ▶"))
        self.btn_move_right.clicked.connect(lambda: self._move_station(1))
        self.btn_remove = make_compact(TouchButton("Remove"))
        self.btn_remove.setToolTip("Take this station off the dial")
        self.btn_remove.clicked.connect(self._remove_station)
        self.btn_relink = make_compact(TouchButton("Find a new link…", primary=True))
        self.btn_relink.setToolTip("Look up a working stream for this station")
        self.btn_relink.clicked.connect(self.find_new_link)
        self.btn_folder = make_compact(TouchButton("Choose folder…", primary=True))
        self.btn_folder.clicked.connect(self.choose_folder)
        self.btn_starters = make_compact(TouchButton("Add old-time radio stations", primary=True))
        self.btn_starters.clicked.connect(self._add_starters)
        for b in self._all_buttons():
            self.actions.addWidget(b)
            b.setVisible(False)

        ctx.tuner.tunedChanged.connect(self._on_tuned)
        ctx.tuner.onAirYearChanged.connect(lambda _y: self._update_info())
        ctx.player.trackChanged.connect(lambda _i: self._update_info())
        ctx.player.streamTitleChanged.connect(self._on_stream_title)
        ctx.player.playbackStateChanged.connect(self._on_state)
        ctx.player.volumeChanged.connect(self.radio.set_volume)
        ctx.player.signalChanged.connect(lambda _l: self._sync_signal())
        ctx.tuner.stationAirChanged.connect(lambda _id, _off: self._changed())

    # -- setup -------------------------------------------------------------

    def refresh(self) -> None:
        if not self._initialised:
            try:
                with self.ctx.session() as s:
                    otr.migrate_band_layout(s)
                    s.commit()
            except Exception:  # pragma: no cover - never block the page
                log.exception("couldn't move radio shows to the new band layout")
        self._rebuild()
        self._fetch_missing_logos()
        if not self._initialised:
            self._initialised = True
            self.radio.set_static_enabled(_get_setting(self.ctx, STATIC_KEY, "1") == "1")
            kind, ident = self.ctx.tuner.tuned
            if not kind:
                kind, ident = self.ctx.tuner.last_tuned()
            if not self._point_at(kind, ident, animate=False):
                self.radio.set_band(_get_setting(self.ctx, BAND_KEY, START_BAND), animate=False)
        self._update_info()

    def _fetch_missing_logos(self) -> None:
        """Stations that arrived by USB sync (2026-10-01) bring their logo's
        web address, not the picture: fetch each one once, when the Radio
        page is opened."""
        tried = self.__dict__.setdefault("_logos_tried", set())
        with self.ctx.session() as s:
            missing = [(st.id, st.favicon_url) for st in stations.dial(s)
                       if st.favicon_url and not st.favicon_path and st.id not in tried]
        for sid, url in missing:
            tried.add(sid)
            thread = LogoThread(sid, url, self)
            thread.finished.connect(self._update_info)
            thread.start()

    def set_band(self, band: str) -> None:
        if band == self.radio.band:
            return
        self.radio.set_band(band)
        _set_setting(self.ctx, BAND_KEY, band)
        self._update_info()

    def _rebuild(self) -> None:
        entries: dict[str, list[tuple[str, int, str]]] = {k: [] for k in BAND_KEYS}
        off_air: set[int] = set()
        with self.ctx.session() as s:
            shows = otr.list_shows(s)
            if shows:
                entries["police"].append(("onair", 0, ONAIR_LABEL))
            for sh in shows:
                band = sh.band if sh.band in entries else otr.DEFAULT_SHOW_BAND
                entries[band].append(("show", sh.id, otr.dial_label(sh.name)))
            for st in stations.dial(s):
                band = st.band if st.band in entries else "fm"
                entries[band].append(("station", st.id, st.name))
                if st.off_air_since:
                    off_air.add(st.id)
        self._entries = entries
        self._off_air = off_air
        for band, items in entries.items():
            self.radio.set_entries(band, [label for _, _, label in items],
                                   faded=[i for i, (k, n, _) in enumerate(items)
                                          if k == "station" and n in off_air])

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
        self._sync_signal()
        self._update_info()

    def _sync_signal(self) -> None:
        """The magic eye shows the stream's signal only while a station is
        tuned in; shows and On the Air are local files."""
        live_station = self.ctx.tuner.tuned[0] == "station"
        self.radio.set_signal(self.ctx.player.signal if live_station else SIGNAL_NONE)

    def _on_stream_title(self, title: str) -> None:
        self._stream_title = title
        self._update_info()

    def _on_state(self, state: str) -> None:
        self.radio.set_playing(state == "playing" and bool(self.ctx.tuner.tuned[0]))

    # -- info card ---------------------------------------------------------

    def _all_buttons(self):
        return (self.btn_listen, self.btn_episodes, self.btn_prev, self.btn_next,
                self.btn_new_evening, self.btn_band, self.btn_rename, self.btn_move_left,
                self.btn_move_right, self.btn_remove, self.btn_relink, self.btn_folder,
                self.btn_starters)

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

    def _set_status(self, *parts: str, tip: str = "", warn: bool = False) -> None:
        text = " · ".join(p for p in parts if p)
        if self._scan_text:
            text = self._scan_text
        self.status.setText(text)
        self.status.setToolTip(tip or text)
        self.status.setStyleSheet(f"color: {COLORS['jukebox_key_hi']};" if warn else "")

    def _update_info(self) -> None:
        kind, ident, live = self._pointed()
        self._live = live
        band = self.radio.band
        if not self._entries.get(band):
            self._empty_band(band)
        elif kind == "show":
            self._info_show(ident)
        elif kind == "onair":
            self._info_onair()
        elif kind == "station":
            self._info_station(ident)
        else:
            self._set_status(f"{BY_KEY[band].label} band — tap a name on the lit band, drag the "
                             "pointer, or turn the TUNING knob")
            self._show_buttons()

    def _empty_band(self, band: str) -> None:
        with self.ctx.session() as s:
            root = otr.get_root(s)
            any_shows = bool(otr.list_shows(s))
        label = BY_KEY[band].label
        if band in otr.STATION_BANDS:
            self._set_status(f"No stations on the {label} band yet",
                             "find some in the free Radio Browser directory",
                             tip="Find internet stations in the free Radio Browser directory, "
                                 "add one by its stream address, or start with a few old-time "
                                 "radio stations.")
            self._show_buttons(self.btn_starters)
            return
        if not any_shows:
            if root:
                self._set_status(f"No shows found in {root}",
                                 "each show should be a folder of episodes inside it")
            else:
                self._set_status("Choose the folder that holds your radio programs",
                                 "one folder per show (The Shadow, Lone Ranger, …)")
            self._show_buttons(self.btn_folder)
            return
        self._set_status(f"Nothing on {label} yet",
                         "move a show or station here with its Band… button",
                         tip=BAND_BLURBS.get(band, ""))
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
            bits = []
            if ep is not None:
                if ep.air_date:
                    bits.append(f"broadcast {otr.format_air_date(ep.air_date)}")
                if ep.episode_no:
                    bits.append(f"episode {ep.episode_no}")
            heard_text = f"{heard:,} of {total:,} heard" if total else ""
            if not self._live and ep is not None and ep.position_ms:
                heard_text += f", picks up at {format_duration(ep.position_ms)}"
            # the player bar already names what's playing; say the show
            # only when the pointer rests on one that isn't on
            self._set_status("" if self._live else show.name,
                             ep.title if (ep and not self._live) else "",
                             *bits, heard_text)
        if self._live:
            self._show_buttons(self.btn_episodes, self.btn_prev, self.btn_next, self.btn_band)
        else:
            self._show_buttons(self.btn_listen, self.btn_episodes, self.btn_band)

    def _info_onair(self) -> None:
        year = self.ctx.tuner.onair_year
        tip = "Programs from your collection, with commercials and news bulletins between them"
        if not self._live:
            self._set_status("On the Air", "a night of radio from your collection", tip=tip)
            self._show_buttons(self.btn_listen)
            return
        self._set_status(f"On the Air — an evening in {year}" if year else "On the Air", tip=tip)
        self._show_buttons(self.btn_next, self.btn_new_evening)

    def _info_station(self, station_id: int) -> None:
        with self.ctx.session() as s:
            st = s.get(RadioStation, station_id)
            if st is None:
                return
            band = st.band if st.band in otr.STATION_BANDS else "fm"
            freq = stations.frequency(st.name, band)
            off_air = bool(st.off_air_since)
            # (no frequency here - it's in the name, and placed on the glass)
            detail = " · ".join(x for x in (
                st.country, (st.codec or "").upper(),
                f"{st.bitrate} kbps" if st.bitrate else "") if x)
            tip = "\n".join(x for x in (st.name, detail, st.homepage or "") if x)
            name = st.name
        if off_air:
            self._set_status(name, "off the air — its stream stopped answering",
                             tip=tip, warn=True)
        elif self._live:
            # the player bar names the station; here, what it's playing
            self._set_status(f"♪ {self._stream_title}" if self._stream_title else detail, tip=tip)
        else:
            self._set_status(name, detail, tip=tip)
        buttons = [self.btn_band, self.btn_rename]
        if freq is None:
            # placed by its frequency otherwise, so moving it means nothing
            buttons += [self.btn_move_left, self.btn_move_right]
        buttons.append(self.btn_remove)
        if off_air:
            buttons.insert(0, self.btn_relink)
        if not self._live or off_air:
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
        self._scan_text = "Reading your radio programs…"
        self._update_info()
        self._scan_thread = OtrScanThread(root, self)
        self._scan_thread.progress.connect(self._on_scan_progress)
        self._scan_thread.finished_with.connect(self._on_scan_done)
        self._scan_thread.start()

    def _on_scan_progress(self, done: int, total: int, name: str) -> None:
        self._scan_text = f"Reading {name}… {done:,} of {total:,}"
        self.status.setText(self._scan_text)

    def _on_scan_done(self, result) -> None:
        self._scan_text = ""
        self.ctx.notify(f"Old-time radio: {result.summary()}" if result else
                        "Couldn't read the old-time radio folder")
        self._changed()

    def _changed(self) -> None:
        self._rebuild()
        kind, ident = self.ctx.tuner.tuned
        self._point_at(kind, ident, animate=False)
        self._update_info()

    def find_stations(self) -> None:  # pragma: no cover - dialog
        dialog = StationSearchDialog(self.ctx, self, band=self.station_band())
        dialog.changed.connect(self._changed)
        dialog.listenRequested.connect(self.ctx.tuner.tune_station)
        dialog.exec()

    def station_band(self) -> str:
        """Where new stations go: the lit band if it's FM or AM, else FM."""
        band = self.radio.band
        return band if band in otr.STATION_BANDS else "fm"

    def _add_starters(self) -> None:
        band = self.station_band()
        self.ctx.notify("Looking up old-time radio stations…")
        thread = StationSearchThread("", stations.STARTER_SEARCH, self)

        def done(results, error: str) -> None:
            if error or not results:
                self.ctx.notify("Couldn't reach the station directory — try Find stations later")
                return
            with self.ctx.session() as s:
                added = [stations.add(s, f, band=band) for f in results[: stations.STARTER_COUNT]]
                s.commit()
                logos = [(st.id, st.favicon_url) for st in added if st.favicon_url]
            for sid, url in logos:
                LogoThread(sid, url, self).start()
            self._changed()
            self.ctx.notify(f"{len(added)} stations added to the {BY_KEY[band].label} band")

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
        """Swap places with the next internet-only station along the band -
        the ones with a frequency stay at their frequency."""
        kind, ident, _ = self._pointed()
        if kind != "station":
            return
        with self.ctx.session() as s:
            st = s.get(RadioStation, ident)
            if st is None:
                return
            band = st.band if st.band in otr.STATION_BANDS else "fm"
            loose = [x.id for x in stations.band_stations(s, band)
                     if stations.frequency(x.name, band) is None]
            stations.move(s, ident, delta, among=loose)
            s.commit()
        self._changed()

    def _ask_rename_station(self) -> None:  # pragma: no cover - dialog
        kind, ident, _live = self._pointed()
        if kind != "station":
            return
        with self.ctx.session() as s:
            st = s.get(RadioStation, ident)
            current = st.name if st else ""
        name, ok = QInputDialog.getText(self, "Rename station", "Name on the dial:", text=current)
        if ok:
            self.rename_station(name)

    def rename_station(self, name: str) -> None:
        """Rename the station the card describes."""
        kind, ident, live = self._pointed()
        if kind != "station" or not (name or "").strip():
            return
        with self.ctx.session() as s:
            stations.rename(s, ident, name)
            s.commit()
        item = self.ctx.player.current
        if live and item is not None and item.kind == "stream" and item.station_id == ident:
            item.title = name.strip()  # the player bar shows it from the next update
        self._rebuild()
        b, idx = self._locate("station", ident)
        if idx >= 0:
            self.radio.set_tuned(b, idx, animate=False)
        self._update_info()

    def find_new_link(self) -> None:  # pragma: no cover - dialog
        kind, ident, _ = self._pointed()
        if kind != "station":
            return
        dialog = ReplacementDialog(self.ctx, ident, self)
        dialog.replaced.connect(self._relinked)
        dialog.exec()

    def _relinked(self, station_id: int) -> None:
        self._changed()
        self.ctx.tuner.tune_station(station_id)

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
