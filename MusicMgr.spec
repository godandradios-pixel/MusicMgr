# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the Windows .exe distribution.

Run from the repo root, on Windows (PyInstaller does not cross-compile -
see the "build" doc comment at the bottom of this file for the full
Windows build steps):

    pyinstaller MusicMgr.spec

Output lands at dist/MusicMgr.exe - a single self-extracting file (--onefile),
matching the existing portable build already in use (2026-09-08's, found on
James's USB stick next to its data/ folder: one MusicMgr.exe, no _internal/
folder alongside it) rather than PyInstaller 6+'s --onedir default, which
would split the same content across MusicMgr.exe plus a separate
_internal/ folder that has to be copied alongside it. One file is simpler
to drop onto a USB stick or a kiosk machine, at the cost of unpacking
itself to a per-run temp directory on every launch rather than reading
straight off disk - a real tradeoff (slower startup) but the one already
in production for this app. config.py's `_BUNDLE_ROOT`/`sys._MEIPASS`
handling (see its own comments) already accounts for both modes, so
nothing there needed to change.

Only the Qt submodules the app actually imports (QtCore/QtGui/QtWidgets/
QtMultimedia/QtMultimediaWidgets - see the `grep` this was built from) are
collected; everything else PySide6 ships (Qt Quick/QML, Qt3D, WebEngine,
Charts, ...) is excluded below rather than left for PyInstaller to
auto-detect, since a blanket PySide6 collection balloons the dist folder
with modules nothing here ever imports. PyInstaller's own bundled
hook-PySide6.QtMultimedia.py (not anything in this file) is what pulls in
the actual multimedia backend plugins - confirmed by a throwaway Linux
smoke build actually loading its FFmpeg backend at runtime. An earlier cut
of this spec also ran collect_all() on QtMultimedia/QtMultimediaWidgets by
hand, from the same worry that plugin bundling is the finicky part of Qt
packaging - PyInstaller reported both calls as no-ops (neither is a
Python package with data files of its own to collect), so it's gone.

data/ (the user's library.db/artwork/artists - real personal data, not
app assets) is deliberately NOT bundled here - see config.py's
`_resolve_data_dir()`. Ship without a data/ folder next to the exe and it
uses %APPDATA%\\MusicMgr on first run; put an (empty, or copied-over) data/
folder next to MusicMgr.exe instead for portable mode.
"""

from PyInstaller.utils.hooks import collect_all

datas = [("musicmgr/assets", "musicmgr/assets")]
binaries = []
hiddenimports = []

# mutagen picks its format module (mp3/flac/mp4/...) by file extension at
# runtime rather than importing all of them up front anywhere PyInstaller's
# static analysis can see - without this, files of a format nobody
# happened to `from mutagen.X import Y` elsewhere in the codebase would
# fail to read only in the packaged exe, not when run from source.
_mutagen_datas, _mutagen_binaries, _mutagen_hidden = collect_all("mutagen")
datas += _mutagen_datas
binaries += _mutagen_binaries
hiddenimports += _mutagen_hidden

excludes = [
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSensors",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtTest",
    "PySide6.QtHelp",
    "PySide6.QtDesigner",
    "PySide6.QtUiTools",
    "PySide6.QtSql",
]

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MusicMgr",
    # a touch-kiosk app has no terminal to read a console window from -
    # flip to True only temporarily, to see a Python traceback if a fresh
    # build crashes on launch instead of opening (see the build doc at the
    # end of this file)
    console=False,
    icon="musicmgr/assets/app_icon.ico",
)
