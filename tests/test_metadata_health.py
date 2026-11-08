"""Tests for services/metadata_health.py: the display-row layer behind
Settings' "Missing metadata" dashboard (ui/widgets/common.py:
MissingMetadataDialog). Three of the four queries are simple indexed
lookups dressed up with names/titles for display; the fourth
(`releases_missing_lyrics_rows`) is the one that actually touches the
filesystem (a sibling `.lrc` check per track, same as services/lyrics.py's
`find_lyrics_path`), so its own tests use real temp files rather than
mocking that check out.
"""

from __future__ import annotations

from pathlib import Path

from musicmgr.db.models import ArtistTopTrack, MediaFile
from musicmgr.services import library as lib
from musicmgr.services import metadata_health as mh
from musicmgr.services.matching import normalize


def make_track(session, title, *, artist="Artist", album="Album", **fields):
    artist_obj = lib.get_or_create_artist(session, artist)
    release = lib.get_or_create_release(session, album, artist)
    release.album_artist_id = artist_obj.id
    from musicmgr.db.models import Track

    track = Track(
        release_id=release.id,
        title=title,
        title_key=normalize(title),
        artist_display=artist,
        **fields,
    )
    session.add(track)
    session.flush()
    return artist_obj, release, track


class TestReleasesMissingCoverRows:
    def test_only_releases_with_no_cover_are_returned(self, session):
        _, has_cover, _ = make_track(session, "Song", artist="A", album="Has Cover")
        has_cover.cover_path = "/some/cover.jpg"
        _, no_cover, _ = make_track(session, "Song", artist="B", album="No Cover")
        session.flush()

        rows = mh.releases_missing_cover_rows()

        ids = {r["id"] for r in rows}
        assert no_cover.id in ids
        assert has_cover.id not in ids
        row = next(r for r in rows if r["id"] == no_cover.id)
        assert row["title"] == "No Cover"


class TestArtistsMissingBioRows:
    def test_only_artists_with_no_profile_are_returned(self, session):
        with_bio, _, _ = make_track(session, "Song", artist="Has Bio")
        with_bio.profile = "A biography."
        without_bio, _, _ = make_track(session, "Song", artist="No Bio")
        session.flush()

        rows = mh.artists_missing_bio_rows()

        ids = {r["id"] for r in rows}
        assert without_bio.id in ids
        assert with_bio.id not in ids
        row = next(r for r in rows if r["id"] == without_bio.id)
        assert row["name"] == "No Bio"


class TestArtistsMissingPopularityRows:
    def test_artist_with_top_tracks_already_fetched_is_excluded(self, session):
        fetched, _, _ = make_track(session, "Song", artist="Fetched")
        session.add(ArtistTopTrack(artist_id=fetched.id, rank=1, title="Song", playcount=100))
        not_fetched, _, _ = make_track(session, "Song", artist="Not Fetched")
        session.flush()

        rows = mh.artists_missing_popularity_rows()

        ids = {r["id"] for r in rows}
        assert not_fetched.id in ids
        assert fetched.id not in ids

    def test_artist_with_no_releases_is_excluded(self, session):
        artist = lib.get_or_create_artist(session, "No Releases")
        session.flush()

        rows = mh.artists_missing_popularity_rows()

        assert artist.id not in {r["id"] for r in rows}


class TestReleasesMissingLyricsRows:
    def _add_file(self, session, track, path: Path) -> None:
        session.add(MediaFile(track_id=track.id, path=str(path)))
        session.flush()

    def test_release_with_one_missing_lrc_is_reported_with_counts(self, session, tmp_path):
        _, release, has_lrc = make_track(session, "Track One", artist="X", album="Album")
        _, _, no_lrc = make_track(session, "Track Two", artist="X", album="Album")

        has_lrc_path = tmp_path / "track1.mp3"
        has_lrc_path.write_bytes(b"")
        has_lrc_path.with_suffix(".lrc").write_text("[00:01.00]la la la")
        no_lrc_path = tmp_path / "track2.mp3"
        no_lrc_path.write_bytes(b"")

        self._add_file(session, has_lrc, has_lrc_path)
        self._add_file(session, no_lrc, no_lrc_path)

        rows = mh.releases_missing_lyrics_rows()

        row = next(r for r in rows if r["id"] == release.id)
        assert row["title"] == "Album"
        assert row["artist"] == "X"
        assert row["total"] == 2
        assert row["missing"] == 1

    def test_release_with_all_lyrics_present_is_not_reported(self, session, tmp_path):
        _, release, track = make_track(session, "Track", artist="Y", album="Complete")
        path = tmp_path / "complete.mp3"
        path.write_bytes(b"")
        path.with_suffix(".lrc").write_text("[00:01.00]done")
        self._add_file(session, track, path)

        rows = mh.releases_missing_lyrics_rows()

        assert release.id not in {r["id"] for r in rows}

    def test_missing_media_file_is_excluded_from_the_check(self, session):
        _, release, track = make_track(session, "Track", artist="Z", album="Gone")
        session.add(MediaFile(track_id=track.id, path="/nowhere/gone.mp3", is_missing=True))
        session.flush()

        rows = mh.releases_missing_lyrics_rows()

        assert release.id not in {r["id"] for r in rows}

    def test_progress_callback_is_called_with_final_total(self, session, tmp_path):
        _, _, track = make_track(session, "Track", artist="P", album="Progress")
        path = tmp_path / "p.mp3"
        path.write_bytes(b"")
        self._add_file(session, track, path)

        calls = []
        mh.releases_missing_lyrics_rows(progress=lambda done, total, name: calls.append((done, total)))

        assert calls
        assert calls[-1] == (1, 1)


