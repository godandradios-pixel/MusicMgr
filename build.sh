#!/usr/bin/env bash
# Build the single-file Linux MusicMgr binary (dist/MusicMgr) - the Linux
# Mint counterpart of build.ps1. Also what the "Release" GitHub Action runs
# on ubuntu-22.04 (see .github/workflows/release.yml).
#
#   bash build.sh              install deps, bake VERSION, build
#   ./build.sh --skip-install  skip pip install (env already set up)
#
# Run it inside a venv on your own machine:
#   python3 -m venv venv && source venv/bin/activate && ./build.sh
#
# The resulting binary needs one system package on Mint/Ubuntu that Qt 6.5+
# no longer bundles:  sudo apt install libxcb-cursor0
set -euo pipefail
cd "$(dirname "$0")"

skip_install=0
for arg in "$@"; do
  case "$arg" in
    --skip-install) skip_install=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PY="${PYTHON:-python3}"

if [ "$skip_install" -eq 0 ]; then
  echo "==> pip install -r requirements-dev.txt"
  "$PY" -m pip install -r requirements-dev.txt
fi

# Same commit stamp build.ps1 bakes in (see musicmgr/version.py), no newline.
echo "==> Writing version file"
printf '%s' "$(git log -1 --format='%h - %cd' --date=short 2>/dev/null || echo unknown)" \
  > musicmgr/assets/VERSION

echo "==> pyinstaller MusicMgr.spec"
"$PY" -m PyInstaller MusicMgr.spec --noconfirm

test -x dist/MusicMgr || { echo "Build reported success but dist/MusicMgr is missing" >&2; exit 1; }
echo "==> Build complete: dist/MusicMgr ($(du -h dist/MusicMgr | cut -f1))"
