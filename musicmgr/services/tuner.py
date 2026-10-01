"""The radio tuner's brain (2026-10-01): what's tuned in, episode resume,
and the On-Air station. The page itself is ui/views/tuner.py.

Three kinds of thing sit on the dial:

- an **old-time radio show** - plays its current episode (resuming
  mid-episode) and carries on through the following broadcasts in
  air-date order; positions are saved as it plays, and an episode heard to
  the end moves the show on (services/otr.py);
- the **On-Air** station - a broadcast night: a year is picked for the
  evening, then program after program from that era with a commercial or
  two before each and now and then a news bulletin from the same year.
  Endless - topped up like the song radio (services/radio.py). On-Air
  doesn't touch shows' resume points;
- an **internet station** (services/stations.py).

Anything else played from elsewhere in the app simply takes over the
player; the dial stays where it was.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

from PySide6.QtCore import QObject, Signal

from ..db.models import RadioEpisode, RadioShow, RadioStation, Setting
from ..db.session import session_scope
from . import otr, stations

log = logging.getLogger(__name__)

SOURCE_SHOW = "otr:show"
SOURCE_ONAIR = "otr:onair"
SOURCE_STATION = "station"

#: save an episode's position at most this often while it plays
SAVE_EVERY_MS = 10_000
ON_AIR_REFILL_WHEN_LEFT = 2

#: where the dial was left: "band|kind|id" in the settings table
LAST_TUNED_KEY = "tuner_last"


class TunerController(QObject):
    #: (kind, id) now tuned in: ("show", id) / ("onair", 0) / ("station", id),
    #: or ("", 0) when something else took over the player
    tunedChanged = Signal(str, int)
    #: on-air: the evening's year changed (0 = unknown)
    onAirYearChanged = Signal(int)
    notified = Signal(str)

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self.player = player
        self._tuned: tuple[str, int] = ("", 0)
        self._onair: Optional[otr.OnAirState] = None
        self._rng = random.Random()
        self._busy = False
        self._tuning: Optional[tuple[str, int]] = None
        self._last_saved_ms = 0
        #: (episode_id, last position, duration) of the episode playing
        self._ep_progress: Optional[list] = None
        player.trackChanged.connect(self._on_track_changed)
        player.positionChanged.connect(self._on_position)
        player.queueChanged.connect(self._check)
        player.playbackStateChanged.connect(self._on_state)
        player.itemFinished.connect(self._on_item_finished)

    # -- what's tuned -----------------------------------------------------

    @property
    def tuned(self) -> tuple[str, int]:
        return self._tuned

    @property
    def onair_year(self) -> Optional[int]:
        return self._onair.year if self._onair else None

    def _set_tuned(self, kind: str, ident: int) -> None:
        if self._tuned != (kind, ident):
            self._tuned = (kind, ident)
            self.tunedChanged.emit(kind, ident)
        if kind:
            try:
                with session_scope() as session:
                    row = session.get(Setting, LAST_TUNED_KEY)
                    value = f"{kind}|{ident}"
                    if row is None:
                        session.add(Setting(key=LAST_TUNED_KEY, value=value))
                    else:
                        row.value = value
            except Exception:  # pragma: no cover
                pass

    @staticmethod
    def last_tuned() -> tuple[str, int]:
        try:
            with session_scope() as session:
                row = session.get(Setting, LAST_TUNED_KEY)
                if row and row.value and "|" in row.value:
                    kind, ident = row.value.split("|", 1)
                    return kind, int(ident)
        except Exception:  # pragma: no cover
            pass
        return "", 0

    def _play(self, items, source: str, tuned: tuple[str, int]) -> bool:
        if not items:
            return False
        self._save_progress()
        self._ep_progress = None
        #: what's being tuned in, for _on_track_changed while play_tracks runs
        self._tuning = tuned
        self._busy = True
        try:
            self.player.set_shuffle(False)
            self.player.play_tracks(items, start=0, source=source)
        finally:
            self._busy = False
            self._tuning = None
        return True

    # -- tuning -----------------------------------------------------------

    def tune_show(self, show_id: int, episode_id: Optional[int] = None) -> bool:
        with session_scope() as session:
            show = session.get(RadioShow, show_id)
            if show is None:
                return False
            items = otr.queue_for_show(session, show_id, start_episode_id=episode_id)
            if episode_id is not None:
                ep = session.get(RadioEpisode, episode_id)
                if ep is not None:
                    otr.set_current(session, ep)
            name = show.name
        if not items:
            self.notified.emit(f"Nothing to play for {name} - is the radio folder connected?")
            return False
        self._onair = None
        self._play(items, f"{SOURCE_SHOW}:{show_id}", ("show", show_id))
        self._set_tuned("show", show_id)
        return True

    def tune_onair(self) -> bool:
        with session_scope() as session:
            state = otr.OnAirState(year=otr.pick_evening_year(session, self._rng), played_ids=set())
            items = []
            for _ in range(2):
                items += otr.build_on_air_block(session, state, self._rng)
        if not items:
            self.notified.emit("Nothing to broadcast yet - scan your old-time radio folder first")
            return False
        self._onair = state
        self._play(items, SOURCE_ONAIR, ("onair", 0))
        self._set_tuned("onair", 0)
        self.onAirYearChanged.emit(state.year or 0)
        return True

    def tune_station(self, station_id: int) -> bool:
        with session_scope() as session:
            st = session.get(RadioStation, station_id)
            if st is None:
                return False
            item = stations.queue_item(st)
            uuid = st.rb_uuid
        self._onair = None
        self._play([item], f"{SOURCE_STATION}:{station_id}", ("station", station_id))
        self._set_tuned("station", station_id)
        if uuid:
            import threading

            threading.Thread(target=stations.register_click, args=(uuid,), daemon=True).start()
        return True

    # -- keeping track ----------------------------------------------------

    def _check(self) -> None:
        if self._busy:
            return
        source = self.player.source
        kind = self._tuned[0]
        still_ours = (
            (kind == "show" and source.startswith(SOURCE_SHOW))
            or (kind == "onair" and source == SOURCE_ONAIR)
            or (kind == "station" and source.startswith(SOURCE_STATION))
        )
        if kind and (not still_ours or not self.player.queue):
            self._onair = None
            self._tuned = ("", 0)
            self.tunedChanged.emit("", 0)
            return
        if kind == "onair" and self.player.remaining_count() <= ON_AIR_REFILL_WHEN_LEFT:
            with session_scope() as session:
                items = otr.build_on_air_block(session, self._onair, self._rng)
            if items:
                self._busy = True
                try:
                    self.player.enqueue(items)
                finally:
                    self._busy = False

    def _on_track_changed(self, item) -> None:
        self._save_progress()
        self._ep_progress = None
        self._last_saved_ms = 0
        kind = (self._tuning or self._tuned)[0]
        if item is not None and item.kind == "episode" and kind == "show":
            self._ep_progress = [item.episode_id, item.start_ms or 0, item.duration_ms or 0]
            with session_scope() as session:
                ep = session.get(RadioEpisode, item.episode_id)
                if ep is not None:
                    otr.set_current(session, ep)
        self._check()

    def _on_item_finished(self, item) -> None:
        """Heard to the end: count the whole episode as played."""
        if self._ep_progress is not None and item.kind == "episode" \
                and item.episode_id == self._ep_progress[0]:
            length = self._ep_progress[2] or item.duration_ms or 0
            self._ep_progress[1] = max(self._ep_progress[1], length)

    def _on_position(self, ms: int) -> None:
        if self._ep_progress is None:
            return
        self._ep_progress[1] = ms
        duration = self.player.duration()
        if duration:
            self._ep_progress[2] = duration
        if abs(ms - self._last_saved_ms) >= SAVE_EVERY_MS:
            self._save_progress(final=False)

    def _on_state(self, state: str) -> None:
        if state in ("paused", "stopped"):
            self._save_progress(final=False)

    def _save_progress(self, final: bool = True) -> None:
        """Write the playing episode's position; at the end of an episode
        (`final`, the queue moving on) a finished one is marked played."""
        if self._ep_progress is None:
            return
        episode_id, position, duration = self._ep_progress
        if episode_id is None:
            return
        self._last_saved_ms = position
        try:
            with session_scope() as session:
                if final:
                    otr.save_position(session, episode_id, position, duration)
                else:
                    ep = session.get(RadioEpisode, episode_id)
                    if ep is not None and not ep.played:
                        ep.position_ms = position if position >= otr.MIN_RESUME_MS else 0
        except Exception as exc:  # pragma: no cover
            log.debug("could not save episode position: %s", exc)

    def shutdown(self) -> None:
        """MainWindow.closeEvent - keep the place in the episode."""
        self._save_progress(final=False)
