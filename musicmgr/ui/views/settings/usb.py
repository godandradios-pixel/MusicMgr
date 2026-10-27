"""Settings › USB sync.

See services/usb_sync.py. The flow: poll_usb notices the drive (every
5 s), check_usb walks and compares both sides in UsbCompareThread,
UsbSyncDialog shows what it found, and UsbSyncThread copies what is left
checked and then imports just those files into the library."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
)
from ....services import updater
from ....services import library_backup
from ....services import library_state
from ....services import usb_sync
from ... import skins
from ...widgets.common import TouchButton, clear_art_cache, dim_label
from ...widgets.usb_sync import (
    UsbCompareThread,
    UsbPairsDialog,
    UsbSyncDialog,
    UsbSyncThread,
    human_size,
)


class UsbSection:
    """Settings › USB sync: a mixin of SettingsView (view.py) - its
    methods share the view's state."""

    def _build_usb(self, section) -> None:
        """Build the Usb group."""
        # ---- USB sync ----
        # Two-way sync of music, videos and movies with a USB drive (in
        # place of GoodSync). See services/usb_sync.py and
        # claude/2026-09-23-usb-sync-plan.md.
        body = section("USB sync", "usb")

        usb_card = QFrame()
        usb_card.setObjectName("Card")
        usb_layout = QVBoxLayout(usb_card)
        usb_layout.setContentsMargins(20, 14, 20, 14)
        usb_layout.setSpacing(8)
        self.usb_drive_label = QLabel("")
        self.usb_drive_label.setStyleSheet("font-weight: 600;")
        usb_layout.addWidget(self.usb_drive_label)
        self.usb_pairs_label = dim_label("")
        self.usb_pairs_label.setWordWrap(True)
        usb_layout.addWidget(self.usb_pairs_label)
        self.usb_status = dim_label("")
        self.usb_status.setWordWrap(True)
        usb_layout.addWidget(self.usb_status)
        # sync direction, per PC (services/usb_sync.py: PREF_MODE)
        mode_row = QHBoxLayout()
        mode_row.addWidget(dim_label("Sync direction:"))
        self.usb_mode_from_usb = QRadioButton("One way: USB → this PC")
        self.usb_mode_from_usb.setToolTip(
            "Only what's new or changed on the USB comes to this PC (music, videos, artwork, "
            "radio shows and the library data ticked below). Nothing is written to the USB "
            "and nothing is deleted on this PC. For the odd two-way sync, use "
            "\"Two-way sync now…\" below")
        self.usb_mode_two_way = QRadioButton("Two-way")
        self.usb_mode_two_way.setToolTip(
            "New and changed files go both ways, and deletions on either side are offered "
            "for review")
        self.usb_mode_group = QButtonGroup(usb_card)
        for b in (self.usb_mode_from_usb, self.usb_mode_two_way):
            self.usb_mode_group.addButton(b)
            mode_row.addWidget(b)
        mode_row.addStretch(1)
        self.usb_mode_from_usb.toggled.connect(self._on_usb_mode_toggled)
        usb_layout.addLayout(mode_row)
        self.usb_auto_cb = QCheckBox("Check when the USB drive is plugged in")
        self.usb_auto_cb.toggled.connect(
            lambda on: self._save_update_pref(usb_sync.PREF_AUTO_CHECK, on)
        )
        usb_layout.addWidget(self.usb_auto_cb)
        self.usb_snapshot_cb = QCheckBox("Save a copy of the library on the drive after each sync")
        self.usb_snapshot_cb.setToolTip(
            "Leaves MusicMgr\\data-<date> on the drive (the newest two are kept), which "
            "Install-MusicMgr.bat and Copy-USB-to-D.bat use to set up a PC with "
            "this library")
        self.usb_snapshot_cb.toggled.connect(
            lambda on: self._save_update_pref(library_backup.PREF_DRIVE_SNAPSHOT, on)
        )
        usb_layout.addWidget(self.usb_snapshot_cb)
        # which library data rides along, per PC; see
        # services/library_state.py:sync_library_state's `include`. A grid,
        # as there are too many for one row.
        usb_layout.addWidget(dim_label("Also sync library data:"))
        data_row = QGridLayout()
        data_row.setHorizontalSpacing(18)
        data_cols = 4
        self.usb_data_cbs: dict[str, QCheckBox] = {}
        for cat, text in (
            (library_state.PLAYS, "Plays"),
            (library_state.RATINGS, "Ratings"),
            (library_state.PLAYLISTS, "Playlists"),
            (library_state.JUKEBOX, "Jukebox"),
            (library_state.CHARTS, "Charts"),
            (library_state.RADIO, "Radio"),
            (library_state.PROFILES, "Artist profiles"),
            # Last.fm top tracks
            (library_state.TOP_TRACKS, "Top songs"),
            (library_state.SIMILAR, "Similar artists"),
            (library_state.TUNER, "Radio dial"),
            (library_state.PREFS, "Playback & look"),
        ):
            cb = QCheckBox(text)
            if cat == library_state.PLAYLISTS:
                # chosen playlist/folder images ride on this one
                cb.setToolTip("Manual and smart playlists, plus the images chosen for any "
                              "playlist or folder")
            if cat == library_state.RADIO:
                cb.setToolTip("The internet stations on the Radio page's FM and AM bands, "
                              "and old-time radio progress: each show's band, the "
                              "episodes heard and where one was stopped")
            if cat == library_state.CHARTS:
                cb.setToolTip("Imported charts, their folders and editions, and matches "
                              "fixed by hand. Each PC matches the rest to its own library")
            if cat == library_state.PROFILES:
                cb.setToolTip("The biography on each artist's page and the links it came "
                              "from. Artist photos travel with the artwork files")
            if cat == library_state.TOP_TRACKS:
                cb.setToolTip("Each artist's Last.fm top tracks, and the top songs "
                              "chosen by hand to show instead")
            if cat == library_state.SIMILAR:
                cb.setToolTip("Last.fm's similar artists, which \"radio from this song\" "
                              "picks from, so the other PC needn't fetch them again")
            if cat == library_state.TUNER:
                cb.setToolTip("Where the Radio page's dial was left: the band, the "
                              "station or show tuned in, and the static switch")
            if cat == library_state.PREFS:
                cb.setToolTip("Crossfade, volume levelling and preamp, the record player "
                              "on Now Playing, and the skin and finishes (a new look shows "
                              "after a restart). Off unless switched on: each PC may want "
                              "its own")
            cb.toggled.connect(lambda on, cat=cat: self._save_usb_data_choice(cat, on))
            n = len(self.usb_data_cbs)
            self.usb_data_cbs[cat] = cb
            data_row.addWidget(cb, n // data_cols, n % data_cols)
        data_row.setColumnStretch(data_cols, 1)
        usb_layout.addLayout(data_row)
        # no progress bar of its own: the page's pinned bar stays in view
        # wherever the page is scrolled and with this group folded (see
        # _usb_show).

        usb_buttons = QHBoxLayout()
        self.usb_check_btn = TouchButton("Check USB now", primary=True)
        self.usb_check_btn.setObjectName("PrimaryWarm")
        self.usb_check_btn.clicked.connect(lambda: self.check_usb(auto=False))
        # the odd sync from this PC back to the USB, without changing the
        # usual direction (2026-10-27)
        self.usb_two_way_btn = TouchButton("Two-way sync now…")
        self.usb_two_way_btn.setToolTip(
            "Check and sync both ways this once - this PC's new and changed files go to the "
            "USB too. The sync direction above stays as it is")
        self.usb_two_way_btn.clicked.connect(
            lambda: self.check_usb(auto=False, mode=usb_sync.MODE_TWO_WAY))
        self.usb_review_btn = TouchButton("Review…", primary=True)
        self.usb_review_btn.setObjectName("PrimaryWarm")
        self.usb_review_btn.clicked.connect(self.review_usb_plan)
        self.usb_cancel_btn = TouchButton("Cancel")
        self.usb_cancel_btn.clicked.connect(self._cancel_usb)
        self.usb_setup_btn = TouchButton("Set up this drive…")
        self.usb_setup_btn.clicked.connect(self.setup_usb_drive)
        self.usb_pairs_btn = TouchButton("Folder pairs…")
        self.usb_pairs_btn.clicked.connect(self.edit_usb_pairs)
        self.usb_empty_btn = TouchButton("Empty _deleted…")
        self.usb_empty_btn.clicked.connect(self.empty_usb_deleted)
        for b in (
            self.usb_check_btn, self.usb_two_way_btn, self.usb_review_btn, self.usb_cancel_btn,
            self.usb_setup_btn, self.usb_pairs_btn, self.usb_empty_btn,
        ):
            usb_buttons.addWidget(b)
        usb_buttons.addStretch(1)
        usb_layout.addLayout(usb_buttons)
        body.addWidget(usb_card)

        self._usb_compare_thread: Optional[UsbCompareThread] = None
        self._usb_sync_thread: Optional[UsbSyncThread] = None
        #: the drive found by the last poll, if any
        self._usb_drive: Optional[usb_sync.Drive] = None
        #: False until the first poll after launch has run - see poll_usb
        self._usb_polled_once = False
        #: an auto-check's result, waiting behind "Review…"
        self._usb_pending_plan: Optional[usb_sync.ComparePlan] = None
        with self.ctx.session() as db:
            auto = updater.get_bool_pref(db, usb_sync.PREF_AUTO_CHECK, True)
            snapshot = updater.get_bool_pref(db, library_backup.PREF_DRIVE_SNAPSHOT, True)
            chosen = library_state.enabled_categories(db)
            mode = usb_sync.get_mode(db)
        for b in (self.usb_mode_two_way, self.usb_mode_from_usb):
            b.blockSignals(True)
        (self.usb_mode_from_usb if mode == usb_sync.MODE_FROM_USB
         else self.usb_mode_two_way).setChecked(True)
        for b in (self.usb_mode_two_way, self.usb_mode_from_usb):
            b.blockSignals(False)
        self.usb_auto_cb.blockSignals(True)
        self.usb_auto_cb.setChecked(auto)
        self.usb_auto_cb.blockSignals(False)
        self.usb_snapshot_cb.blockSignals(True)
        self.usb_snapshot_cb.setChecked(snapshot)
        self.usb_snapshot_cb.blockSignals(False)
        for cat, cb in self.usb_data_cbs.items():
            cb.blockSignals(True)
            cb.setChecked(cat in chosen)
            cb.blockSignals(False)
        #: looks for the drive every few seconds - see poll_usb
        self._usb_timer = QTimer(self)
        self._usb_timer.setInterval(5000)
        self._usb_timer.timeout.connect(self.poll_usb)
        self._usb_timer.start()
        self._refresh_usb_controls()
        QTimer.singleShot(1500, self.poll_usb)

    def usb_mode(self) -> str:
        return (usb_sync.MODE_FROM_USB if self.usb_mode_from_usb.isChecked()
                else usb_sync.MODE_TWO_WAY)

    def _on_usb_mode_toggled(self, _on: bool) -> None:
        with self.ctx.session() as db:
            usb_sync.set_mode(db, self.usb_mode())
            db.commit()
        # a waiting plan was made for the other direction
        if self._usb_pending_plan is not None and self._usb_pending_plan.mode != self.usb_mode():
            self._usb_pending_plan = None
            self.usb_status.setText("Sync direction changed — check the USB again")
        self._refresh_usb_controls()

    def _save_usb_data_choice(self, category: str, on: bool) -> None:
        with self.ctx.session() as db:
            library_state.set_category_enabled(db, category, on)

    def _find_usb_drives(self) -> list:
        """Overridable seam for tests."""
        return usb_sync.find_drives()

    def _usb_running(self) -> bool:
        return any(
            t is not None and t.isRunning()
            for t in (self._usb_compare_thread, self._usb_sync_thread)
        )

    def poll_usb(self) -> None:
        """Notice the drive being plugged in or pulled out. A newly seen
        drive gets a quiet check when "Check when the USB drive is plugged
        in" is on - it never copies anything without a review."""
        try:
            drives = self._find_usb_drives()
        except Exception:  # never let a poll take the page down
            logging.getLogger(__name__).exception("USB poll failed")
            drives = []
        drive = drives[0] if drives else None
        previous = self._usb_drive.drive_id if self._usb_drive else None
        current = drive.drive_id if drive else None
        self._usb_drive = drive
        first_poll = not self._usb_polled_once
        self._usb_polled_once = True
        if current == previous and not first_poll:
            return
        if current is None:
            self._usb_pending_plan = None
            self.usb_status.setText("")
        self._refresh_usb_controls()
        # The first poll after launch only notes a drive that's already in:
        # walking a terabyte at startup would make for a slow start.
        # Plugging the drive in while MusicMgr runs still triggers the
        # quiet check.
        if first_poll:
            return
        if drive is not None and self.usb_auto_cb.isChecked() and not self._busy_with():
            self.check_usb(auto=True)

    def _refresh_usb_controls(self) -> None:
        self._refresh_usb_buttons()
        sec = getattr(self, "_sections", {}).get("usb")
        if sec is not None:
            sec.refresh_summary()

    def _refresh_usb_buttons(self) -> None:
        drive = self._usb_drive
        running = self._usb_running()
        self.usb_setup_btn.setEnabled(not running)
        self.usb_cancel_btn.setVisible(running)
        if not running:
            self.usb_cancel_btn.setEnabled(True)
        self.usb_review_btn.setVisible(self._usb_pending_plan is not None and not running)
        if drive is None:
            self.usb_drive_label.setText("No MusicMgr USB drive connected")
            self.usb_pairs_label.setText(
                "Plug in the USB drive, or choose \"Set up this drive…\" to make one."
            )
            for b in (self.usb_check_btn, self.usb_two_way_btn, self.usb_pairs_btn,
                      self.usb_empty_btn):
                b.setEnabled(False)
            self.usb_two_way_btn.setVisible(self.usb_mode() == usb_sync.MODE_FROM_USB)
            return
        self.usb_drive_label.setText(f"USB drive: {drive.describe()}")
        arrow = "→" if self.usb_mode() == usb_sync.MODE_FROM_USB else "↔"
        with self.ctx.session() as db:
            pairs = [p for p in usb_sync.ensure_default_pairs(db, drive) if p.enabled]
            names = []
            for pair in pairs:
                when = (
                    f" (last synced {pair.last_synced_at:%Y-%m-%d %H:%M})" if pair.last_synced_at else ""
                )
                if usb_sync.on_drive(pair.local_path, drive):
                    # a PC folder on the USB itself would sync with itself
                    when = "  ⚠ on the USB drive itself - not synced; change it in Folder pairs…"
                if arrow == "→":
                    names.append(f"USB\\{pair.usb_rel_path} → {pair.local_path}{when}")
                else:
                    names.append(f"{pair.local_path} ↔ USB\\{pair.usb_rel_path}{when}")
        self.usb_pairs_label.setText(
            "\n".join(names) if names else
            "No folder pairs yet - choose \"Folder pairs…\" to add one."
        )
        self.usb_check_btn.setEnabled(not running and bool(names))
        self.usb_two_way_btn.setEnabled(not running and bool(names))
        # only needed while the usual direction is one way
        self.usb_two_way_btn.setVisible(arrow == "→")
        self.usb_pairs_btn.setEnabled(not running)
        deleted = usb_sync.usb_deleted_dir(drive)
        has_deleted = deleted.is_dir() and any(deleted.iterdir())
        self.usb_empty_btn.setEnabled(not running and has_deleted)
        self.usb_empty_btn.setVisible(has_deleted)

    def check_usb(self, auto: bool = False, mode: Optional[str] = None) -> None:
        """"Check USB now" (auto=False, opens the review straight away) or
        the plug-in check (auto=True, leaves a "Review…" button). `mode` -
        default: the Sync direction setting; "Two-way sync now…" passes
        MODE_TWO_WAY for that one sync."""
        if self._usb_running():
            if not auto:
                self.ctx.notify("The USB is already being checked")
            return
        busy = self._busy_with()
        if busy:
            if not auto:
                self.ctx.notify(busy)
            return
        if self._usb_drive is None:
            self.poll_usb()
        drive = self._usb_drive
        if drive is None:
            if not auto:
                self.ctx.notify("No MusicMgr USB drive found — plug it in, or set it up first")
            return
        self._usb_pending_plan = None
        thread = UsbCompareThread(drive, auto=auto, mode=mode or self.usb_mode(), parent=self)
        thread.progress.connect(lambda text: self._usb_show(text))
        thread.finished_with.connect(lambda plan, error: self._on_usb_compare_done(plan, error, auto))
        self._usb_compare_thread = thread
        self._usb_show("Checking the USB (two-way)…" if mode == usb_sync.MODE_TWO_WAY
                       and self.usb_mode() != usb_sync.MODE_TWO_WAY else "Checking the USB…")
        self._show_cancel()
        thread.start()
        self._refresh_usb_controls()

    def _usb_show(self, text: str, value: Optional[int] = None) -> None:
        """What a USB check or sync is doing right now.
        Shown in the USB sync group and, so it's in view wherever the page
        is scrolled and even with that group folded, on the progress line
        pinned under the page. `value` (0-1000) for a copy's progress;
        None for a step with no count (a moving bar)."""
        self.usb_status.setText(text)
        self._usb_busy = True
        # one bar: the page's pinned one
        bar = self.progress
        if value is None:
            bar.setRange(0, 0)
        else:
            bar.setRange(0, 1000)
            bar.setValue(value)
        bar.setVisible(True)
        self.progress_label.setText(f"USB sync: {text}")
        sec = getattr(self, "_sections", {}).get("usb")
        if sec is not None:
            sec.refresh_summary()

    def _usb_idle(self) -> None:
        """A USB check or sync finished: put the progress line away."""
        self._usb_busy = False
        self.progress.setVisible(False)
        self.progress_label.setText("")

    def _on_usb_stage(self, text: str) -> None:
        self._usb_show(text)

    def _usb_summary(self) -> str:
        """The folded USB group's header: what's happening, else the drive."""
        if getattr(self, "_usb_busy", False):
            return self.usb_status.text()
        return self.usb_drive_label.text()

    def _on_usb_compare_done(self, plan, error, auto: bool) -> None:
        thread = self._usb_compare_thread
        self._usb_idle()
        self.cancel_btn.setVisible(False)
        if thread is not None:
            thread.wait()
        self._usb_compare_thread = None
        if error:
            self.usb_status.setText(f"USB check failed: {error}")
            if not auto:
                self.ctx.notify("USB check failed")
            self._refresh_usb_controls()
            return
        if plan.cancelled:
            self.usb_status.setText("USB check cancelled")
            self._refresh_usb_controls()
            return
        problems = [e for pp in plan.pairs for e in pp.errors]
        if not plan.all_items():
            # the files are done; the quiet sync below still exchanges
            # playlists, plays, ratings and the Jukebox, and replaces this
            # line with what it did
            nothing = "nothing new on the USB" if plan.one_way else "everything is in sync"
            self.usb_status.setText(
                f"Files: {nothing} · checking playlists and library data…"
                + (f" · {'; '.join(problems)}" if problems else "")
            )
            if not auto:
                self.ctx.notify(f"USB: {nothing}")
            # Still run a quiet sync: a first sync of identical folders has a
            # file list to record, and plays/ratings/playlists/the Jukebox
            # (services/library_state.py) are exchanged on every sync.
            self._start_usb_sync(plan, quiet=True)
            return
        self._usb_pending_plan = plan
        self.usb_status.setText(f"USB: {plan.summary()}")
        self._refresh_usb_controls()
        if auto:
            self.ctx.notify(f"USB: {plan.summary()} — review it in Settings → USB sync")
        else:
            self.review_usb_plan()

    def _make_usb_dialog(self, plan) -> UsbSyncDialog:
        """Overridable seam for tests."""
        return UsbSyncDialog(plan, parent=self)

    def review_usb_plan(self) -> None:
        plan = self._usb_pending_plan
        if plan is None:
            return
        if self._busy_with():
            self.ctx.notify(self._busy_with())
            return
        dialog = self._make_usb_dialog(plan)
        if dialog.exec() == QDialog.Accepted:
            self._start_usb_sync(plan)

    def _start_usb_sync(self, plan, quiet: bool = False) -> None:
        thread = UsbSyncThread(plan, parent=self)
        thread.confirm_removals = self._confirm_usb_folder_removals
        thread.progress.connect(self._on_usb_progress)
        thread.stage.connect(self._on_usb_stage)
        thread.finished_with.connect(
            lambda result, message, error: self._on_usb_sync_done(result, message, error, quiet)
        )
        self._usb_sync_thread = thread
        self._usb_pending_plan = None
        if quiet:
            # keeps the "Files: everything is in sync · checking playlists
            # and library data…" line _on_usb_compare_done just wrote
            self._usb_show(self.usb_status.text())
        else:
            self._usb_show("Syncing…", 0)
        self._show_cancel()
        thread.start()
        self._refresh_usb_controls()

    def _confirm_usb_folder_removals(self, folders) -> bool:
        """Ask before a sync removes whole playlist folders the drive no
        longer lists. Asked on every sync, quiet ones too. Keep is the
        default; it sends the folders back to the drive. True = remove them
        here."""
        names = "\n".join(f"  • {f.label()}" for f in folders)
        many = len(folders) != 1
        radio = getattr(self, "usb_mode_from_usb", None)
        one_way = radio is not None and radio.isChecked()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("USB sync")
        box.setText(f"Remove {'these playlist folders' if many else 'this playlist folder'} "
                    "from this PC?")
        box.setInformativeText(
            f"The USB drive no longer has {'them' if many else 'it'}, so another PC may have "
            f"deleted {'them' if many else 'it'}:\n\n{names}\n\n"
            + ("Keep leaves them on this PC; the next two-way sync asks about them again."
               if one_way else
               "Keep puts them back on the drive, so your other PCs get them again."))
        keep = box.addButton("Keep", QMessageBox.RejectRole)
        box.addButton("Remove", QMessageBox.DestructiveRole)
        box.setDefaultButton(keep)
        box.setEscapeButton(keep)
        box.exec()
        return box.clickedButton() is not keep

    def _on_usb_progress(self, done: int, total: int, name: str) -> None:
        if total > 0:
            self._usb_show(f"Copying {name} — {human_size(done)} of {human_size(total)}",
                           int(1000 * min(done, total) / total))

    def _on_usb_sync_done(self, result, message: str, error, quiet: bool = False) -> None:
        thread = self._usb_sync_thread
        self._usb_idle()
        self.cancel_btn.setVisible(False)
        if thread is not None:
            thread.wait()
        self._usb_sync_thread = None
        if error:
            self.usb_status.setText(f"USB sync failed: {error}")
            self.ctx.notify("USB sync failed")
        notes = getattr(result, "library_notes", None) if result is not None else None
        data_changed = notes is not None and notes.received()
        if not error:
            # "Everything is in sync" covers the playlists too: the files
            # and the library data each get their own say, both directions
            one_way = thread is not None and thread.plan.one_way
            files = "Files: " + (("nothing new on the USB" if one_way else "everything is in sync")
                                 if quiet else result.summary())
            other = message
            if notes is not None and message:
                # update_library's own note (tracks added…) without the
                # library-data summary it was joined to
                other = message.replace(notes.summary(), "").strip(" ·")
            parts = [files] + ([other] if other else [])
            if notes is not None:
                parts.append(notes.status())
            text = " · ".join(parts)
            if result.errors:
                text += "\n" + "\n".join(result.errors[:5])
            self.usb_status.setText(text)
            if not quiet:
                self.ctx.notify(f"USB sync: {result.summary()}")
            elif notes is not None and notes.summary() != "Library data already in sync":
                self.ctx.notify(f"USB: {notes.summary()}")
        if data_changed:
            self.ctx.libraryChanged.emit()
            self.ctx.playlistsChanged.emit()
        if notes is not None and notes.prefs_in:
            # playback & look arrived - show them, play by them
            self._load_playback_prefs()
            self.ctx.player.reload_prefs()
            saved_look = skins.load_choice()
            for kind, box in self.look_combos.items():
                box.blockSignals(True)
                box.setCurrentIndex(max(0, box.findData(saved_look[kind])))
                box.blockSignals(False)
            self._sync_look_status()
        if result is not None and getattr(result, "artwork_changed", False):
            clear_art_cache()
        if result is not None and (result.new_local_paths or result.removed_local_paths):
            self.ctx.libraryChanged.emit()
            self.ctx.videosChanged.emit()
        self._refresh_usb_controls()

    def _cancel_usb(self) -> None:
        for thread in (self._usb_compare_thread, self._usb_sync_thread):
            if thread is not None and thread.isRunning():
                thread.requestInterruption()
                self.usb_cancel_btn.setEnabled(False)
                self.ctx.notify("Cancelling — finishing the current file…")

    def _pick_usb_root(self) -> str:  # pragma: no cover - file dialog
        return QFileDialog.getExistingDirectory(self, "Choose the USB drive (its top folder)")

    def setup_usb_drive(self) -> None:
        """Write the MusicMgr marker to a drive the user picks, so it's found
        whatever letter it gets. Existing folders are left exactly as they
        are; watched folders get paired with same-named folders on it."""
        path = self._pick_usb_root()
        if not path:
            return
        root = Path(path)
        if root.anchor and Path(root.anchor) != root:
            confirm = QMessageBox.question(
                self,
                "Set up USB drive",
                f"{root} isn't the top folder of a drive. Use it as the sync root anyway? "
                "Folder pairs are matched by name inside it.",
                QMessageBox.Yes | QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        try:
            drive = usb_sync.write_marker(root, "MusicMgr USB")
        except OSError as exc:
            QMessageBox.warning(self, "Set up USB drive", f"Couldn't write to {root}: {exc}")
            return
        self._usb_drive = drive
        self._refresh_usb_controls()
        self.ctx.notify(f"{drive.describe()} is set up for USB sync")

    def edit_usb_pairs(self) -> None:
        if self._usb_drive is None:
            return
        UsbPairsDialog(self._usb_drive, parent=self).exec()
        self._usb_pending_plan = None
        self._refresh_usb_controls()

    def empty_usb_deleted(self) -> None:
        if self._usb_drive is None:
            return
        folder = usb_sync.usb_deleted_dir(self._usb_drive)
        if not folder.is_dir():
            return
        size = usb_sync.folder_size(folder)
        confirm = QMessageBox.question(
            self,
            "Empty _deleted",
            f"Permanently delete the {human_size(size)} of files the USB sync set aside in "
            f"{folder}? These are files deleted from the USB (or replaced in a conflict) "
            "during earlier syncs.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        import shutil

        shutil.rmtree(folder, ignore_errors=True)
        self.ctx.notify("USB _deleted folder emptied")
        self._refresh_usb_controls()
