"""2026-10-14 - James: "After a scan on D:\\Music why doesn't it pick up the
added album Da Doo Ron Ron for the Crystals".

The scan did pick it up, but its files carry the Compilation flag (TCMP=1 -
MusicBrainz types a single-artist "Very Best of" as "Album, Compilation"),
and MusicMgr took the flag to mean "various artists": the album was hidden
from the Crystals' page and its cover named "Various Artists - <title>".
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from musicmgr import config
from musicmgr.db.models import Artist, Release, Track
from musicmgr.services import artwork_names
from musicmgr.services import library as lib
from musicmgr.services import scanner


def make_tagged_mp3(path: Path, title: str, artist: str, album: str,
                    album_artist: str, compilation: bool) -> None:
    from mutagen.id3 import TALB, TCMP, TIT2, TPE1, TPE2
    from mutagen.mp3 import MP3

    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytes([0xFF, 0xFB, 0x90, 0x00])
    frame = header + bytes(417 - len(header))
    with open(path, "wb") as f:
        for _ in range(5):
            f.write(frame)
    audio = MP3(str(path))
    audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=[title]))
    audio.tags.add(TPE1(encoding=3, text=[artist]))
    audio.tags.add(TPE2(encoding=3, text=[album_artist]))
    audio.tags.add(TALB(encoding=3, text=[album]))
    if compilation:
        audio.tags.add(TCMP(encoding=3, text=["1"]))
    audio.save()


class TestIsVariousArtists:
    def test_the_flag_alone_doesnt_make_an_artists_own_album_a_compilation(self):
        assert not scanner.is_various_artists("The Crystals", ["The Crystals"], flagged=True)
        assert not scanner.is_various_artists("Crystals", ["The Crystals"], flagged=True)

    def test_various_artists_is_always_a_compilation(self):
        assert scanner.is_various_artists("Various Artists")
        assert scanner.is_various_artists("VA", ["Someone"], flagged=False)

    def test_flagged_with_other_performers_still_is(self):
        assert scanner.is_various_artists("Phil Spector", ["The Crystals", "The Ronettes"],
                                          flagged=True)

    def test_unflagged_with_guests_isnt(self):
        assert not scanner.is_various_artists("The Crystals", ["The Crystals", "Darlene Love"])

    def test_folder_decision(self):
        flagged = {
            Path("a.mp3"): scanner.TrackTags(artist="The Crystals", album_artist="The Crystals",
                                             had_explicit_album_artist=True, compilation=True),
            Path("b.mp3"): scanner.TrackTags(artist="The Crystals", album_artist="The Crystals",
                                             had_explicit_album_artist=True, compilation=True),
        }
        assert scanner._folder_album_artist(flagged) == ("The Crystals", False)
        untagged = {
            Path("a.mp3"): scanner.TrackTags(artist="The Crystals", album_artist="",
                                             compilation=True),
            Path("b.mp3"): scanner.TrackTags(artist="The Crystals", album_artist="",
                                             compilation=True),
        }
        assert scanner._folder_album_artist(untagged) == ("The Crystals", False)


class TestScan:
    def test_a_flagged_best_of_lands_on_the_artists_page(self, session, tmp_path):
        album = tmp_path / "The Crystals" / "Da Doo Ron Ron  The Very Best of The Crystals"
        for n, title in enumerate(("Uptown", "He's a Rebel", "Da Doo Ron Ron"), 1):
            make_tagged_mp3(album / f"{n:02} {title}.mp3", title, "The Crystals",
                            "Da Doo Ron Ron: The Very Best of The Crystals", "The Crystals",
                            compilation=True)
        various = tmp_path / "Back to Mono"
        for n, who in enumerate(("The Crystals", "The Ronettes"), 1):
            make_tagged_mp3(various / f"{n:02} Song.mp3", f"Song {n}", who, "Back to Mono",
                            "Various Artists", compilation=True)

        scanner.scan_folder(session, tmp_path)

        best_of = session.scalar(select(Release).where(Release.title.like("Da Doo%")))
        assert best_of.is_compilation is False
        assert session.scalar(select(Release).where(Release.title == "Back to Mono")).is_compilation
        crystals = session.get(Artist, best_of.album_artist_id)
        assert crystals in lib.list_artists(session)
        assert len(lib.list_tracks_for_album_artist(session, crystals.id)) >= 3


class TestFixExisting:
    def _release(self, session, title, album_artist, performers, cover=None):
        artist = Artist(name=album_artist, name_key=album_artist.casefold())
        session.add(artist)
        session.flush()
        rel = Release(title=title, title_key=title.casefold(), artist_display=album_artist,
                      is_compilation=True, album_artist_id=artist.id,
                      cover_path=str(cover) if cover else None)
        session.add(rel)
        session.flush()
        for n, who in enumerate(performers):
            session.add(Track(release_id=rel.id, title=f"Song {n}", title_key=f"song {n}",
                              artist_display=who, position=n))
        session.flush()
        return rel

    def test_single_artist_releases_are_unflagged_and_keep_their_cover(
            self, session, tmp_path, monkeypatch):
        art = tmp_path / "artwork"
        art.mkdir()
        monkeypatch.setattr(config, "ART_DIR", art)
        old = art / "Various Artists - Greatest Hits.jpg"
        old.write_bytes(b"\xff\xd8\xff\xe0 picture")
        hits = self._release(session, "Greatest Hits", "The Crystals",
                             ["The Crystals", "The Crystals"], cover=old)
        other = self._release(session, "Greatest Hits", "The Ronettes", ["The Ronettes"],
                              cover=old)          # two albums sharing one file
        back = self._release(session, "Back to Mono", "Various Artists",
                             ["The Crystals", "The Ronettes"])

        assert lib.fix_single_artist_compilations(session) == 2

        assert hits.is_compilation is False and other.is_compilation is False
        assert back.is_compilation is True
        assert Path(hits.cover_path).name == "The Crystals - Greatest Hits.jpg"
        assert Path(hits.cover_path).read_bytes() == old.read_bytes()
        assert Path(other.cover_path).name == "The Ronettes - Greatest Hits.jpg"
        assert old.exists()               # copied, never moved
        assert artwork_names.release_stem(hits) == "The Crystals - Greatest Hits"
        assert lib.fix_single_artist_compilations(session) == 0
