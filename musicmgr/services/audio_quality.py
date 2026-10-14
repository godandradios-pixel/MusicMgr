"""A short "how good is this file" label for what's playing (2026-10-13).

James: "With the radio there is a LIVE pill at the bottom. For other songs,
can we have a Mono Stereo or bit rate or some other indicator that can be
used to describe the quality of the file". The player bar shows the label in
a pill where LIVE sits for a station (ui/widgets/player_bar.py), and the
phone remote shows it under the album.

    FLAC · 16/44.1 · Stereo        lossless: bit depth / sample rate
    FLAC · 24/96 · Stereo          (hi-res is just the bigger numbers)
    MP3 · 320 kbps · Stereo        lossy: bitrate
    MP3 · 128 kbps · Mono          a 78 transfer

Read straight from the file's own header with mutagen (only the header -
no decoding), once per file, and cached. Nothing is stored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

#: mutagen module name -> (label, lossless)
_CODECS = {
    "flac": ("FLAC", True),
    "oggflac": ("FLAC", True),
    "mp3": ("MP3", False),
    "oggvorbis": ("Ogg Vorbis", False),
    "oggopus": ("Opus", False),
    "wave": ("WAV", True),
    "aiff": ("AIFF", True),
    "asf": ("WMA", False),
    "monkeysaudio": ("APE", True),
    "wavpack": ("WavPack", True),
    "aac": ("AAC", False),
    "dsf": ("DSD", True),
    "dsdiff": ("DSD", True),
    "trueaudio": ("TTA", True),
    "optimfrog": ("OptimFROG", True),
    "musepack": ("Musepack", False),
}


@dataclass(frozen=True)
class Quality:
    #: what the pill says - "FLAC · 16/44.1 · Stereo"
    label: str
    #: the longer version for its tooltip
    detail: str
    lossless: bool


def _channels(n: Optional[int]) -> str:
    if not n:
        return ""
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(n, f"{n} channels")


def _khz(rate: Optional[int]) -> str:
    if not rate:
        return ""
    k = rate / 1000
    return f"{k:.2f}".rstrip("0").rstrip(".")


def _codec(info) -> tuple[str, bool]:
    module = type(info).__module__.rsplit(".", 1)[-1]
    if module == "mp4":
        codec = (getattr(info, "codec", "") or "").lower()
        if codec.startswith("alac"):
            return "ALAC", True
        return "AAC", False
    if module == "asf":
        name = (getattr(info, "codec_name", "") or "")
        if "lossless" in name.lower():
            return "WMA Lossless", True
    return _CODECS.get(module, (module.upper() or "Audio", False))


def describe_info(info) -> Optional[Quality]:
    """The label for a mutagen `.info` object (or anything with the same
    attributes) - separate from `describe` so it can be tested without
    files."""
    if info is None:
        return None
    name, lossless = _codec(info)
    rate = getattr(info, "sample_rate", None) or None
    bits = getattr(info, "bits_per_sample", None) or None
    bitrate = getattr(info, "bitrate", None) or None
    kbps = int(round(bitrate / 1000)) if bitrate else None
    chans = _channels(getattr(info, "channels", None))
    vbr = str(getattr(info, "bitrate_mode", "")).upper().endswith("VBR")

    parts = [name]
    if lossless and bits and rate:
        parts.append(f"{bits}/{_khz(rate)}")
    elif lossless and rate:
        parts.append(f"{_khz(rate)} kHz")
    elif kbps:
        parts.append(f"{kbps} kbps{' VBR' if vbr else ''}")
    if chans:
        parts.append(chans)

    detail = [f"{name} ({'lossless' if lossless else 'compressed'})"]
    if bits:
        detail.append(f"{bits}-bit")
    if rate:
        detail.append(f"{_khz(rate)} kHz")
    if chans:
        detail.append(chans.lower() if chans in ("Mono", "Stereo") else chans)
    if kbps:
        detail.append(f"{kbps:,} kbps{' (variable)' if vbr else ''}")
    return Quality(" · ".join(parts), ", ".join(detail), lossless)


@lru_cache(maxsize=512)
def _describe_cached(path: str, mtime: float) -> Optional[Quality]:
    try:
        import mutagen

        audio = mutagen.File(path)
    except Exception:
        return None
    return describe_info(getattr(audio, "info", None)) if audio is not None else None


def describe(path: Optional[str]) -> Optional[Quality]:
    """The quality label for an audio file, or None when it can't be read."""
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return _describe_cached(path, mtime)
