"""2026-10-19 - James: "help me clean up these Unknown question marks".

"? and the Mysterians" came apart into "?" + "the Mysterians"; "?" (no
letters) keyed as "unknown artist", the same artist every untagged file's
"Unknown Artist" lands on; and untagged music videos named "Artist - Title
(Official Video).mp4" in a flat Videos folder had no artist at all."""

from __future__ import annotations

import wave
from pathlib import Path

from sqlalchemy import select

from musicmgr.db.models import Artist, Credit, MediaFile, Track
from musicmgr.services import library as lib
from musicmgr.services import scanner
from musicmgr.services.matching import split_artists


def make_silent_wav(path: Path, seconds: float = 0.2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))


def make_tagged_mp3(path: Path, title: str, artist: str, album: str) -> None:
    from mutagen.id3 import TALB, TIT2, TPE1
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
    audio.tags.add(TALB(encoding=3, text=[album]))
    audio.save()


def performers(session, track: Track) -> list[tuple[str, str]]:
    return sorted(
        (c.role, session.get(Artist, c.artist_id).name)
        for c in session.scalars(select(Credit).where(Credit.track_id == track.id))
        if c.role in (Credit.ROLE_MAIN, Credit.ROLE_FEATURING)
    )


class TestSplitArtists:
    def test_a_name_starting_with_punctuation_stays_whole(self):
        assert split_artists("? and the Mysterians") == ["? and the Mysterians"]
        assert split_artists("!!!") == ["!!!"]

    def test_a_letterless_piece_is_dropped(self):
        assert split_artists("Meyer Davis Orch. (vocal trio S. Ballew and )") == [
            "Meyer Davis Orch. (vocal trio S. Ballew"
        ]

    def test_ordinary_credits_still_split(self):
        assert split_artists("Daft Punk feat. Pharrell Williams & Nile Rodgers") == [
            "Daft Punk", "Pharrell Williams", "Nile Rodgers",
        ]


class TestLetterlessArtistNames:
    def test_question_mark_is_its_own_artist_not_unknown(self, session):
        mark = lib.get_or_create_artist(session, "?")
        unknown = lib.get_or_create_artist(session, "Unknown Artist")
        blank = lib.get_or_create_artist(session, "")

        assert mark.id != unknown.id
        assert mark.name == "?"
        assert blank.id == unknown.id and unknown.name == "Unknown Artist"


class TestArtistTitleFromFilename:
    def test_video_noise_is_dropped(self):
        f = scanner._artist_title_from_stem
        assert f("Colbie Caillat - Realize (Official Music Video)") == ("Colbie Caillat", "Realize")
        assert f("Kid Rock - Cowboy [Official Music Video]") == ("Kid Rock", "Cowboy")
        assert f("Ella Langley - Excuse The Mess – Official Visualizer") == (
            "Ella Langley", "Excuse The Mess")
        assert f("Ella Langley - you look like you love me (Official Video)(0)") == (
            "Ella Langley", "you look like you love me")
        assert f("Ella Langley - 20-20 (Official Visualizer)") == ("Ella Langley", "20-20")

    def test_real_title_parts_are_kept(self):
        f = scanner._artist_title_from_stem
        assert f("Journey - Separate Ways (Worlds Apart)") == ("Journey", "Separate Ways (Worlds Apart)")
        assert f("AC-DC - Thunderstruck (Live)") == ("AC-DC", "Thunderstruck (Live)")

    def test_not_artist_title(self):
        assert scanner._artist_title_from_stem("01 - Intro") is None
        assert scanner._artist_title_from_stem("Intro") is None

    def test_an_untagged_video_in_a_flat_folder_gets_its_artist(self, session, tmp_path):
        make_silent_wav(tmp_path / "Videos" / "Colbie Caillat - Realize (Official Music Video).wav")

        scanner.scan_folder(session, tmp_path / "Videos")

        track = session.scalar(select(Track))
        assert (track.title, track.artist_display) == ("Realize", "Colbie Caillat")
        assert performers(session, track) == [("Main", "Colbie Caillat")]

    def test_a_tagged_file_keeps_its_tags(self, session, tmp_path):
        make_tagged_mp3(tmp_path / "Videos" / "Journey - Separate Ways (Official Video).mp3",
                        "Separate Ways (Worlds Apart)", "Journey", "Music Videos")

        scanner.scan_folder(session, tmp_path / "Videos")

        track = session.scalar(select(Track))
        assert (track.title, track.artist_display) == ("Separate Ways (Worlds Apart)", "Journey")


class TestRepairUnknownArtistMerge:
    def test_moves_the_band_and_the_videos_out_from_under_question_mark(self, session, tmp_path):
        band = tmp_path / "Music" / "(Question Mark) and the Mysterians" / "96 Tears" / "96 Tears.mp3"
        make_tagged_mp3(band, "96 Tears", "? and the Mysterians", "96 Tears")
        video = tmp_path / "Videos" / "Kid Rock - Cowboy [Official Music Video].wav"
        make_silent_wav(video)
        scanner.scan_folder(session, tmp_path / "Music")
        scanner.scan_folder(session, tmp_path / "Videos")
        # the library as the old rules left it
        old_mark = Artist(name="?", name_key="unknown artist", name_sort="?")
        session.add(old_mark)
        session.flush()
        for path, title, display in ((band, "96 Tears", "? and the Mysterians"),
                                     (video, video.stem, "Unknown Artist")):
            track = session.scalar(select(MediaFile).where(MediaFile.path == str(path))).track
            track.title, track.artist_display = title, display
            for credit in session.scalars(select(Credit).where(Credit.track_id == track.id)):
                session.delete(credit)
            session.flush()
            lib.add_credit(session, old_mark, Credit.ROLE_MAIN, track=track)
        session.flush()

        reread = scanner.repair_unknown_artist_merge(session)

        assert reread == 2
        band_track = session.scalar(select(MediaFile).where(MediaFile.path == str(band))).track
        video_track = session.scalar(select(MediaFile).where(MediaFile.path == str(video))).track
        assert performers(session, band_track) == [("Main", "? and the Mysterians")]
        assert (video_track.title, video_track.artist_display) == ("Cowboy", "Kid Rock")
        assert performers(session, video_track) == [("Main", "Kid Rock")]
        assert session.get(Artist, old_mark.id) is None
        assert scanner.repair_unknown_artist_merge(session) == 0
