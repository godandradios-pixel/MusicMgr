"""Tests for services/musicbrainz.py (2026-10-06, the tag editor's
"Look up on MusicBrainz"). No network: a fake HTTP object hands back
responses shaped like MusicBrainz's JSON web service (ws/2) - the same
keys a real search and release lookup return."""

from __future__ import annotations

import pytest

from musicmgr.services import musicbrainz as mb

SEARCH = {
    "releases": [
        {
            "id": "rel-remaster", "score": 100, "title": "Back in Black", "status": "Official",
            "date": "2003-02-18", "country": "XE", "track-count": 10,
            "artist-credit": [{"name": "AC/DC", "artist": {"id": "acdc", "name": "AC/DC"}}],
            "release-group": {"id": "rg1", "primary-type": "Album"},
            "label-info": [{"catalog-number": "5107612", "label": {"name": "Epic"}}],
            "media": [{"format": "CD", "track-count": 10}],
        },
        {
            "id": "rel-1980", "score": 100, "title": "Back in Black", "status": "Official",
            "date": "1980-07-25", "country": "US", "track-count": 10, "barcode": "",
            "artist-credit": [{"name": "AC/DC", "artist": {"id": "acdc", "name": "AC/DC"}}],
            "release-group": {"id": "rg1", "primary-type": "Album"},
            "label-info": [{"catalog-number": "SD 16018", "label": {"name": "Atlantic"}}],
            "media": [{"format": "12\" Vinyl", "track-count": 10}],
        },
        {
            "id": "rel-bootleg", "score": 100, "title": "Back in Black", "status": "Bootleg",
            "date": "1980", "country": "", "track-count": 12,
            "artist-credit": [{"name": "AC/DC", "artist": {"id": "acdc", "name": "AC/DC"}}],
            "media": [{"format": "CD", "track-count": 12}],
        },
        {
            "id": "rel-weak", "score": 60, "title": "Back in Black Live", "status": "Official",
            "date": "1990", "track-count": 10,
            "artist-credit": [{"name": "AC/DC", "artist": {"id": "acdc"}}],
            "media": [{"format": "CD", "track-count": 10}],
        },
    ]
}

TITLES = ["Hells Bells", "Shoot to Thrill", "What Do You Do for Money Honey",
          "Givin the Dog a Bone", "Let Me Put My Love Into You", "Back in Black",
          "You Shook Me All Night Long", "Have a Drink on Me", "Shake a Leg",
          "Rock and Roll Ain't Noise Pollution"]

RELEASE = {
    "id": "rel-1980", "title": "Back in Black", "status": "Official", "date": "1980-07-25",
    "country": "US", "barcode": "075678155820",
    "artist-credit": [{"name": "AC/DC", "joinphrase": "", "artist": {"id": "acdc", "name": "AC/DC"}}],
    "release-group": {"id": "rg1", "primary-type": "Album"},
    "label-info": [{"catalog-number": "SD 16018", "label": {"name": "Atlantic"}}],
    "genres": [{"name": "hard rock", "count": 9}, {"name": "rock", "count": 4}],
    "cover-art-archive": {"front": True},
    "media": [{
        "position": 1, "format": "12\" Vinyl", "track-count": 10,
        "tracks": [
            {"id": f"t{i}", "position": i, "number": ("A" if i <= 5 else "B") + str((i - 1) % 5 + 1),
             "title": title, "length": 200000 + i * 1000,
             "recording": {"id": f"r{i}", "title": title, "length": 200000 + i * 1000,
                           "isrcs": [f"AUAP0800{i:04d}"] if i == 1 else []}}
            for i, title in enumerate(TITLES, start=1)
        ],
    }],
}

