"""Settings › Appearance (split out of settings.py, 2026-10-22)."""

from __future__ import annotations

import sys
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox
from ....services import updater
from ... import skins
from ...widgets.common import TouchButton, dim_label


class AppearanceSection:
    """Settings › Appearance: a mixin of SettingsView (view.py) - its
    methods share the view's state."""

    def _build_appearance(self, section) -> None:
        """Build the Appearance group (moved out of __init__, 2026-10-22)."""
        # ---- appearance (2026-10-13) ----
        # James: "I would like the ability for the users to change 'skins'.
        # Right now it's the brown pallete" ... "apply at restart. and the
        # jukebox, radio and turntable can also get their own finish
        # choice." A skin for the app, a finish for each of the three
        # pieces of equipment - see ui/skins.py. Saved per PC; shown from
        # the next start (Restart now).
        body = section("Appearance", "appearance")
        look_card = QFrame()
        look_card.setObjectName("Card")
        lgrid = QGridLayout(look_card)
        lgrid.setContentsMargins(20, 14, 20, 14)
        lgrid.setHorizontalSpacing(18)
        lgrid.setVerticalSpacing(10)
        self.look_combos: dict[str, QComboBox] = {}
        saved_look = skins.load_choice()
        for row, (kind, label) in enumerate((
            ("skin", "Skin"), ("jukebox", "Jukebox finish"),
            ("radio", "Radio finish"), ("turntable", "Turntable finish"),
        )):
            box = QComboBox()
            for name, spec in skins.TABLES[kind].items():
                box.addItem(spec["label"], name)
            box.setCurrentIndex(max(0, box.findData(saved_look[kind])))
            box.currentIndexChanged.connect(self._save_look)
            lgrid.addWidget(QLabel(label), row, 0)
            lgrid.addWidget(box, row, 1)
            self.look_combos[kind] = box
        look_row = QHBoxLayout()
        self.look_status = dim_label("")
        self.look_status.setWordWrap(True)
        self.look_restart_btn = TouchButton("Restart now")
        self.look_restart_btn.clicked.connect(self._restart_for_look)
        look_row.addWidget(self.look_status, 1)
        look_row.addWidget(self.look_restart_btn)
        lgrid.addLayout(look_row, 4, 1)
        lgrid.setColumnStretch(1, 1)
        body.addWidget(look_card)
        self._sync_look_status()

    def _chosen_look(self) -> dict:
        return {kind: box.currentData() for kind, box in self.look_combos.items()}

    def _save_look(self) -> None:
        try:
            skins.save_choice(self._chosen_look())
        except OSError as exc:
            self.ctx.notify(f"Couldn't save the appearance ({exc})")
        self._sync_look_status()

    def _sync_look_status(self) -> None:
        pending = self._chosen_look() != skins.ACTIVE
        self.look_status.setText(
            "Restart MusicMgr to see the new look." if pending else
            "Each PC keeps its own choice.")
        self.look_restart_btn.setVisible(pending)
        sec = getattr(self, "_sections", {}).get("appearance")
        if sec is not None:
            sec.refresh_summary()

    def _look_summary(self) -> str:
        names = [self.look_combos[k].currentText() for k in ("skin", "jukebox", "radio", "turntable")]
        text = f"{names[0]}  ·  jukebox {names[1].lower()}  ·  radio {names[2].lower()}  ·  " \
               f"turntable {names[3].lower()}"
        return text + ("  ·  restart to apply" if self._chosen_look() != skins.ACTIVE else "")

    def _restart_for_look(self) -> None:
        exe = self._current_exe()
        if exe is None:
            # running from source (python run.py) - there's no exe to start
            self.ctx.notify("Close MusicMgr and start it again to see the new look")
            return
        busy = self._busy_with()
        if busy:
            self.ctx.notify(busy)
            return
        try:
            updater.relaunch(exe, sys.argv, after_update=False)
        except OSError as exc:
            QMessageBox.warning(self, "Restart", f"Couldn't restart ({exc}). "
                                                 "Close MusicMgr and open it again.")
            return
        self.window().close()
