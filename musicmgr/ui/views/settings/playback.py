"""Settings › Playback (split out of settings.py, 2026-10-22)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel
from sqlalchemy import select
from ....services import playback_prefs
from ...widgets.common import TouchButton, dim_label
from .parts import (
    CROSSFADE_CHOICES,
    LEVELLING_CHOICES,
    LoudnessAnalyzeThread,
    PREAMP_CHOICES,
)


class PlaybackSection:
    """Settings › Playback: a mixin of SettingsView (view.py) - its
    methods share the view's state."""

    def _build_playback(self, section) -> None:
        """Build the Playback group (moved out of __init__, 2026-10-22)."""
        # ---- playback (2026-10-01) ----
        # Crossfade/gapless and volume levelling - see services/player.py's
        # module docstring and services/replaygain.py.
        body = section("Playback", "playback")
        playback_card = QFrame()
        playback_card.setObjectName("Card")
        pgrid = QGridLayout(playback_card)
        pgrid.setContentsMargins(20, 14, 20, 14)
        pgrid.setHorizontalSpacing(18)
        pgrid.setVerticalSpacing(10)

        def combo(choices) -> QComboBox:
            box = QComboBox()
            for label, value in choices:
                box.addItem(label, value)
            return box

        self.crossfade_combo = combo(CROSSFADE_CHOICES)
        self.crossfade_album_cb = QCheckBox("Crossfade between tracks of the same album too")
        self.levelling_combo = combo(LEVELLING_CHOICES)
        self.preamp_combo = combo(PREAMP_CHOICES)
        self.levels_status = dim_label("")
        self.levels_status.setWordWrap(True)
        # 2026-10-31: moved here from Maintenance › Volume levels, beside
        # the setting it feeds - reads ReplayGain tags or measures loudness
        # for every track without a level yet (services/replaygain.py)
        self.analyze_levels_btn = TouchButton("Analyze all…")
        self.analyze_levels_btn.setToolTip(
            "Read ReplayGain tags, or measure loudness, for every track without a level "
            "yet. Your music files aren't changed.")
        self.analyze_levels_btn.clicked.connect(self.analyze_loudness)

        pgrid.addWidget(QLabel("Crossfade"), 0, 0)
        pgrid.addWidget(self.crossfade_combo, 0, 1)
        pgrid.addWidget(self.crossfade_album_cb, 1, 1)
        pgrid.addWidget(QLabel("Volume levelling"), 2, 0)
        pgrid.addWidget(self.levelling_combo, 2, 1)
        pgrid.addWidget(QLabel("Levelling adjust"), 3, 0)
        pgrid.addWidget(self.preamp_combo, 3, 1)
        levels_row = QHBoxLayout()
        levels_row.setSpacing(16)
        levels_row.addWidget(self.levels_status, 1)
        levels_row.addWidget(self.analyze_levels_btn, 0, Qt.AlignTop)
        pgrid.addLayout(levels_row, 4, 1)
        pgrid.setColumnStretch(1, 1)
        body.addWidget(playback_card)
        self._load_playback_prefs()
        for box in (self.crossfade_combo, self.levelling_combo, self.preamp_combo):
            box.currentIndexChanged.connect(self._save_playback_prefs)
        self.crossfade_album_cb.toggled.connect(self._save_playback_prefs)

    def _load_playback_prefs(self) -> None:
        with self.ctx.session() as db:
            prefs = playback_prefs.load(db)
        widgets = (
            (self.crossfade_combo, prefs.crossfade_s),
            (self.levelling_combo, prefs.levelling),
            (self.preamp_combo, float(round(prefs.preamp_db))),
        )
        for box, value in widgets:
            box.blockSignals(True)
            idx = box.findData(value)
            box.setCurrentIndex(idx if idx >= 0 else 0)
            box.blockSignals(False)
        self.crossfade_album_cb.blockSignals(True)
        self.crossfade_album_cb.setChecked(prefs.crossfade_same_album)
        self.crossfade_album_cb.blockSignals(False)
        self._sync_playback_controls()

    def _sync_playback_controls(self) -> None:
        self.crossfade_album_cb.setVisible(self.crossfade_combo.currentData() != 0)
        self.preamp_combo.setEnabled(
            self.levelling_combo.currentData() != playback_prefs.LEVELLING_OFF
        )

    def _save_playback_prefs(self, *_args) -> None:
        prefs = playback_prefs.PlaybackPrefs(
            crossfade_s=int(self.crossfade_combo.currentData() or 0),
            crossfade_same_album=self.crossfade_album_cb.isChecked(),
            levelling=self.levelling_combo.currentData() or playback_prefs.LEVELLING_AUTO,
            preamp_db=float(self.preamp_combo.currentData() or 0.0),
        )
        with self.ctx.session() as db:
            playback_prefs.save(db, prefs)
            db.commit()
        self._sync_playback_controls()
        self.ctx.player.reload_prefs()

    def _refresh_levels_status(self) -> None:
        from sqlalchemy import func

        from ....db.models import MediaFile

        with self.ctx.session() as db:
            total = db.scalar(
                select(func.count(MediaFile.id)).where(MediaFile.is_missing.is_(False))
            ) or 0
            known = db.scalar(
                select(func.count(MediaFile.id)).where(
                    MediaFile.is_missing.is_(False), MediaFile.rg_track_gain.is_not(None)
                )
            ) or 0
        if total == 0:
            text = ""
        elif known >= total:
            text = f"All {total:,} tracks have a volume level."
        else:
            text = (
                f"{known:,} of {total:,} tracks have a volume level. The rest are measured "
                "as they play, or all at once with Analyze all…"
            )
        self.levels_status.setText(text)

    def analyze_loudness(self) -> None:
        if self._loudness_thread is not None and self._loudness_thread.isRunning():
            self.ctx.notify("Already analyzing volume levels")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_label.setText("Finding tracks without a volume level…")
        self._loudness_thread = LoudnessAnalyzeThread(parent=self)
        self._loudness_thread.progress.connect(self._on_loudness_progress)
        self._loudness_thread.finished_with.connect(self._on_loudness_done)
        self._loudness_thread.start()
        self._show_cancel()

    def _on_loudness_progress(self, done: int, total: int, name: str) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
            self.progress_label.setText(name or "Analyzing…")
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.progress_label.setText(f"Volume levels {done:,}/{total:,}  {name}")

    def _on_loudness_done(self, result) -> None:
        self.progress.setVisible(False)
        self.progress_label.setText("")
        self._hide_cancel(self._loudness_thread)
        self.ctx.notify(f"Volume levels: {result.summary()}")
        self.ctx.player.reload_prefs()
        self._refresh_levels_status()