class TestReleasesMissingLyricsRowsShouldStop:
    """2026-09-22 - James: "add a real cancel button that stops any process
    running within Settings" - this whole module is read-only, so
    `should_stop` (checked once per row) just means fewer rows get
    checked - no partial-write risk to guard against at all."""

    def _add_file(self, session, track, path: Path) -> None:
        session.add(MediaFile(track_id=track.id, path=str(path)))
        session.flush()

    def test_should_stop_true_from_the_start_reports_nothing(self, session, tmp_path):
        _, release, no_lrc = make_track(session, "Track Two", artist="X", album="Album")
        no_lrc_path = tmp_path / "track2.mp3"
        no_lrc_path.write_bytes(b"")
        self._add_file(session, no_lrc, no_lrc_path)

        rows = mh.releases_missing_lyrics_rows(should_stop=lambda: True)

        assert rows == []


class TestScanMissingMetadata:
    def test_aggregates_all_four_categories(self, session, tmp_path):
        # missing cover
        _, no_cover, _ = make_track(session, "Song", artist="A", album="No Cover")
        # missing bio
        no_bio, _, _ = make_track(session, "Song", artist="No Bio")
        # missing popularity (has a release, no ArtistTopTrack)
        no_pop, _, _ = make_track(session, "Song", artist="No Popularity")
        # missing lyrics
        _, lyrics_release, lyrics_track = make_track(session, "Track", artist="C", album="Lyrics Album")
        path = tmp_path / "t.mp3"
        path.write_bytes(b"")
        session.add(MediaFile(track_id=lyrics_track.id, path=str(path)))
        session.flush()

        result = mh.scan_missing_metadata()

        assert no_cover.id in {r["id"] for r in result.missing_covers}
        assert no_bio.id in {r["id"] for r in result.missing_bios}
        assert no_pop.id in {r["id"] for r in result.missing_popularity}
        assert lyrics_release.id in {r["id"] for r in result.missing_lyrics}

    def test_progress_announces_each_phase_then_real_lyrics_progress(self, session, tmp_path):
        _, _, track = make_track(session, "Track", artist="X", album="Album")
        path = tmp_path / "t.mp3"
        path.write_bytes(b"")
        session.add(MediaFile(track_id=track.id, path=str(path)))
        session.flush()

        calls = []
        mh.scan_missing_metadata(progress=lambda done, total, name: calls.append((done, total, name)))

        phase_calls = [c for c in calls if c[1] == 0]
        phase_names = {c[2] for c in phase_calls}
        assert {
            "Checking album artwork…", "Checking artist profiles…",
            "Checking track popularity…", "Checking lyrics…",
        } <= phase_names
        # the real lyrics-scan progress tick(s) come after the phase ticks
        assert calls[-1] == (1, 1, "")

    def test_should_stop_true_from_the_start_returns_an_entirely_empty_result(
        self, session, tmp_path
    ):
        # 2026-09-22 same-day follow-up - James: "add a real cancel button
        # that stops any process running within Settings" - checked before
        # even the first (fast) phase starts, so nothing gets a chance to
        # run at all.
        make_track(session, "Song", artist="A", album="No Cover")

        result = mh.scan_missing_metadata(should_stop=lambda: True)

        assert result.missing_covers == []
        assert result.missing_bios == []
        assert result.missing_popularity == []
        assert result.missing_lyrics == []

    def test_should_stop_after_two_phases_skips_the_rest(self, session, tmp_path):
        _, no_cover, _ = make_track(session, "Song", artist="A", album="No Cover")
        no_bio, _, _ = make_track(session, "Song", artist="No Bio")
        no_pop, _, _ = make_track(session, "Song", artist="No Popularity")

        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            # call 1: before the cover phase - let it through
            # call 2: before the bio phase - let it through
            # call 3: before the popularity phase - stop here
            return calls["n"] > 2

        result = mh.scan_missing_metadata(should_stop=should_stop)

        assert no_cover.id in {r["id"] for r in result.missing_covers}
        assert no_bio.id in {r["id"] for r in result.missing_bios}
        assert result.missing_popularity == []
        assert result.missing_lyrics == []