VA_RELEASE = {
    "id": "va", "title": "1949 Hits", "date": "1949",
    "artist-credit": [{"name": "Various Artists", "artist": {"id": mb.VARIOUS_ARTISTS_ID}}],
    "media": [
        {"position": 1, "track-count": 1, "tracks": [
            {"id": "x1", "position": 1, "number": "1", "title": "Again",
             "artist-credit": [{"name": "Doris Day", "joinphrase": " with ", "artist": {"id": "dd"}},
                               {"name": "The Mellomen", "artist": {"id": "mm"}}],
             "recording": {"id": "rx1"}}]},
        {"position": 2, "track-count": 1, "tracks": [
            {"id": "x2", "position": 1, "number": "1", "title": "Mule Train",
             "artist-credit": [{"name": "Frankie Laine", "artist": {"id": "fl"}}],
             "recording": {"id": "rx2"}}]},
    ],
}


class FakeResponse:
    def __init__(self, status, payload=None, content=b""):
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        for key, resp in self.routes:
            if key in url and (not isinstance(resp, list) or resp):
                return resp.pop(0) if isinstance(resp, list) else resp
        return FakeResponse(404)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(mb, "MIN_INTERVAL_S", 0)
    monkeypatch.setattr(mb.time, "sleep", lambda s: None)


class TestSearch:
    def test_best_first_right_track_count_official(self):
        http = FakeHttp([("/release/", FakeResponse(200, SEARCH))])
        results = mb.search_releases("AC/DC", "Back in Black", track_count=10, http=http)
        assert [r.id for r in results] == ["rel-1980", "rel-remaster", "rel-bootleg", "rel-weak"]
        r = results[0]
        assert r.describe() == '1980 · US · 12" Vinyl · Atlantic SD 16018 · 10 tracks'

    def test_query_and_user_agent(self):
        http = FakeHttp([("/release/", FakeResponse(200, SEARCH))])
        mb.search_releases("AC/DC", "Back in Black", http=http)
        url, params, headers = http.calls[0]
        assert params["query"] == 'release:"Back in Black" AND artist:"AC\\/DC"'
        assert headers["User-Agent"].startswith("MusicMgr/")
        assert "github.com/godandradios-pixel/MusicMgr" in headers["User-Agent"]

    def test_allows_misspellings_then_the_title_alone(self):
        http = FakeHttp([("/release/", [FakeResponse(200, {"releases": []}),
                                        FakeResponse(200, {"releases": []}),
                                        FakeResponse(200, SEARCH)])])
        results = mb.search_releases("ACDC", "Back in Blak", http=http)
        assert len(results) == 4
        queries = [c[1]["query"] for c in http.calls]
        assert queries == ['release:"Back in Blak" AND artist:"ACDC"',
                           "release:(back~ in blak~) AND artist:(acdc~)",
                           "release:(back~ in blak~)"]

    def test_various_artists_isnt_searched_as_an_artist(self):
        http = FakeHttp([("/release/", FakeResponse(200, {"releases": []}))])
        mb.search_releases("Various Artists", "1949 Hits", http=http)
        assert http.calls[0][1]["query"] == 'release:"1949 Hits"'
        assert http.calls[1][1]["query"] == "release:(1949 hits~)"

    def test_busy_then_ok(self):
        http = FakeHttp([("/release/", [FakeResponse(503), FakeResponse(200, SEARCH)])])
        assert mb.search_releases("AC/DC", "Back in Black", http=http)

    def test_unreachable(self):
        class Down:
            def get(self, *a, **k):
                raise mb.requests.ConnectionError("no route")
        with pytest.raises(mb.MusicBrainzError, match="internet"):
            mb.search_releases("AC/DC", "Back in Black", http=Down())


