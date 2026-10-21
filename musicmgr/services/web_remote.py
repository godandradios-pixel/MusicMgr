"""Phone remote - control MusicMgr from a phone's web browser (2026-10-13).

James: "Web Access remote to MusicMgr" - "I have multiple PC and Linux PCs
running Music Mgr at different physical locations. I would like to be able
to connect to any one of them via my iPhone". Remote control only (the music
plays on the PC's own speakers), home network only, no login.

Each MusicMgr runs its own small web server when Settings › Phone remote is
turned on; the phone opens http://<that PC's address>:<port>/ in Safari
(Settings shows the address and a QR code to scan). Every PC serves the same
page with its own name at the top, so "Add to Home Screen" once per PC gives
one icon per machine.

How it fits together:

* `RemoteServer` runs a `ThreadingHTTPServer` on a background thread.
* Nothing on that thread touches Qt or the database. Every request that
  needs the player or the library is handed to the Qt main thread through
  `_Bridge` (a queued signal) and the request thread waits for the answer -
  the player and SQLAlchemy objects are only ever used from the thread they
  belong to, exactly as when the person taps the screen.
* `RemoteApi` is that main-thread side: plain methods returning JSON-able
  dicts, testable without any HTTP at all.
* The page itself is `musicmgr/assets/web_remote.html` - one self-contained
  file; it polls /api/state about once a second.

Ask (2026-10-15): POST /api/ask {"text": ...} plays what a spoken request
asks for - see services/voice_ask.py. It runs on the request thread (the AI
can take several seconds) with every library look-up handed to the main
thread like everything else; `RemoteApi.play_ask` plays the picks and keeps
what was playing so the `ask_queue_instead` command can put it back.

Safety without a login: the server answers only devices on the local network
(private, loopback and link-local addresses - see `_allowed_client`), so a
router port-forward set up by mistake still gets nothing; and every command
must carry an `X-MusicMgr` header, which a web page on another site can't add
to a request without the browser first asking this server's permission
(which it never gives).
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import logging
import mimetypes
import socket
import sys
import threading
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QObject, Qt, Signal
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from .. import config
from ..db.models import Artist, Playlist, RadioShow, RadioStation, Release, Setting, Track
from . import voice_ask

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
KEY_ENABLED = "web_remote_enabled"
KEY_PORT = "web_remote_port"
KEY_NAME = "web_remote_name"

PAGE_FILE = config.ASSETS_DIR / "web_remote.html"
#: how long a request waits for the main thread before giving up
MAIN_THREAD_TIMEOUT_S = 8.0
#: the header every command must carry - see the module docstring
COMMAND_HEADER = "X-MusicMgr"


# --------------------------------------------------------------------------
# preferences (the generic `settings` table, per PC)
# --------------------------------------------------------------------------


@dataclass
class RemotePrefs:
    enabled: bool = False
    port: int = DEFAULT_PORT
    #: shown at the top of the phone page; blank = this computer's name
    name: str = ""


def load_prefs(session) -> RemotePrefs:
    prefs = RemotePrefs()
    row = session.get(Setting, KEY_ENABLED)
    if row is not None:
        prefs.enabled = row.value == "1"
    row = session.get(Setting, KEY_PORT)
    if row is not None and row.value:
        try:
            port = int(row.value)
            if 1024 <= port <= 65535:
                prefs.port = port
        except ValueError:
            pass
    row = session.get(Setting, KEY_NAME)
    if row is not None and row.value:
        prefs.name = row.value.strip()
    return prefs


def save_prefs(session, prefs: RemotePrefs) -> None:
    for key, value in (
        (KEY_ENABLED, "1" if prefs.enabled else "0"),
        (KEY_PORT, str(int(prefs.port))),
        (KEY_NAME, prefs.name.strip()),
    ):
        row = session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
    session.flush()


def computer_name() -> str:
    try:
        return socket.gethostname() or "MusicMgr"
    except OSError:  # pragma: no cover
        return "MusicMgr"


def _route_address() -> Optional[str]:
    """The address this PC would use to reach the internet - the one on the
    home network the phone is on. The UDP "connect" never sends a packet; it
    only asks the OS which address it would use.

    2026-10-15: this used to ask about 10.255.255.255, which a virtual
    adapter (Hyper-V's "Default Switch", WSL, Docker, a VPN) can claim; a
    real internet address always follows the default route instead."""
    for target in ("8.8.8.8", "1.1.1.1", "10.255.255.255"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((target, 80))
                addr = s.getsockname()[0]
        except OSError:
            continue
        if addr and not addr.startswith(("0.", "127.")):
            return addr
    return None


def _hostname_addresses() -> list[str]:
    try:
        return [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        return []


def _usable(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_unspecified or ip.is_link_local)


def local_addresses() -> list[str]:
    """This PC's IPv4 addresses on its local network(s), best guess first:
    the default-route address, then the others. An address ending in .1
    other than that one is left out - it's almost always the PC's own side
    of a virtual network (Hyper-V's Default Switch 172.x.x.1, Docker's
    172.17.0.1, libvirt's 192.168.122.1), which a phone can never reach;
    a PC is hardly ever its home network's router."""
    route = _route_address()
    out: list[str] = []
    for addr in ([route] if route else []) + _hostname_addresses():
        if not _usable(addr) or addr in out:
            continue
        if addr != route and addr.endswith(".1"):
            continue
        out.append(addr)
    return out


# --------------------------------------------------------------------------
# Windows networks (2026-10-15)
# --------------------------------------------------------------------------
#
# James's iPhone couldn't open the remote ("Safari couldn't open the page
# because the server stopped responding"): Windows had his home network
# "912Latonka" set as *Public*, and MusicMgr is allowed through Windows
# Firewall on Private networks only, so the phone's connection was dropped
# silently. The QR code also could have offered the Hyper-V "Default
# Switch" address (172.23.192.1). So on Windows, Settings › Phone remote now
# asks Windows which networks are really connected (Get-NetConnectionProfile
# - virtual adapters have none), lists only their addresses, and warns, with
# a one-click fix, when one of them is Public.

#
# Same day, follow-up: with the network Private the phone still couldn't get
# in - Windows Firewall had no rule for MusicMgr at all (the "Allow access?"
# prompt never came up, or was dismissed), and Windows blocks incoming
# connections that no rule allows. So the check also looks for MusicMgr's own
# firewall rule (FIREWALL_RULE: inbound TCP on the remote's port, Private and
# Domain networks - a port rule, so updating MusicMgr to a new exe doesn't
# undo it) and for Block rules on the MusicMgr program, which a dismissed
# prompt leaves behind and which beat any Allow rule. One "Fix it" button
# repairs all of it behind a single Windows permission prompt.

FIREWALL_RULE = "MusicMgr phone remote"

_PS_CHECK = (
    "$nets = @(Get-NetConnectionProfile | ForEach-Object { $p = $_; "
    "$ips = @(Get-NetIPAddress -InterfaceIndex $p.InterfaceIndex -AddressFamily IPv4 "
    "-ErrorAction SilentlyContinue | ForEach-Object { $_.IPAddress }); "
    "[pscustomobject]@{ name = [string]$p.Name; alias = [string]$p.InterfaceAlias; "
    "category = [string]$p.NetworkCategory; ips = $ips } }); "
    f"$ports = @(Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue | "
    "Where-Object { [string]$_.Enabled -eq 'True' -and [string]$_.Action -eq 'Allow' -and "
    "[string]$_.Direction -eq 'Inbound' } | Get-NetFirewallPortFilter | "
    "ForEach-Object { [string]$_.LocalPort }); "
    "$blocked = @(Get-NetFirewallApplicationFilter | Where-Object { $_.Program -like '*MusicMgr*' } | "
    "Get-NetFirewallRule | Where-Object { [string]$_.Enabled -eq 'True' -and "
    "[string]$_.Action -eq 'Block' -and [string]$_.Direction -eq 'Inbound' }).Count; "
    "[pscustomobject]@{ networks = $nets; ports = $ports; blocked = $blocked } | "
    "ConvertTo-Json -Compress -Depth 4"
)


@dataclass
class HomeNetwork:
    name: str
    alias: str
    category: str  # "Public", "Private" or "DomainAuthenticated"
    ips: list[str]

    @property
    def public(self) -> bool:
        return self.category.lower() == "public"


@dataclass
class WindowsCheck:
    networks: list[HomeNetwork]
    #: ports MusicMgr's own Allow rule opens
    allowed_ports: list[str]
    #: inbound Block rules on the MusicMgr program
    blocked: int


def _powershell(command: str, timeout: float = 20.0) -> Optional[str]:
    import subprocess

    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout, **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse_networks(data) -> list[HomeNetwork]:
    """Get-NetConnectionProfile rows (one object, or a list of them)."""
    out = []
    for row in _as_list(data):
        if not isinstance(row, dict):
            continue
        ips = [str(i) for i in _as_list(row.get("ips")) if _usable(str(i))]
        out.append(HomeNetwork(str(row.get("name") or ""), str(row.get("alias") or ""),
                               str(row.get("category") or ""), ips))
    return out


def parse_check(text: Optional[str]) -> Optional[WindowsCheck]:
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        blocked = int(data.get("blocked") or 0)
    except (TypeError, ValueError):
        blocked = 0
    return WindowsCheck(parse_networks(data.get("networks")),
                        [str(p) for p in _as_list(data.get("ports"))], blocked)


def windows_check() -> Optional[WindowsCheck]:
    """Windows' connected networks and MusicMgr's firewall rules, or None
    off Windows or if PowerShell couldn't say. Takes a few seconds - call it
    off the main thread."""
    if sys.platform != "win32":
        return None
    return parse_check(_powershell(_PS_CHECK))


@dataclass
class NetworkInfo:
    #: best first - the QR code uses the first
    addresses: list[str]
    #: Windows networks set to Public (the firewall blocks the phone there)
    public: list[HomeNetwork] = field(default_factory=list)
    #: no MusicMgr Allow rule for this port (Windows only)
    firewall_closed: bool = False
    #: Block rules on the MusicMgr program (Windows only)
    firewall_blocked: int = 0

    @property
    def needs_fix(self) -> bool:
        return bool(self.public or self.firewall_closed or self.firewall_blocked)


def network_info(check: Optional[WindowsCheck] = None, port: int = DEFAULT_PORT) -> NetworkInfo:
    """Addresses for the phone plus anything on Windows that would stop it.
    With Windows' own network list, only those networks' addresses are
    offered (the default-route one first); otherwise local_addresses()."""
    if check is None:
        return NetworkInfo(local_addresses())
    addresses: list[str] = []
    if check.networks:
        route = _route_address()
        real = [ip for n in check.networks for ip in n.ips]
        ordered = ([route] if route in real else []) + [ip for ip in real if ip != route]
        addresses = list(dict.fromkeys(ordered))
    return NetworkInfo(
        addresses or local_addresses(),
        [n for n in check.networks if n.public],
        firewall_closed=str(port) not in check.allowed_ports,
        firewall_blocked=check.blocked,
    )


def fix_command(info: NetworkInfo, port: int) -> str:
    """PowerShell (needs Administrator) that fixes what network_info found:
    Public networks to Private, MusicMgr's Block rules removed, and its
    Allow rule (re)made for this port."""
    parts = []
    for n in info.public:
        alias = n.alias.replace("'", "''")
        parts.append(f"Set-NetConnectionProfile -InterfaceAlias '{alias}' -NetworkCategory Private")
    if info.firewall_blocked:
        parts.append(
            "Get-NetFirewallApplicationFilter | Where-Object { $_.Program -like '*MusicMgr*' } | "
            "Get-NetFirewallRule | Where-Object { [string]$_.Action -eq 'Block' -and "
            "[string]$_.Direction -eq 'Inbound' } | Remove-NetFirewallRule")
    if info.firewall_closed or info.firewall_blocked:
        parts.append(f"Remove-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -ErrorAction SilentlyContinue")
        parts.append(
            f"New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -Direction Inbound -Action Allow "
            f"-Protocol TCP -LocalPort {int(port)} -Profile Private,Domain")
    return "; ".join(parts)


def run_elevated_powershell(command: str) -> bool:
    """Start PowerShell as Administrator (Windows shows its UAC prompt) to
    run `command`. Returns False if it couldn't be started. The command goes
    over as -EncodedCommand, so no quoting can get mangled on the way."""
    if sys.platform != "win32" or not command:
        return False
    import base64
    import subprocess

    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    outer = (f"Start-Process powershell -Verb RunAs -WindowStyle Hidden "
             f"-ArgumentList '-NoProfile','-EncodedCommand','{encoded}'")
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", outer],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return False
    return True


def _allowed_client(addr: str) -> bool:
    """Only devices on the local network - see the module docstring."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


# --------------------------------------------------------------------------
# the main-thread side
# --------------------------------------------------------------------------


def _art_url(kind: str, ident, path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    # the file's name rides along so a changed picture isn't served from
    # the phone's cache
    tag = zlib.crc32(path.encode("utf-8", "replace")) % 100000
    return f"/api/art?kind={kind}&id={ident}&v={tag}"


def _track_dict(track: Track) -> dict:
    release = track.release
    return {
        "id": track.id,
        "title": track.title,
        "artist": track.artist_display or (release.artist_display if release else "") or "",
        "album": release.title if release else "",
        "release_id": track.release_id,
        "duration_ms": track.duration_ms or 0,
    }


def _letter(sort_key: str) -> str:
    """The A-Z strip's letter for a sort key: A-Z, else '#' (digits, symbols,
    accented or non-Latin first letters)."""
    first = (sort_key or "").strip()[:1].upper()
    return first if "A" <= first <= "Z" else "#"


class RemoteApi:
    """Everything the phone can ask for or do. Main thread only."""

    def __init__(self, ctx, name: Callable[[], str]) -> None:
        self.ctx = ctx
        self._name = name
        self.queue_rev = 0
        self.stream_title = ""
        player = ctx.player
        player.queueChanged.connect(self._bump_queue)
        player.trackChanged.connect(self._on_track)
        player.streamTitleChanged.connect(self._on_stream_title)

    # -- bookkeeping ------------------------------------------------------

    def _bump_queue(self) -> None:
        self.queue_rev += 1

    def _on_track(self, _item) -> None:
        self.stream_title = ""
        self.queue_rev += 1

    def _on_stream_title(self, title: str) -> None:
        self.stream_title = title or ""

    # -- reading ----------------------------------------------------------

    def state(self) -> dict:
        player = self.ctx.player
        item = player.current
        playing = player.is_playing()
        tuner = getattr(self.ctx, "tuner", None)
        tuned = list(tuner.tuned) if tuner is not None else ["", 0]
        out: dict[str, Any] = {
            "name": self._name(),
            "version": _version(),
            "state": "playing" if playing else ("paused" if item is not None else "stopped"),
            "volume": round(player.volume, 3),
            "queue_rev": self.queue_rev,
            "queue_index": player.current_index,
            "queue_len": len(player.queue),
            "tuned": tuned,
            "item": None,
        }
        if item is not None:
            live = item.is_stream
            out["item"] = {
                "kind": item.kind,
                "title": item.title,
                "artist": item.artist,
                "album": item.album,
                "track_id": item.track_id or None,
                "release_id": item.release_id,
                "station_id": item.station_id,
                "live": live,
                "stream_title": self.stream_title if live else "",
                # 2026-10-13 - the player bar's quality pill (FLAC · 16/44.1 · Stereo)
                "quality": "" if live else _quality_label(item.path),
                "cover": _art_url("current", item.track_id or item.station_id or 0, item.cover_path),
                "position_ms": 0 if live else int(player.position() or 0),
                "duration_ms": 0 if live else int(player.duration() or 0),
            }
        return out

    def queue(self) -> dict:
        player = self.ctx.player
        items = []
        for idx, item in enumerate(player.queue):
            items.append({
                "index": idx,
                "title": item.title,
                "artist": item.artist,
                "album": item.album,
                "duration_ms": item.duration_ms or 0,
                "kind": item.kind,
            })
        return {"rev": self.queue_rev, "current": player.current_index, "items": items}

    def playlists(self) -> dict:
        from . import playlists as pl_svc

        with self.ctx.session() as s:
            folders = [
                {"id": f.id, "name": f.name, "parent_id": f.parent_id,
                 "cover": _art_url("folder", f.id, f.cover_path)}
                for f in pl_svc.list_folders(s)
            ]
            lists = [
                {"id": p.id, "name": p.name, "folder_id": p.folder_id, "kind": p.kind,
                 "cover": _art_url("playlist", p.id, p.cover_path)}
                for p in pl_svc.list_playlists(s)
            ]
        return {"folders": folders, "playlists": lists}

    def playlist(self, playlist_id: int) -> dict:
        from . import playlists as pl_svc

        with self.ctx.session() as s:
            pl = s.get(Playlist, playlist_id)
            if pl is None:
                raise LookupError("No such playlist")
            tracks = [_track_dict(t) for t in pl_svc.playlist_tracks(s, playlist_id)]
            return {"id": pl.id, "name": pl.name, "tracks": tracks}

    def release(self, release_id: int) -> dict:
        with self.ctx.session() as s:
            rel = s.get(Release, release_id)
            if rel is None:
                raise LookupError("No such album")
            return {
                "id": rel.id,
                "title": rel.title,
                "artist": rel.artist_display or "",
                "year": rel.year,
                "cover": _art_url("release", rel.id, rel.cover_path),
                "tracks": [_track_dict(t) for t in rel.tracks],
            }

    def search(self, text: str) -> dict:
        text = (text or "").strip()
        if len(text) < 2:
            return {"albums": [], "tracks": []}
        like = f"%{text}%"
        with self.ctx.session() as s:
            albums = s.scalars(
                select(Release)
                .where(or_(Release.title.ilike(like), Release.artist_display.ilike(like)))
                .order_by(func.lower(Release.artist_display), Release.year, func.lower(Release.title))
                .limit(40)
            ).all()
            tracks = s.scalars(
                select(Track)
                .options(selectinload(Track.release))
                .where(or_(Track.title.ilike(like), Track.artist_display.ilike(like)))
                .order_by(func.lower(Track.artist_display), func.lower(Track.title))
                .limit(80)
            ).unique().all()
            return {
                "albums": [
                    {"id": r.id, "title": r.title, "artist": r.artist_display or "",
                     "year": r.year, "cover": _art_url("release", r.id, r.cover_path)}
                    for r in albums
                ],
                "tracks": [_track_dict(t) for t in tracks],
            }

    # 2026-10-18 - Artists and Albums tabs (James: "I would like Artist and
    # album to appear as an option"). Same rules as the desktop Library
    # grids (ui/views/library.py _load_artists/_load_albums): an artist is
    # someone a non-compilation release is filed under, and their page shows
    # only those releases, so the list's count and the page always agree.

    def artists(self) -> dict:
        with self.ctx.session() as s:
            rows = s.execute(
                select(
                    Artist,
                    func.count(func.distinct(Release.id)),
                    func.count(func.distinct(Track.id)),
                )
                .join(Release, Release.album_artist_id == Artist.id)
                .outerjoin(Track, Track.release_id == Release.id)
                .where(Release.is_compilation.is_(False))
                .group_by(Artist.id)
            ).all()
            # no portrait? borrow the earliest release's cover, like the desktop
            covers: dict[int, tuple[int, str]] = {}
            for artist_id, rid, cover in s.execute(
                select(Release.album_artist_id, Release.id, Release.cover_path)
                .where(Release.cover_path.is_not(None), Release.album_artist_id.is_not(None))
                .order_by(func.coalesce(Release.year, 9999), Release.id)
            ):
                covers.setdefault(artist_id, (rid, cover))
            out = []
            for artist, releases, tracks in rows:
                key = (artist.name_sort or artist.name_key or artist.name or "").lower()
                if artist.image_path:
                    cover = _art_url("artist", artist.id, artist.image_path)
                elif artist.id in covers:
                    cover = _art_url("release", *covers[artist.id])
                else:
                    cover = None
                out.append({"id": artist.id, "name": artist.name, "letter": _letter(key),
                            "releases": releases or 0, "tracks": tracks or 0,
                            "cover": cover, "_key": key})
        out.sort(key=lambda a: a["_key"])
        for a in out:
            del a["_key"]
        return {"artists": out}

    def artist(self, artist_id: int) -> dict:
        with self.ctx.session() as s:
            artist = s.get(Artist, artist_id)
            if artist is None:
                raise LookupError("No such artist")
            rows = s.execute(
                select(Release, func.count(Track.id))
                .outerjoin(Track, Track.release_id == Release.id)
                .where(Release.album_artist_id == artist_id, Release.is_compilation.is_(False))
                .group_by(Release.id)
                .order_by(func.coalesce(Release.year, 9999), func.lower(Release.title))
            ).all()
            if artist.image_path:
                cover = _art_url("artist", artist.id, artist.image_path)
            else:   # no portrait: the first album with a cover, as the list shows
                first = next(((r.id, r.cover_path) for r, _n in rows if r.cover_path), None)
                cover = _art_url("release", *first) if first else None
            return {
                "id": artist.id,
                "name": artist.name,
                "cover": cover,
                "albums": [
                    {"id": r.id, "title": r.title, "year": r.year, "tracks": n or 0,
                     "cover": _art_url("release", r.id, r.cover_path)}
                    for r, n in rows
                ],
            }

    def albums(self) -> dict:
        """Every release, lightest fields only - about 6,000 on PC1. Sorted by
        title here; the phone re-sorts by artist or newest without asking again."""
        with self.ctx.session() as s:
            rows = s.execute(
                select(Release.id, Release.title, Release.title_key, Release.artist_display,
                       Release.year, Release.cover_path)
                .order_by(Release.created_at.desc(), Release.id.desc())
            ).all()
        out = []
        for n, (rid, title, title_key, artist, year, cover) in enumerate(rows):
            key = (title_key or title or "").lower()
            akey = (artist or "").lower()
            out.append({"id": rid, "title": title, "artist": artist or "", "year": year,
                        "cover": _art_url("release", rid, cover),
                        "letter": _letter(key), "aletter": _letter(akey), "new": n,
                        "_k": key, "_a": akey})
        out.sort(key=lambda a: a["_k"])
        # sort ranks the phone uses instead of string compares on 6,000 rows
        for rank, a in enumerate(sorted(out, key=lambda a: (a["_a"], a["year"] or 9999, a["_k"]))):
            a["by_artist"] = rank
        for a in out:
            del a["_k"], a["_a"]
        return {"albums": out}

    def radio(self) -> dict:
        from . import otr, stations

        with self.ctx.session() as s:
            st = [
                {"id": x.id, "name": x.name, "band": x.band or "fm",
                 "off_air": x.off_air_since is not None,
                 "cover": _art_url("station", x.id, x.favicon_path)}
                for x in stations.dial(s)
            ]
            shows = [
                {"id": x.id, "name": x.name, "band": x.band,
                 "cover": _art_url("show", x.id, x.cover_path)}
                for x in otr.list_shows(s, kinds=[RadioShow.KIND_SHOW])
            ]
        return {"stations": st, "shows": shows}

    def art_path(self, kind: str, ident: int) -> Optional[str]:
        from ..db.models import PlaylistFolder

        if kind == "current":
            item = self.ctx.player.current
            return item.cover_path if item is not None else None
        model = {
            "release": (Release, "cover_path"),
            "artist": (Artist, "image_path"),
            "playlist": (Playlist, "cover_path"),
            "folder": (PlaylistFolder, "cover_path"),
            "station": (RadioStation, "favicon_path"),
            "show": (RadioShow, "cover_path"),
        }.get(kind)
        if model is None:
            return None
        with self.ctx.session() as s:
            row = s.get(model[0], ident)
            return getattr(row, model[1]) if row is not None else None

    # -- doing ------------------------------------------------------------

    def command(self, body: dict) -> dict:
        cmd = str(body.get("cmd") or "")
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            raise ValueError(f"Unknown command: {cmd or '(none)'}")
        message = handler(body) or ""
        return {"ok": True, "message": message, "state": self.state()}

    def _live_station(self) -> bool:
        item = self.ctx.player.current
        tuner = getattr(self.ctx, "tuner", None)
        return bool(item is not None and item.is_stream and tuner is not None)

    def _cmd_toggle(self, _b) -> None:
        self.ctx.play_or_resume()

    def _cmd_next(self, _b) -> None:
        if self._live_station() and self.ctx.tuner.step_station(1):
            return
        self.ctx.player.next(user_initiated=True)

    def _cmd_previous(self, _b) -> None:
        if self._live_station() and self.ctx.tuner.step_station(-1):
            return
        self.ctx.player.previous()

    def _cmd_seek(self, b) -> None:
        self.ctx.player.seek(max(0, int(b.get("ms", 0))))

    def _cmd_skip(self, b) -> None:
        self.ctx.player.seek_relative(int(b.get("ms", 0)))

    def _cmd_volume(self, b) -> None:
        self.ctx.player.set_volume(float(b.get("value", 0.5)))

    def _cmd_jump(self, b) -> None:
        self.ctx.player.jump_to(int(b.get("index", -1)))

    def _cmd_remove(self, b) -> None:
        self.ctx.player.remove_at(int(b.get("index", -1)))

    def _play(self, tracks, start: int, shuffle: bool, source: str) -> str:
        if not tracks:
            return "Nothing to play"
        self.ctx.player.set_shuffle(bool(shuffle))
        n = self.ctx.play_tracks(tracks, start=start, source=source)
        return f"Playing {n} track{'s' if n != 1 else ''}" if n else "No playable files"

    def _cmd_play_playlist(self, b) -> str:
        from . import playlists as pl_svc

        pid = int(b.get("id", 0))
        with self.ctx.session() as s:
            tracks = pl_svc.playlist_tracks(s, pid)
            return self._play(tracks, int(b.get("start", 0)), bool(b.get("shuffle")), f"playlist:{pid}")

    def _cmd_play_release(self, b) -> str:
        rid = int(b.get("id", 0))
        with self.ctx.session() as s:
            rel = s.get(Release, rid)
            if rel is None:
                raise LookupError("No such album")
            tracks = list(rel.tracks)
            start = int(b.get("start", 0))
            return self._play(tracks, start, bool(b.get("shuffle")), "library")

    def _cmd_play_track(self, b) -> str:
        """A search result: its album, starting at that song."""
        tid = int(b.get("id", 0))
        with self.ctx.session() as s:
            track = s.get(Track, tid)
            if track is None:
                raise LookupError("No such track")
            tracks = list(track.release.tracks) if track.release is not None else [track]
            start = next((i for i, t in enumerate(tracks) if t.id == tid), 0)
            return self._play(tracks, start, False, "library")

    def _cmd_enqueue_track(self, b) -> str:
        tid = int(b.get("id", 0))
        with self.ctx.session() as s:
            track = s.get(Track, tid)
            if track is None:
                raise LookupError("No such track")
            n = self.ctx.enqueue_tracks([track], play_next=bool(b.get("next")))
        return ("Playing next" if b.get("next") else "Added to queue") if n else "No playable file"

    def _cmd_enqueue_release(self, b) -> str:
        rid = int(b.get("id", 0))
        with self.ctx.session() as s:
            rel = s.get(Release, rid)
            if rel is None:
                raise LookupError("No such album")
            n = self.ctx.enqueue_tracks(list(rel.tracks), play_next=bool(b.get("next")))
        return f"{n} track{'s' if n != 1 else ''} added" if n else "No playable files"

    def _cmd_tune_station(self, b) -> str:
        from . import stations

        sid = int(b.get("id", 0))
        with self.ctx.session() as s:
            browser = stations.opens_in_browser(s.get(RadioStation, sid))
        ok = self.ctx.tuner.tune_station(sid)
        if browser:   # a SiriusXM channel (2026-10-21): it opens on the PC
            return "Opened in the PC's web browser" if ok else "Couldn't open the PC's web browser"
        return "Tuned in" if ok else "Couldn't tune that station"

    def _cmd_tune_show(self, b) -> str:
        ok = self.ctx.tuner.tune_show(int(b.get("id", 0)))
        return "Tuned in" if ok else "Nothing to play for that show"

    def _cmd_tune_onair(self, _b) -> str:
        ok = self.ctx.tuner.tune_onair()
        return "On the air" if ok else "Nothing to broadcast yet"

    # -- Ask (2026-10-15, services/voice_ask.py) ---------------------------

    def play_ask(self, result) -> dict:
        """Play what a spoken request picked, remembering what was playing
        so the phone's "Add to queue instead" can put it back."""
        player = self.ctx.player
        out = {"ok": True, "title": result.title, "message": result.message,
               "engine": result.engine, "count": 0, "songs": [], "can_queue_instead": False}
        if not result.track_ids:
            out["ok"] = False
            return out
        with self.ctx.session() as s:
            found = {t.id: t for t in s.scalars(
                select(Track).options(selectinload(Track.release))
                .where(Track.id.in_(result.track_ids))
            ).unique()}
            tracks = [found[i] for i in result.track_ids if i in found]
            out["songs"] = [_track_dict(t) for t in tracks]
            before = self._snapshot()
            player.set_shuffle(bool(result.shuffle))
            n = self.ctx.play_tracks(tracks, start=0, source="ask") if tracks else 0
        out["count"] = n
        if not n:
            out["ok"] = False
            out["message"] = result.message or "None of those songs have a playable file"
            return out
        self._ask_before = before
        self._ask_ids = [t["id"] for t in out["songs"]]
        out["can_queue_instead"] = before is not None
        out["state"] = self.state()
        return out

    def _snapshot(self):
        player = self.ctx.player
        item = player.current
        if item is None or not player.queue:
            return None
        return {
            "items": player.queue,
            "index": player.current_index,
            "position_ms": 0 if item.is_stream else int(player.position() or 0),
            "playing": player.is_playing(),
            "shuffle": player.shuffle,
            "source": player.source,
        }

    def _cmd_ask_queue_instead(self, _b) -> str:
        """Undo the last Ask's "play now": what was playing comes back where
        it was, and the picks are queued after it."""
        before, ids = getattr(self, "_ask_before", None), getattr(self, "_ask_ids", None)
        if not before or not ids:
            return "Nothing to put back"
        self._ask_before = self._ask_ids = None
        player = self.ctx.player
        items = [dataclasses.replace(it) for it in before["items"]]
        index = before["index"]
        if 0 <= index < len(items):
            items[index] = dataclasses.replace(items[index], start_ms=before["position_ms"])
        player.set_shuffle(bool(before["shuffle"]))
        player.set_queue(items, start=max(index, 0), source=before["source"])
        if not before["playing"]:
            player.pause()
        with self.ctx.session() as s:
            found = {t.id: t for t in s.scalars(select(Track).where(Track.id.in_(ids)))}
            n = self.ctx.enqueue_tracks([found[i] for i in ids if i in found])
        return f"Put back what was playing - {n} song{'s' if n != 1 else ''} queued after it"


def _quality_label(path: str) -> str:
    from .audio_quality import describe

    q = describe(path)
    return q.label if q else ""


def _version() -> str:
    try:
        from .. import __version__

        return __version__
    except Exception:  # pragma: no cover
        return ""


# --------------------------------------------------------------------------
# main-thread hand-off
# --------------------------------------------------------------------------


class _Job:
    __slots__ = ("fn", "done", "result", "error")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None


class _Bridge(QObject):
    """Runs callables on the thread this object lives on (the main thread)."""

    submit = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.submit.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, job: _Job) -> None:
        try:
            job.result = job.fn()
        except BaseException as exc:  # handed back to the request thread
            job.error = exc
        finally:
            job.done.set()

    def call(self, fn: Callable[[], Any], timeout: float = MAIN_THREAD_TIMEOUT_S) -> Any:
        job = _Job(fn)
        self.submit.emit(job)
        if not job.done.wait(timeout):
            raise TimeoutError("MusicMgr is busy - try again")
        if job.error is not None:
            raise job.error
        return job.result


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "MusicMgr"
    protocol_version = "HTTP/1.1"

    # set on the subclass made per server
    remote: "RemoteServer"

    def log_message(self, fmt, *args) -> None:  # quiet - no console spam
        log.debug("web remote %s - " + fmt, self.client_address[0], *args)

    # -- plumbing ---------------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "message": message}, status)

    def _guard(self) -> bool:
        if not _allowed_client(self.client_address[0]):
            self._error(403, "Only devices on this network can use the remote")
            return False
        return True

    def _main(self, fn):
        return self.remote.bridge.call(fn)

    # -- routes -----------------------------------------------------------

    def do_HEAD(self) -> None:  # pragma: no cover - browsers rarely send it
        self.do_GET()

    def do_GET(self) -> None:
        if not self._guard():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        api = self.remote.api
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, self.remote.page(), "text/html; charset=utf-8")
            elif url.path in ("/icon.png", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
                icon = self.remote.icon_png
                if icon:
                    self._send(200, icon, "image/png", "max-age=86400")
                else:
                    self._error(404, "No icon")
            elif url.path == "/api/state":
                self._json(self._main(api.state))
            elif url.path == "/api/queue":
                self._json(self._main(api.queue))
            elif url.path == "/api/playlists":
                self._json(self._main(api.playlists))
            elif url.path == "/api/playlist":
                pid = int(q.get("id", 0))
                self._json(self._main(lambda: api.playlist(pid)))
            elif url.path == "/api/release":
                rid = int(q.get("id", 0))
                self._json(self._main(lambda: api.release(rid)))
            elif url.path == "/api/search":
                text = q.get("q", "")
                self._json(self._main(lambda: api.search(text)))
            elif url.path == "/api/artists":
                self._json(self._main(api.artists))
            elif url.path == "/api/artist":
                aid = int(q.get("id", 0))
                self._json(self._main(lambda: api.artist(aid)))
            elif url.path == "/api/albums":
                self._json(self._main(api.albums))
            elif url.path == "/api/radio":
                self._json(self._main(api.radio))
            elif url.path == "/api/art":
                self._art(q.get("kind", ""), int(q.get("id", 0) or 0))
            else:
                self._error(404, "Not found")
        except LookupError as exc:
            self._error(404, str(exc))
        except (ValueError, TypeError) as exc:
            self._error(400, str(exc))
        except TimeoutError as exc:
            self._error(503, str(exc))
        except Exception as exc:  # pragma: no cover - logged, reported
            log.exception("web remote GET %s failed", self.path)
            self._error(500, f"Something went wrong: {exc}")

    def _art(self, kind: str, ident: int) -> None:
        path = self._main(lambda: self.remote.api.art_path(kind, ident))
        if not path:
            self._error(404, "No picture")
            return
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError:
            self._error(404, "Picture missing")
            return
        ctype = mimetypes.guess_type(p.name)[0] or "image/jpeg"
        cache = "no-cache" if kind == "current" else "max-age=3600"
        self._send(200, data, ctype, cache)

    def do_POST(self) -> None:
        if not self._guard():
            return
        if self.headers.get(COMMAND_HEADER) is None:
            self._error(403, "Missing remote header")
            return
        url = urlparse(self.path)
        if url.path == "/api/ask":
            self._ask()
            return
        if url.path != "/api/cmd":
            self._error(404, "Not found")
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            self._json(self._main(lambda: self.remote.api.command(body)))
        except LookupError as exc:
            self._error(404, str(exc))
        except (ValueError, TypeError) as exc:
            self._error(400, str(exc))
        except TimeoutError as exc:
            self._error(503, str(exc))
        except Exception as exc:  # pragma: no cover
            log.exception("web remote command failed")
            self._error(500, f"Something went wrong: {exc}")


    def _ask(self) -> None:
        """A spoken request (2026-10-15). Runs on this request thread - the AI
        can take a while - with every library look-up handed to the main
        thread; one at a time per PC."""
        remote = self.remote
        if not remote.ask_lock.acquire(blocking=False):
            self._error(409, "Still working on the last request")
            return
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 16 * 1024)
            body = json.loads(self.rfile.read(length) or b"{}")
            text = str(body.get("text") or "")[:500] if isinstance(body, dict) else ""
            prefs = remote.run_with_session(voice_ask.load_prefs)
            result = remote.asker.ask(text, prefs)
            self._json(self._main(lambda: remote.api.play_ask(result)))
        except TimeoutError as exc:
            self._error(503, str(exc))
        except (ValueError, TypeError) as exc:
            self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover
            log.exception("web remote ask failed")
            self._error(500, f"Something went wrong: {exc}")
        finally:
            remote.ask_lock.release()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # a second MusicMgr on the same port should fail loudly

    def handle_error(self, request, client_address) -> None:  # pragma: no cover
        # a phone dropping the connection mid-reply is routine
        log.debug("web remote: connection from %s ended early", client_address[0])


