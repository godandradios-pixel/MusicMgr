"""Tests for services/artist_bio_downloader.py's `should_stop` plumbing.

2026-09-22 - James: "add a real cancel button that stops any process
running within Settings". `download_bios_for_artists` is network-bound
(one Wikipedia lookup per artist), so these tests monkeypatch
`download_bio_for_artist` itself rather than hitting the network - the
same "swap the one per-item function, keep the real bulk loop" shape
tests/test_video_scanner.py's flaky-import test already uses. Nothing
else about this module (the actual Wikipedia lookup/matching logic) is
covered here.
"""

from __future__ import annotations

from musicmgr.db.models import Artist
from musicmgr.services import artist_bio_downloader as bio_dl


def make_artist(session, name: str) -> Artist:
    artist = Artist(name=name, name_key=name.lower())
    session.add(artist)
    session.flush()
    return artist


class TestDownloadBiosForArtistsShouldStop:
    def test_should_stop_true_from_the_start_downloads_nothing(self, session, monkeypatch):
        artist = make_artist(session, "AC/DC")
        monkeypatch.setattr(
            bio_dl, "download_bio_for_artist",
            lambda http, db, a, **k: bio_dl.ArtistBioOutcome(a.id, a.name, "downloaded"),
        )

        result = bio_dl.download_bios_for_artists([artist.id], should_stop=lambda: True,
                                                  http=FakeActionWiki())

        assert result.outcomes == []
        session.refresh(artist)
        assert artist.profile is None

    def test_should_stop_partway_through_keeps_the_earlier_commit(self, session, monkeypatch):
        first = make_artist(session, "AC/DC")
        second = make_artist(session, "Rush")

        def fake_download(http, db, artist, **kwargs):
            artist.profile = f"bio for {artist.name}"
            return bio_dl.ArtistBioOutcome(artist.id, artist.name, "downloaded")

        monkeypatch.setattr(bio_dl, "download_bio_for_artist", fake_download)

        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1  # let the first artist through, stop before the second

        result = bio_dl.download_bios_for_artists(
            [first.id, second.id], should_stop=should_stop, http=FakeActionWiki()
        )

        assert len(result.outcomes) == 1
        session.refresh(first)
        session.refresh(second)
        assert first.profile == "bio for AC/DC"
        assert second.profile is None


# -- 2026-10-13: "(band)" first, and only music articles from a plain name ----
#
# James: "please include (band) to get a better match. For example, the
# profile return on Boston was the city and not the band. Similarly the
# search on the band .38 special returned, the pistol."

import pytest

