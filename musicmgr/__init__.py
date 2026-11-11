"""MusicMgr - local touchscreen music library and player."""

#: The release version (SemVer). Single source of truth for the in-app
#: updater (services/updater.py compares GitHub release tags against it)
#: and for the release pipeline (.github/workflows/release.yml refuses to
#: build a `vX.Y.Z` tag that doesn't match this line). Bump it in the same
#: commit you tag.
__version__ = "2.6.6"
