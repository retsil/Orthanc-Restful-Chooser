# Copyright (C) 2026 retsil <https://github.com/retsil/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Listing the series of a study, and not listing one twice."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from OrthancRC import series  # noqa: E402


def _entry(uid, number=None, modality="CT", description="head",
           instances=2, seriesUID=None):
    tags = {"SeriesInstanceUID": seriesUID or f"1.2.{uid}",
            "Modality": modality, "SeriesDescription": description}
    if number is not None:
        tags["SeriesNumber"] = number
    return {"ID": uid, "MainDicomTags": tags,
            "Instances": [f"{uid}-i{i}" for i in range(instances)]}


def _client(listings):
    client = mock.Mock()
    client.get_studies_id_series.side_effect = (
        lambda uuid, params=None: list(listings[uuid]))
    return client


class FetchSeriesTest(unittest.TestCase):
    def test_series_come_back_in_series_number_order(self):
        client = _client({"st1": [_entry("sB", "10"), _entry("sA", "2")]})

        records = series.fetch_series(client, "st1")

        # Numbers are compared as numbers: "10" after "2", not before it.
        self.assertEqual([r.uid for r in records], ["sA", "sB"])

    def test_an_unnumbered_series_sorts_after_the_numbered_ones(self):
        client = _client({"st1": [_entry("sX"), _entry("sA", "1")]})
        self.assertEqual([r.uid for r in series.fetch_series(client, "st1")],
                         ["sA", "sX"])

    def test_the_uid_breaks_a_tie_so_the_order_is_stable(self):
        client = _client({"st1": [
            _entry("sB", "1", seriesUID="1.2.9"),
            _entry("sA", "1", seriesUID="1.2.1"),
        ]})
        self.assertEqual([r.uid for r in series.fetch_series(client, "st1")],
                         ["sA", "sB"])

    def test_a_record_carries_what_a_picker_shows(self):
        client = _client({"st1": [
            _entry("sA", "3", modality="MR", description="brain", instances=5),
        ]})

        record = series.fetch_series(client, "st1")[0]

        self.assertEqual(record.uid, "sA")
        self.assertEqual(record.study_uid, "st1")
        self.assertEqual(record.series_instance_uid, "1.2.sA")
        self.assertEqual(record.series_number, "3")
        self.assertEqual(record.modality, "MR")
        self.assertEqual(record.description, "brain")
        # Already in the listing, so the count costs no request of its own.
        self.assertEqual(record.instance_count, 5)

    def test_one_study_is_listed_in_one_request(self):
        client = _client({"st1": [_entry("sA", "1")]})
        series.fetch_series(client, "st1")

        self.assertEqual(client.get_studies_id_series.call_count, 1)
        # Expanded server side rather than fetched series by series.
        _args, kwargs = client.get_studies_id_series.call_args
        self.assertEqual(kwargs["params"], {"expand": ""})
        client.get_series_id.assert_not_called()

    def test_a_listing_of_bare_uuids_is_fetched_one_by_one(self):
        client = _client({"st1": ["sA"]})
        client.get_series_id.side_effect = lambda uuid: _entry(uuid, "1")

        records = series.fetch_series(client, "st1")

        self.assertEqual([r.uid for r in records], ["sA"])
        client.get_series_id.assert_called_once_with("sA")

    def test_a_series_with_no_main_tags_at_all_is_still_a_row(self):
        client = _client({"st1": [{"ID": "sA"}]})
        record = series.fetch_series(client, "st1")[0]

        self.assertEqual(record.series_number, "")
        self.assertEqual(record.instance_count, 0)


class SeriesCacheTest(unittest.TestCase):
    """A request per study is a great many for a large selection."""

    def _cache(self):
        client = _client({"st1": [_entry("sA", "1")],
                          "st2": [_entry("sB", "1")]})
        return series.SeriesCache(client), client

    def test_a_study_is_listed_once_however_often_it_is_asked_for(self):
        cache, client = self._cache()

        cache.get("st1")
        cache.get("st1")

        self.assertEqual(client.get_studies_id_series.call_count, 1)

    def test_load_keeps_the_order_the_studies_were_given_in(self):
        cache, _client = self._cache()
        loaded = cache.load(["st2", "st1"])
        self.assertEqual(list(loaded), ["st2", "st1"])

    def test_progress_is_reported_only_for_studies_not_already_held(self):
        cache, _client = self._cache()
        cache.get("st1")

        seen = []
        cache.load(["st1", "st2"], lambda done, total: seen.append((done, total)))

        self.assertEqual(seen, [(1, 2)])

    def test_a_run_entirely_from_the_cache_reports_no_progress(self):
        cache, _client = self._cache()
        cache.load(["st1", "st2"])

        seen = []
        cache.load(["st1", "st2"], lambda done, total: seen.append(done))
        self.assertEqual(seen, [])

    def test_known_answers_only_from_what_is_held(self):
        cache, client = self._cache()

        self.assertIsNone(cache.known("st1"))
        cache.get("st1")
        self.assertEqual([r.uid for r in cache.known("st1")], ["sA"])
        # A caller drawing one row per study must not pay a request per row.
        self.assertEqual(client.get_studies_id_series.call_count, 1)

    def test_what_a_caller_is_handed_cannot_disturb_the_cache(self):
        cache, _client = self._cache()
        cache.get("st1").clear()
        self.assertEqual(len(cache.get("st1")), 1)


if __name__ == "__main__":
    unittest.main()