class TestRelease:
    def test_tracks_and_details(self):
        http = FakeHttp([("/release/rel-1980", FakeResponse(200, RELEASE))])
        rel = mb.get_release("rel-1980", http=http)
        assert rel.title == "Back in Black" and rel.artist == "AC/DC"
        assert rel.label == "Atlantic" and rel.catalog_number == "SD 16018"
        assert rel.genres == ["Hard Rock", "Rock"]
        assert rel.has_front_cover
        assert len(rel.tracks) == 10
        assert rel.tracks[0].number == "A1" and rel.tracks[5].number == "B1"
        assert rel.tracks[0].isrc == "AUAP08000001"
        assert rel.disc_sizes == {1: 10}
        assert "recordings" in http.calls[0][1]["inc"]

    def test_compilation_with_joined_credits(self):
        http = FakeHttp([("/release/va", FakeResponse(200, VA_RELEASE))])
        rel = mb.get_release("va", http=http)
        assert rel.is_compilation
        assert rel.tracks[0].artist == "Doris Day with The Mellomen"
        assert rel.disc_count == 2
        v = mb.values_for(rel, rel.tracks[1])
        assert v.album_artist == "Various Artists"
        assert v.compilation == "1"
        assert (v.disc, v.disc_total, v.track, v.track_total) == ("2", "2", "1", "1")

    def test_values_for_a_track(self):
        http = FakeHttp([("/release/rel-1980", FakeResponse(200, RELEASE))])
        rel = mb.get_release("rel-1980", http=http)
        v = mb.values_for(rel, rel.tracks[6])
        assert v.title == "You Shook Me All Night Long"
        assert (v.track, v.track_total, v.disc) == ("7", "10", None)
        assert v.date == "1980-07-25" and v.label == "Atlantic"
        assert v.genre is None  # left alone unless asked for
        assert v.mb_album_id == "rel-1980" and v.mb_recording_id == "r7"
        assert mb.values_for(rel, rel.tracks[6], include_genre=True).genre == "Hard Rock, Rock"
        assert mb.values_for(rel, rel.tracks[6], keep_side=True).track == "B2"

    def test_cover(self):
        http = FakeHttp([("coverartarchive.org/release/rel-1980/front-500",
                          FakeResponse(200, content=b"\xff\xd8jpeg"))])
        assert mb.fetch_front_cover("rel-1980", http=http) == b"\xff\xd8jpeg"
        assert mb.fetch_front_cover("none", http=FakeHttp([])) is None


class TestMatchTracks:
    def _release(self):
        return mb.get_release("rel-1980", http=FakeHttp([("/release/", FakeResponse(200, RELEASE))]))

    def test_matches_by_title_even_out_of_order(self):
        rel = self._release()
        local = [mb.LocalTrack("Back In Black", 1, 1), mb.LocalTrack("hells bells", 1, 6),
                 mb.LocalTrack("Givin' the Dog a Bone", 1, 4)]
        assert mb.match_tracks(local, rel) == [5, 0, 3]

    def test_misspelt_titles_line_up_by_position_and_length(self):
        rel = self._release()
        local = [mb.LocalTrack("Track 01", 1, 1, 201000), mb.LocalTrack("Track 02", 1, 2, 202500)]
        assert mb.match_tracks(local, rel) == [0, 1]

    def test_a_bonus_track_matches_nothing(self):
        rel = self._release()
        local = [mb.LocalTrack("Hells Bells", 1, 1), mb.LocalTrack("Live Encore Jam", 1, 11)]
        assert mb.match_tracks(local, rel) == [0, None]

    def test_each_release_track_used_once(self):
        rel = self._release()
        local = [mb.LocalTrack("Hells Bells", 1, 1), mb.LocalTrack("Hells Bells", 1, 1)]
        out = mb.match_tracks(local, rel)
        assert out[0] == 0 and out[1] != 0

    def test_a_loose_likeness_alone_is_not_a_match(self):
        rel = self._release()
        local = [mb.LocalTrack("Shake the Leg Again", 1, 3)]  # like "Shake a Leg" (#9)
        assert mb.match_tracks(local, rel) == [None]


class TestOddInput:
    def test_punctuation_only_title_skips_the_fuzzy_search(self):
        http = FakeHttp([("/release/", FakeResponse(200, {"releases": []}))])
        assert mb.search_releases("", "?!", http=http) == []
        assert all("()" not in c[1]["query"] for c in http.calls)