class RemoteServer(QObject):
    """Owns the web server; create and use it on the main thread."""

    #: running state changed - (running, message)
    statusChanged = Signal(bool, str)
    #: the network check finished (see check_network) - Settings re-reads
    networkChecked = Signal()

    def __init__(self, ctx, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.bridge = _Bridge(self)
        self.api: Optional[RemoteApi] = None
        self._httpd: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._name = ""
        self.port = DEFAULT_PORT
        self.icon_png: bytes = b""
        self._page_cache: Optional[bytes] = None
        #: spoken requests (2026-10-15) - one at a time
        self.ask_lock = threading.Lock()
        self.asker = voice_ask.Asker(self.run_with_session)
        #: last check_network() answer; None until one finishes
        self.network: Optional[NetworkInfo] = None
        self._network_busy = False

    def run_with_session(self, fn: Callable[[Any], Any]) -> Any:
        """fn(session) on the main thread - from any thread."""
        def job():
            with self.ctx.session() as s:
                return fn(s)
        if threading.current_thread() is threading.main_thread():
            return job()
        return self.bridge.call(job)

    # -- info -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def display_name(self) -> str:
        return self._name or computer_name()

    def set_name(self, name: str) -> None:
        self._name = (name or "").strip()

    def urls(self) -> list[str]:
        addresses = self.network.addresses if self.network is not None else local_addresses()
        return [f"http://{addr}:{self.port}/" for addr in addresses]

    def check_network(self) -> None:
        """Ask Windows (on a background thread) which networks are really
        connected, whether any is Public and whether the firewall lets the
        phone in; networkChecked fires when done."""
        if self._network_busy:
            return
        self._network_busy = True

        port = self.port

        def work():
            try:
                info = network_info(windows_check(), port)
            except Exception:  # pragma: no cover - never break Settings over it
                log.exception("web remote: network check failed")
                info = network_info(None)
            self.network = info
            self._network_busy = False
            self.networkChecked.emit()

        threading.Thread(target=work, name="web-remote-network", daemon=True).start()

    def page(self) -> bytes:
        if self._page_cache is None:
            try:
                self._page_cache = PAGE_FILE.read_bytes()
            except OSError:
                self._page_cache = b"<!doctype html><title>MusicMgr</title><p>The remote page is missing from this install."
        return self._page_cache

    # -- lifecycle --------------------------------------------------------

    def start(self, port: int, name: str = "") -> tuple[bool, str]:
        """Start (or restart on a new port). Returns (ok, message)."""
        self.set_name(name)
        if self.running and port == self.port:
            return True, "Running"
        self.stop()
        if self.api is None:
            self.api = RemoteApi(self.ctx, lambda: self.display_name)
        if not self.icon_png:
            self.icon_png = _icon_png()
        handler = type("Handler", (_Handler,), {"remote": self})
        try:
            httpd = _Server(("0.0.0.0", int(port)), handler)
        except OSError as exc:
            msg = f"Couldn't start on port {port} - is another program (or another MusicMgr) using it? ({exc.strerror or exc})"
            log.warning("web remote: %s", msg)
            self.statusChanged.emit(False, msg)
            return False, msg
        self._httpd = httpd
        self.port = int(port)
        self._thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.5},
            name="web-remote", daemon=True,
        )
        self._thread.start()
        self.network = None  # the port may have changed - check again
        log.info("web remote listening on port %s", self.port)
        self.statusChanged.emit(True, "Running")
        return True, "Running"

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:  # pragma: no cover
            log.exception("web remote: stopping the server failed")
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self.statusChanged.emit(False, "Off")


def _icon_png() -> bytes:
    """The app icon as a 180px PNG for the iPhone home screen."""
    try:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from PySide6.QtGui import QImage

        img = QImage(str(config.APP_ICON))
        if img.isNull():
            return b""
        img = img.scaled(180, 180, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        return bytes(data.data())
    except Exception:  # pragma: no cover
        return b""


def qr_matrix(text: str) -> Optional[list[list[bool]]]:
    """The QR code for `text` as rows of dark/light, or None when the
    optional `qrcode` package isn't installed."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    return [list(map(bool, row)) for row in qr.get_matrix()]
