"""Global rotation, recipient time zones, and scheduling across zones."""

import asyncio
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

from founder_scout import geo
from founder_scout.outreach import Outreacher, Result, tz_for
from founder_scout.scout import Founder, Store

REGION_OF = {place: region for region, places in geo.REGIONS.items() for place in places}


def test_every_place_has_a_valid_zone():
    for tz in set(geo.CITY_TZ.values()) | {tz for tz, _ in geo.COUNTRIES.values()}:
        ZoneInfo(tz)


def test_consecutive_runs_cycle_through_every_region():
    first = [REGION_OF[place] for _, place in geo.ROTATION[: 6 * 20]]
    for k in range(0, len(first), 6):
        assert set(first[k:k + 6]) == set(geo.REGIONS)
    assert set(Counter(REGION_OF[p] for _, p in geo.ROTATION).values()) == {len(geo.ROTATION) // 6}


def test_rotation_covers_every_pair():
    assert set(geo.ROTATION) == {(s, p) for s in geo.SECTORS for places in geo.REGIONS.values() for p in places}


def test_zone_from_location_city_then_country():
    assert geo.tz_for("Lagos, Nigeria") == "Africa/Lagos"
    assert geo.tz_for("Bengaluru, Karnataka") == "Asia/Kolkata"
    assert geo.tz_for("Remote · Indonesia") == "Asia/Jakarta"
    assert geo.tz_for("Austin, TX, United States") == "America/Chicago"
    assert geo.tz_for("United States") == "America/Denver"


def test_country_names_match_whole_words_only():
    assert geo.tz_for("Indianapolis, Indiana") is None
    assert geo.tz_for("Bucharest, Romania") == "Europe/Bucharest"
    assert geo.tz_for("Timișoara, Romania") == "Europe/Bucharest"


def test_fallbacks_country_code_domain_then_searched_place():
    assert geo.tz_for("", "ke") == "Africa/Nairobi"
    assert geo.tz_for("", "", ("acme.com.br",)) == "America/Sao_Paulo"
    assert geo.tz_for("", "", ("acme.co.uk",)) == "Europe/London"
    assert geo.tz_for("", "", ("acme.co",), "Seoul") == "Asia/Seoul"
    assert geo.tz_for("Global") is None


def test_unknown_founder_defaults_to_berlin():
    assert tz_for(Founder(name="A B", email="a@b.com")) == "Europe/Berlin"
    assert tz_for(Founder(name="A B", email="a@b.com", search_region="Tokyo")) == "Asia/Tokyo"


class ZoneSchedulingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        store = Store(str(Path(self.tmp.name) / "db.sqlite"))
        for i, loc in enumerate(["Berlin", "Berlin", "Tokyo", "Lagos"]):
            store.add(Founder(name=f"Person {i} Example", company=f"Co{i}", location=loc,
                              email=f"p{i}@co{i}.test", email_source=f"https://co{i}.test/team"))
        self.out = Outreacher(store, scout=None, log=lambda _: None)
        self.drafted: list[str] = []

        async def process(f, send=False, **_):
            tz = tz_for(f)
            if tz == "Europe/Berlin":  # Berlin mornings are fully booked
                return Result(f, "remote", "deferred", "no free morning slot", tz=tz)
            self.drafted.append(f.location)
            return Result(f, "remote", "queued", tz=tz)

        self.out.process = process  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_full_zone_does_not_block_other_zones(self) -> None:
        results = asyncio.run(self.out.run())
        self.assertEqual(sorted(self.drafted), ["Lagos", "Tokyo"])
        self.assertEqual(len(results), 2)
