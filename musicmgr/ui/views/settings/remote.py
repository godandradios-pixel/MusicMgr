"""Settings › Phone remote."""

from __future__ import annotations

import sys
from typing import Optional
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)
from ....services import voice_ask, web_remote
from ...theme import COLORS
from ...widgets.common import TouchButton, dim_label


class RemoteSection:
    """Settings › Phone remote: a mixin of SettingsView (view.py) - its
    methods share the view's state."""

    def _build_remote(self, section) -> None:
        """Build the Remote group."""
        # ---- phone remote ----
        # Control this PC's MusicMgr from a phone's browser on the same
        # network (remote control only, no login). See services/web_remote.py.
        # Per PC, so each machine is turned on - and gets its own address -
        # separately.
        body = section("Phone remote", "remote")
        remote_card = QFrame()
        remote_card.setObjectName("Card")
        rgrid = QGridLayout(remote_card)
        rgrid.setContentsMargins(20, 14, 20, 14)
        rgrid.setHorizontalSpacing(18)
        rgrid.setVerticalSpacing(10)
        # an On/Off button, not a checkbox: the theme's checkbox square is
        # nearly invisible on a Card. Still called remote_cb and checkable,
        # so isChecked/setChecked/toggled work - _sync_remote_toggle dresses it.
        self.remote_state = QLabel("")
        self.remote_cb = TouchButton("")
        self.remote_cb.setCheckable(True)
        self.remote_cb.setMinimumWidth(260)
        toggle_row = QHBoxLayout()
        toggle_row.setSpacing(16)
        toggle_row.addWidget(self.remote_state)
        toggle_row.addStretch(1)
        toggle_row.addWidget(self.remote_cb)
        self.remote_name_edit = QLineEdit()
        self.remote_name_edit.setPlaceholderText(web_remote.computer_name())
        self.remote_name_edit.setToolTip("Shown at the top of the phone page, so you can tell your PCs apart")
        self.remote_name_edit.setMaxLength(60)
        self.remote_port_spin = QSpinBox()
        self.remote_port_spin.setRange(1024, 65535)
        self.remote_port_spin.setKeyboardTracking(False)
        self.remote_port_spin.setMinimumWidth(130)
        self.remote_port_spin.setToolTip(
            f"Usually {web_remote.DEFAULT_PORT}. Change it only if another program already uses it."
        )
        self.remote_status = dim_label("")
        self.remote_status.setWordWrap(True)
        self.remote_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.remote_qr = QLabel()
        self.remote_qr.setVisible(False)
        # shown when Windows calls this network Public or the firewall has
        # no rule letting the phone in; see web_remote's "Windows networks"
        self.remote_fix_btn = TouchButton("Fix it")
        self.remote_fix_btn.setToolTip(
            "Lets the phone in: sets this network to Private and adds a Windows Firewall "
            "rule for the phone remote's port. Windows asks for permission first.")
        self.remote_fix_btn.setVisible(False)
        self.remote_fix_btn.clicked.connect(self._fix_remote_network)
        rgrid.addLayout(toggle_row, 0, 0, 1, 3)
        rgrid.addWidget(QLabel("Name on the phone"), 1, 0)
        rgrid.addWidget(self.remote_name_edit, 1, 1)
        rgrid.addWidget(QLabel("Port"), 2, 0)
        rgrid.addWidget(self.remote_port_spin, 2, 1, alignment=Qt.AlignLeft)
        status_box = QVBoxLayout()
        status_box.setSpacing(8)
        status_box.addWidget(self.remote_status)
        status_box.addWidget(self.remote_fix_btn, alignment=Qt.AlignLeft)
        rgrid.addLayout(status_box, 3, 0, 1, 2)
        rgrid.addWidget(self.remote_qr, 1, 2, 3, 1, alignment=Qt.AlignRight | Qt.AlignTop)
        # Ask (services/voice_ask.py) - spoken requests from the
        # phone's Ask box. The AI needs a key for the chosen model - Gemini's
        # is free (Google AI Studio), Claude's is paid (Anthropic); without
        # one only the plain phrasings work (offline). Each key is kept.
        self.ask_model_combo = QComboBox()
        for model_id, label, _provider in voice_ask.MODELS:
            self.ask_model_combo.addItem(label, model_id)
        self.ask_gemini_key_edit = QLineEdit()
        self.ask_gemini_key_edit.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.ask_gemini_key_edit.setPlaceholderText("Free key from aistudio.google.com")
        self.ask_gemini_key_edit.setToolTip(
            "For the Gemini choices. Free: sign in at aistudio.google.com and click "
            "'Get API key'. Saved on this PC only.")
        self.ask_key_edit = QLineEdit()
        self.ask_key_edit.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.ask_key_edit.setPlaceholderText("sk-ant-…  (from console.anthropic.com)")
        self.ask_key_edit.setToolTip("For the Claude choices (paid). Saved on this PC only.")
        self.ask_status = dim_label("")
        self.ask_status.setWordWrap(True)
        rgrid.addWidget(QLabel("Ask with"), 4, 0)
        rgrid.addWidget(self.ask_model_combo, 4, 1, alignment=Qt.AlignLeft)
        rgrid.addWidget(QLabel("Google AI Studio key"), 5, 0)
        rgrid.addWidget(self.ask_gemini_key_edit, 5, 1, 1, 2)
        rgrid.addWidget(QLabel("Anthropic API key"), 6, 0)
        rgrid.addWidget(self.ask_key_edit, 6, 1, 1, 2)
        rgrid.addWidget(self.ask_status, 7, 0, 1, 3)
        rgrid.setColumnStretch(1, 1)
        body.addWidget(remote_card)
        self._load_ask_prefs()
        self.ask_key_edit.editingFinished.connect(self._save_ask_prefs)
        self.ask_gemini_key_edit.editingFinished.connect(self._save_ask_prefs)
        self.ask_model_combo.currentIndexChanged.connect(lambda _i: self._save_ask_prefs())
        self._load_remote_prefs()
        self._sync_remote_toggle()
        self.remote_cb.toggled.connect(lambda _on: self._save_remote_prefs())
        self.remote_port_spin.valueChanged.connect(lambda _v: self._save_remote_prefs())
        self.remote_name_edit.editingFinished.connect(self._save_remote_prefs)
        self.ctx.web_remote.statusChanged.connect(lambda *_: self._refresh_remote_status())
        # a bound method, queued: the check finishes on a background thread
        self.ctx.web_remote.networkChecked.connect(self._refresh_remote_status, Qt.QueuedConnection)
        self._refresh_remote_status()

    def _remote_prefs_from_controls(self) -> web_remote.RemotePrefs:
        return web_remote.RemotePrefs(
            enabled=self.remote_cb.isChecked(),
            port=self.remote_port_spin.value(),
            name=self.remote_name_edit.text().strip(),
        )

    def _load_remote_prefs(self) -> None:
        with self.ctx.session() as db:
            prefs = web_remote.load_prefs(db)
        for w in (self.remote_cb, self.remote_port_spin, self.remote_name_edit):
            w.blockSignals(True)
        self.remote_cb.setChecked(prefs.enabled)
        self.remote_port_spin.setValue(prefs.port)
        self.remote_name_edit.setText(prefs.name)
        for w in (self.remote_cb, self.remote_port_spin, self.remote_name_edit):
            w.blockSignals(False)

    def _save_remote_prefs(self) -> None:
        prefs = self._remote_prefs_from_controls()
        with self.ctx.session() as db:
            web_remote.save_prefs(db, prefs)
            db.commit()
        server = self.ctx.web_remote
        self._remote_error = ""
        if prefs.enabled:
            ok, message = server.start(prefs.port, prefs.name)
            if not ok:
                self._remote_error = message
        else:
            server.set_name(prefs.name)
            server.stop()
        self._refresh_remote_status()

    def _load_ask_prefs(self) -> None:
        with self.ctx.session() as db:
            prefs = voice_ask.load_prefs(db)
        widgets = (self.ask_key_edit, self.ask_gemini_key_edit, self.ask_model_combo)
        for w in widgets:
            w.blockSignals(True)
        self.ask_key_edit.setText(prefs.api_key)
        self.ask_gemini_key_edit.setText(prefs.gemini_key)
        self.ask_model_combo.setCurrentIndex(max(0, self.ask_model_combo.findData(prefs.model)))
        for w in widgets:
            w.blockSignals(False)
        self._refresh_ask_status(prefs)

    def _save_ask_prefs(self) -> None:
        prefs = voice_ask.AskPrefs(
            api_key=self.ask_key_edit.text().strip(),
            model=self.ask_model_combo.currentData() or voice_ask.DEFAULT_MODEL,
            gemini_key=self.ask_gemini_key_edit.text().strip(),
        )
        with self.ctx.session() as db:
            voice_ask.save_prefs(db, prefs)
            db.commit()
        self._refresh_ask_status(prefs)

    def _refresh_ask_status(self, prefs) -> None:
        gemini = prefs.provider == voice_ask.GEMINI
        if prefs.active_key and gemini:
            self.ask_status.setText(
                "Ask: the phone's Ask box sends what you say to Gemini (free tier), which picks "
                "songs from this library. Google may use free-tier requests to improve its products.")
        elif prefs.active_key:
            self.ask_status.setText(
                "Ask: the phone's Ask box sends what you say to Claude, which picks songs from "
                "this library (about a cent a request).")
        else:
            which = "a free Google AI Studio key" if gemini else "an Anthropic API key"
            self.ask_status.setText(
                f"Ask: add {which} above. Until then the phone's Ask box only understands plain "
                "requests - “play Elvis”, “top 20 of 1972”, “number ones from 1965”, "
                "“playlist Road Trip”.")

    def _sync_remote_toggle(self) -> None:
        """Dress the On/Off button and the big state line for whether the
        phone remote is turned on: off -> a bright "Turn on" button, on ->
        a green "ON" and a plain "Turn off" button."""
        on = self.remote_cb.isChecked()
        btn = self.remote_cb
        btn.setText("Turn phone remote off" if on else "Turn phone remote on")
        btn.setObjectName("" if on else "PrimaryWarm")
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        if on:
            self.remote_state.setText("Phone remote is ON")
            self.remote_state.setStyleSheet("font-size: 18px; font-weight: 700; color: #7cc47f;")
        else:
            self.remote_state.setText("Phone remote is OFF")
            self.remote_state.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {COLORS['text_dim']};")

    def _remote_summary(self) -> str:
        server = self.ctx.web_remote
        if server.running:
            urls = server.urls()
            return f"On · {urls[0]}" if urls else "On"
        return "Off"

    def _refresh_remote_status(self) -> None:
        self._sync_remote_toggle()
        sec = getattr(self, "_sections", {}).get("remote")
        if sec is not None:
            sec.refresh_summary()
        server = self.ctx.web_remote
        error = getattr(self, "_remote_error", "")
        if server.running:
            if server.network is None:
                server.check_network()  # refreshes this again when it's done
            urls = server.urls()
            info = server.network
            if urls:
                links = "<br>".join(f"<b>{u}</b>" for u in urls)
                text = (
                    "On your phone, open Safari and go to<br>" + links + "<br>"
                    "or point the Camera at the code. The phone has to be on the same "
                    "network as this PC. In Safari, Share › Add to Home Screen makes it an icon."
                )
                problems = self._remote_problems(info, server.port)
                if problems:
                    text += ("<br><br><span style='color:#e0866f'><b>⚠ The phone can't get in yet:"
                             "</b></span><br>" + "<br>".join(f"• {p}" for p in problems)
                             + "<br>Click <b>Fix it</b> - Windows asks for permission once.")
                elif sys.platform.startswith("win") and info is None:
                    text += "<br>Checking Windows' network and firewall settings…"
                self._show_remote_qr(urls[0])
            else:
                text = ("Running, but this PC doesn't seem to be on a network right now - "
                        f"connect to Wi-Fi or Ethernet, then use this PC's address with port {server.port}.")
                self._show_remote_qr(None)
            self.remote_fix_btn.setVisible(bool(urls) and info is not None and info.needs_fix)
            self.remote_status.setTextFormat(Qt.RichText)
            self.remote_status.setText(text)
        else:
            self._show_remote_qr(None)
            self.remote_fix_btn.setVisible(False)
            self.remote_status.setTextFormat(Qt.PlainText)
            if error:
                self.remote_status.setText(error)
            elif self.remote_cb.isChecked():
                self.remote_status.setText("Not running")
            else:
                self.remote_status.setText(
                    "Turn it on to play, pause, skip, change the volume and pick "
                    "playlists, albums or radio stations from your phone."
                )

    @staticmethod
    def _remote_problems(info, port: int) -> list[str]:
        """What stops the phone reaching this PC, in words (Windows only)."""
        if info is None:
            return []
        out = []
        for n in info.public:
            where = "Wi-Fi" if "wi-fi" in n.alias.lower() else "Ethernet"
            out.append(f"Windows has “{n.name or n.alias}” set as a Public network (Windows "
                       f"Settings › Network &amp; internet › {where} › Network profile type).")
        if info.firewall_blocked:
            out.append("Windows Firewall has a rule blocking MusicMgr (left by a dismissed "
                       "“Allow access?” prompt).")
        if info.firewall_closed:
            out.append(f"Windows Firewall has no rule letting the phone reach port {port}.")
        return out

    def _fix_remote_network(self) -> None:
        """Fix what the network check found - Public network to Private,
        MusicMgr's firewall rule - in an Administrator PowerShell behind
        Windows' own permission prompt, then check again."""
        server = self.ctx.web_remote
        info = server.network
        if info is None or not info.needs_fix:
            return
        if not web_remote.run_elevated_powershell(web_remote.fix_command(info, server.port)):
            self.ctx.notify("Couldn't start PowerShell - see the steps above to fix it by hand")
            return
        self.remote_fix_btn.setEnabled(False)
        self.remote_fix_btn.setText("Waiting for Windows…")

        def recheck():
            self.remote_fix_btn.setEnabled(True)
            self.remote_fix_btn.setText("Fix it")
            server.check_network()

        # give the permission prompt time to be answered, then look again
        for delay in (6000, 15000, 30000):
            QTimer.singleShot(delay, self, recheck)

    def _show_remote_qr(self, url: Optional[str]) -> None:
        matrix = web_remote.qr_matrix(url) if url else None
        if not matrix:
            self.remote_qr.clear()
            self.remote_qr.setVisible(False)
            return
        from PySide6.QtGui import QColor, QPainter, QPixmap

        n = len(matrix)
        cell = max(3, 168 // n)
        pix = QPixmap(n * cell, n * cell)
        pix.fill(QColor("white"))
        painter = QPainter(pix)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("black"))
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    painter.drawRect(x * cell, y * cell, cell, cell)
        painter.end()
        self.remote_qr.setPixmap(pix)
        self.remote_qr.setToolTip(url)
        self.remote_qr.setVisible(True)