# --------------------------------------------------------------------------
# 2026-11-08: Missing tags (MusicBee's "missing tags" view) - albums with a
# playable file but no year, no genre, or songs filed as "Unknown Artist"
# --------------------------------------------------------------------------


def _with_file(session, track, path, *, missing=False):
    session.add(MediaFile(track_id=track.id, path=str(path), is_missing=missing))
    session.flush()


def _complete(session, release, year=1965, genre="Rock"):
    release.year = year
    release.genres = [lib.get_or_create_genre(session, genre)]
    session.flush()


class TestReleasesMissingTagsRows:
    def test_a_fully_tagged_album_is_not_listed(self, session, tmp_path):
        _, rel, track = make_track(session, "Song", artist="A", album="Complete")
        _complete(session, rel)
        _with_file(session, track, tmp_path / "a.mp3")

        assert mh.releases_missing_tags_rows() == []

    def test_missing_year_and_missing_genre_are_flagged_separately(self, session, tmp_path):
        _, no_year, t1 = make_track(session, "Song", artist="A", album="No Year")
        no_year.genres = [lib.get_or_create_genre(session, "Jazz")]
        _with_file(session, t1, tmp_path / "1.mp3")
        _, no_genre, t2 = make_track(session, "Song", artist="B", album="No Genre")
        no_genre.year = 1948
        _with_file(session, t2, tmp_path / "2.mp3")

        rows = {r["id"]: r for r in mh.releases_missing_tags_rows()}

        assert rows[no_year.id]["no_year"] and not rows[no_year.id]["no_genre"]
        assert rows[no_genre.id]["no_genre"] and not rows[no_genre.id]["no_year"]

    def test_unknown_artist_songs_are_counted_and_question_mark_is_not(self, session, tmp_path):
        _, rel, t1 = make_track(session, "One", artist="Unknown Artist", album="Mixed")
        _complete(session, rel)
        _with_file(session, t1, tmp_path / "1.mp3")
        _, _, t2 = make_track(session, "Two", artist="Unknown Artist", album="Mixed")
        _with_file(session, t2, tmp_path / "2.mp3")

        _, qm, t3 = make_track(session, "96 Tears", artist="?", album="96 Tears")
        _complete(session, qm)
        _with_file(session, t3, tmp_path / "3.mp3")

        rows = {r["id"]: r for r in mh.releases_missing_tags_rows()}

        assert rows[rel.id]["unknown_artist"] == rows[rel.id]["total"]
        assert qm.id not in rows

    def test_albums_with_no_playable_file_are_skipped(self, session, tmp_path):
        _, rel, track = make_track(session, "Gone", artist="A", album="Ghost")
        _with_file(session, track, tmp_path / "gone.mp3", missing=True)
        _, bare, _ = make_track(session, "No File", artist="B", album="Bare")

        ids = {r["id"] for r in mh.releases_missing_tags_rows()}

        assert rel.id not in ids and bare.id not in ids

    def test_summary_names_each_gap(self):
        row = {"no_year": True, "no_genre": True, "unknown_artist": 3, "total": 12}
        assert mh.missing_tags_summary(row) == "No year · No genre · 3/12 Unknown Artist"
        assert mh.missing_tags_summary({"no_year": False, "no_genre": True,
                                        "unknown_artist": 0, "total": 5}) == "No genre"

    def test_track_ids_for_release_only_includes_songs_with_a_file(self, session, tmp_path):
        _, rel, kept = make_track(session, "Kept", artist="A", album="Half")
        _with_file(session, kept, tmp_path / "kept.mp3")
        _, _, lost = make_track(session, "Lost", artist="A", album="Half")
        _with_file(session, lost, tmp_path / "lost.mp3", missing=True)

        assert mh.track_ids_for_release(rel.id) == [kept.id]

    def test_scan_missing_metadata_includes_tags(self, session, tmp_path):
        _, rel, track = make_track(session, "Song", artist="A", album="Untagged")
        _with_file(session, track, tmp_path / "s.mp3")

        result = mh.scan_missing_metadata()

        assert rel.id in {r["id"] for r in result.missing_tags}