WIKI = {
    "Boston": {"type": "standard", "description": "Capital city of Massachusetts",
               "extract": "Boston is the capital and most populous city of Massachusetts.",
               "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Boston"}}},
    "Boston_(band)": {"type": "standard", "description": "American rock band",
                      "extract": "Boston is an American rock band from Boston, Massachusetts.",
                      "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Boston_(band)"}}},
    ".38_Special": {"type": "standard", "description": "Rimmed revolver cartridge",
                    "extract": "The .38 Special is a rimmed, centerfire cartridge designed by Smith & Wesson.",
                    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/.38_Special"}}},
    ".38_Special_(band)": {"type": "standard", "description": "American rock band",
                           "extract": ".38 Special is an American rock band formed in 1974.",
                           "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/.38_Special_(band)"}}},
    "Elvis_Presley": {"type": "standard", "description": "American singer and actor (1935–1977)",
                      "extract": "Elvis Aaron Presley was an American singer and actor.",
                      "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Elvis_Presley"}}},
    "Kansas": {"type": "standard", "description": "U.S. state",
               "extract": "Kansas is a landlocked state in the Midwestern United States.",
               "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Kansas"}}},
    "Kansas_City_Rockers": {"type": "standard", "description": "American rock band",
                            "extract": "Kansas City Rockers were an American band.",
                            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/x"}}},
}


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._payload, self.headers = status, payload, {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class FakeWiki:
    def __init__(self, search=()):
        self.asked, self.search = [], list(search)

    def get(self, url, params=None, timeout=None):
        if params and params.get("list") == "search":
            return _Resp(200, {"query": {"search": [{"title": t} for t in self.search]}})
        from urllib.parse import unquote
        title = unquote(url.rsplit("/", 1)[1])
        self.asked.append(title)
        return _Resp(200, WIKI[title]) if title in WIKI else _Resp(404)


def bio_for(name, search=()):
    return bio_dl._find_summary(FakeWiki(search), name)


class TestBandFirst:
    def test_boston_is_the_band_not_the_city(self):
        http = FakeWiki()
        summary = bio_dl._find_summary(http, "Boston")
        assert summary["description"] == "American rock band"
        assert http.asked[0] == "Boston_(band)"

    def test_38_special_is_the_band_not_the_cartridge(self):
        assert bio_for(".38 Special")["description"] == "American rock band"

    def test_a_solo_singer_still_comes_from_the_plain_name(self):
        assert bio_for("Elvis Presley")["description"].startswith("American singer")

    def test_a_plain_name_that_isnt_music_is_passed_over(self):
        # no "Kansas (band)" page here: the state is skipped for a search hit
        assert bio_for("Kansas", search=["Kansas", "Kansas_City_Rockers"])["extract"] \
            == "Kansas City Rockers were an American band."
        assert bio_for("Kansas") is None


class TestWrongBiosAreFixed:
    def test_a_city_bio_is_doubtful_and_gets_replaced(self, session, monkeypatch):
        artist = make_artist(session, "Boston")
        artist.profile = WIKI["Boston"]["extract"]
        artist.urls = "https://www.bostonband.com\nhttps://en.wikipedia.org/wiki/Boston"
        session.flush()
        assert bio_dl.is_doubtful(artist)
        assert artist.id in bio_dl.artists_missing_bio()

        outcome = bio_dl.download_bio_for_artist(FakeWiki(), session, artist)

        assert outcome.status == "downloaded"
        assert artist.profile.startswith("Boston is an American rock band")
        assert artist.urls.splitlines() == ["https://www.bostonband.com",
                                            "https://en.wikipedia.org/wiki/Boston_(band)"]

    def test_a_good_bio_is_left_alone(self, session):
        artist = make_artist(session, "Boston")
        artist.profile = WIKI["Boston_(band)"]["extract"]
        artist.urls = "https://en.wikipedia.org/wiki/Boston_(band)"
        session.flush()
        assert not bio_dl.is_doubtful(artist)
        assert bio_dl.download_bio_for_artist(FakeWiki(), session, artist).status == "already_exists"

    def test_a_hand_written_bio_is_never_doubtful(self, session):
        artist = make_artist(session, "Boston")
        artist.profile = "Saw them at the Civic Arena in 1979."
        session.flush()
        assert not bio_dl.is_doubtful(artist)

    def test_no_better_page_keeps_the_old_text(self, session):
        artist = make_artist(session, "Kansas")
        artist.profile = WIKI["Kansas"]["extract"]
        artist.urls = "https://en.wikipedia.org/wiki/Kansas"
        session.flush()
        outcome = bio_dl.download_bio_for_artist(FakeWiki(), session, artist)
        assert outcome.status == "not_found"
        assert artist.profile == WIKI["Kansas"]["extract"]



# -- 2026-10-13: the bulk run asks about many artists per request -----------
#
# James: "for the artist profiles, can you speed that up, no reason it
# should take that long hitting wikipedia". FakeActionWiki answers the
# MediaWiki action API (formatversion=2) from the same WIKI pages FakeWiki
# serves, so the batched path can be checked against the one-artist path.


def _action_page(title):
    key = title.replace(" ", "_")
    if key not in WIKI:
        return {"title": title, "missing": True}
    w = WIKI[key]
    page = {"title": title, "description": w["description"], "extract": w["extract"] + "\n\nMore.",
            "fullurl": w["content_urls"]["desktop"]["page"]}
    if w["type"] == "disambiguation":
        page["pageprops"] = {"disambiguation": ""}
    return page


class FakeActionWiki(FakeWiki):
    def __init__(self, search=()):
        super().__init__(search)
        self.requests = 0

    def get(self, url, params=None, timeout=None):
        self.requests += 1
        if url != bio_dl.WIKI_SEARCH_URL or (params or {}).get("list") == "search":
            return super().get(url, params, timeout)
        if params.get("generator") == "search":
            pages = [dict(_action_page(t.replace("_", " ")), index=i + 1)
                     for i, t in enumerate(self.search)]
            return _Resp(200, {"query": {"pages": list(reversed(pages))}})
        titles = params["titles"].split("|")
        normalized = [{"from": t, "to": t.replace("_", " ")} for t in titles if "_" in t]
        names = list(dict.fromkeys(t.replace("_", " ") for t in titles))
        return _Resp(200, {"query": {"normalized": normalized,
                                     "pages": [_action_page(t) for t in names]}})


class TestBatchedBulkRun:
    def _run(self, session, names, search=()):
        artists = [make_artist(session, n) for n in names]
        session.commit()
        http = FakeActionWiki(search)
        result = bio_dl.download_bios_for_artists([a.id for a in artists], rate_limit_s=0, http=http)
        for a in artists:
            session.refresh(a)
        return artists, result, http

    def test_picks_the_same_pages_as_the_one_artist_lookup(self, session):
        names = ["Boston", ".38 Special", "Elvis Presley"]
        artists, result, _ = self._run(session, names)
        assert result.downloaded == 3
        for a in artists:
            expected = bio_dl._find_summary(FakeWiki(), a.name)
            assert a.profile == expected["extract"]
            assert a.urls == expected["content_urls"]["desktop"]["page"]

    def test_a_plain_name_that_isnt_music_goes_to_search(self, session):
        (kansas,), result, _ = self._run(session, ["Kansas"], search=["Kansas", "Kansas_City_Rockers"])
        assert kansas.profile == "Kansas City Rockers were an American band."

    def test_unknown_artists_are_not_found(self, session):
        _, result, _ = self._run(session, ["Nobody Ever Heard Of", "Kansas"])
        assert result.not_found == 2 and result.downloaded == 0

    def test_far_fewer_requests(self, session):
        names = [f"Unknown Act {i}" for i in range(16)] + ["Boston", ".38 Special", "Elvis Presley"]
        _, result, http = self._run(session, names)
        assert result.downloaded == 3 and result.not_found == 16
        # one-artist path: 7 requests for each unknown act (5 guesses, the
        # plain name, a search). Batched: 2 per batch of 8 + 1 search each.
        assert http.requests <= 3 * 2 + 16 + 1

    def test_names_that_cant_be_batched_use_the_one_artist_path(self, session):
        (a,), result, http = self._run(session, ["AC|DC"])
        assert result.not_found == 1
        assert any(t.startswith("AC|DC") for t in http.asked)

    def test_a_wikipedia_error_is_not_taken_as_not_found(self, session):
        class Failing(FakeActionWiki):
            def get(self, url, params=None, timeout=None):
                if (params or {}).get("formatversion") == 2:
                    self.requests += 1
                    return _Resp(200, {"error": {"code": "toomanyvalues", "info": "Too many values"}})
                return super().get(url, params, timeout)

        artist = make_artist(session, "Boston")
        session.commit()
        result = bio_dl.download_bios_for_artists([artist.id], rate_limit_s=0, http=Failing())
        session.refresh(artist)
        # fell back to the one-artist lookup, which found the band
        assert result.downloaded == 1
        assert artist.profile.startswith("Boston is an American rock band")
