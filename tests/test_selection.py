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

"""The selection file: what it may say, and what it must refuse to say."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from OrthancRC import selection  # noqa: E402

CRITERIA = {"patient_id": "PID001", "description": None}


def _json(**keys) -> str:
    return json.dumps({"criteria": CRITERIA, **keys})


class StudyLevelFileTest(unittest.TestCase):
    """A file with neither new key is what every file written so far is."""

    def test_a_file_from_before_series_existed_still_loads(self):
        loaded = selection.load_selection_json(_json(study_uids=["b", "a"]))

        self.assertEqual(loaded.criteria, CRITERIA)
        self.assertEqual(loaded.study_uids, {"a", "b"})
        self.assertEqual(loaded.series_uids, {})
        # Nothing to guess at: no series means study level, as it always did.
        self.assertEqual(loaded.selectionLevel, selection.STUDY_LEVEL)

    def test_an_unmentioned_study_means_the_whole_study(self):
        loaded = selection.load_selection_json(_json(study_uids=["a"]))
        self.assertIsNone(loaded.seriesFor("a"))

    def test_a_study_level_save_writes_the_shape_it_always_wrote(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "s.json")
            selection.save_selection(path, CRITERIA, {"b", "a"})
            data = json.loads(Path(path).read_text())

        self.assertEqual(data, {
            "criteria": CRITERIA,
            "selection_level": "study",
            # Sorted, so a file is stable across runs and diffable.
            "study_uids": ["a", "b"],
        })
        # Not written empty, so nothing has to read a key that says nothing.
        self.assertNotIn("series_uids", data)


class SeriesLevelFileTest(unittest.TestCase):
    def test_series_are_carried_per_study_and_sorted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "s.json")
            selection.save_selection(
                path, CRITERIA, {"a", "b"}, {"a": {"s2", "s1"}})
            data = json.loads(Path(path).read_text())

        self.assertEqual(data["selection_level"], "series")
        self.assertEqual(data["series_uids"], {"a": ["s1", "s2"]})

    def test_a_round_trip_says_exactly_what_was_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "s.json")
            selection.save_selection(
                path, CRITERIA, {"a", "b"}, {"a": {"s1"}})
            loaded = selection.load_selection(path)

        self.assertEqual(loaded.study_uids, {"a", "b"})
        self.assertEqual(loaded.seriesFor("a"), {"s1"})
        # b was never narrowed, so it is still the whole study.
        self.assertIsNone(loaded.seriesFor("b"))

    def test_a_selection_can_be_saved_back_as_it_was_loaded(self):
        loaded = selection.load_selection_json(
            _json(selection_level="series", study_uids=["a"],
                  series_uids={"a": ["s1"]}))
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "s.json")
            selection.save_selection(path, loaded)
            again = selection.load_selection(path)

        self.assertEqual(again, loaded)

    def test_a_full_series_list_is_kept_rather_than_dropped(self):
        """Absent and complete-list are different statements.

        Absent means whatever the study holds when the file is read; a full
        list means these series, as the study stood when it was written. Only
        the second records what was actually seen.
        """
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "s.json")
            selection.save_selection(path, CRITERIA, {"a"}, {"a": {"s1", "s2"}})
            loaded = selection.load_selection(path)

        self.assertEqual(loaded.seriesFor("a"), {"s1", "s2"})


class StrictnessTest(unittest.TestCase):
    """A file that does not mean what it says is an error, never a guess."""

    def _rejects(self, text: str, because: str) -> None:
        with self.assertRaises(ValueError) as caught:
            selection.load_selection_json(text)
        self.assertIn(because, str(caught.exception))

    def test_an_unknown_selection_level_is_not_a_fall_back_to_study(self):
        self._rejects(
            _json(selection_level="instance", study_uids=["a"]),
            "unknown 'selection_level'")

    def test_series_level_with_no_series_names_nothing_it_claims_to(self):
        self._rejects(
            _json(selection_level="series", study_uids=["a"]),
            "names no series")

    def test_study_level_alongside_series_is_a_file_contradicting_itself(self):
        self._rejects(
            _json(selection_level="study", study_uids=["a"],
                  series_uids={"a": ["s1"]}),
            "the two disagree")

    def test_an_empty_series_list_is_not_the_same_as_no_list(self):
        self._rejects(
            _json(study_uids=["a"], series_uids={"a": []}),
            "no series at all for study")

    def test_series_for_an_unselected_study_is_a_bug_in_the_writer(self):
        self._rejects(
            _json(study_uids=["a"], series_uids={"b": ["s1"]}),
            "studies that are not selected")

    def test_a_present_but_empty_series_map_asserts_nothing_and_is_allowed(self):
        # It contradicts no level and names no series, so it reads as absent.
        loaded = selection.load_selection_json(
            _json(selection_level="study", study_uids=["a"], series_uids={}))
        self.assertEqual(loaded.selectionLevel, "study")
        self.assertIsNone(loaded.seriesFor("a"))

    def test_malformed_json_is_a_value_error_like_every_other_refusal(self):
        # json.JSONDecodeError already is one, so one except covers the lot.
        with self.assertRaises(ValueError):
            selection.load_selection_json("{not json")

    def test_uid_lists_must_be_lists_of_strings(self):
        self._rejects(_json(study_uids="a"), "'study_uids' is not a list")
        self._rejects(_json(study_uids=[1]), "'study_uids' is not a list")
        self._rejects(_json(study_uids=["a"], series_uids={"a": [1]}),
                      "is not a list of strings")

    def test_a_payload_that_is_not_an_object_is_not_a_selection(self):
        self._rejects("[]", "not a JSON object")


class RestoreChecksTest(unittest.TestCase):
    """The three refusals a restore makes, all shared rather than per front end."""

    def _selection(self, **keys) -> selection.Selection:
        return selection.load_selection_json(_json(study_uids=["a"], **keys))

    def test_criteria_must_match_the_ones_in_force_now(self):
        saved = self._selection()
        selection.check_criteria(saved, CRITERIA)  # the same search: fine

        with self.assertRaises(ValueError) as caught:
            selection.check_criteria(saved, {"patient_id": "OTHER"})
        self.assertIn("do not match", str(caught.exception))

    def test_a_restored_study_that_no_longer_matches_is_refused(self):
        saved = self._selection()
        selection.check_studies(saved, {"a", "z"})

        with self.assertRaises(ValueError) as caught:
            selection.check_studies(saved, {"z"})
        self.assertIn("no longer matching", str(caught.exception))
        self.assertIn("'a'", str(caught.exception))

    def test_a_restored_series_its_study_no_longer_holds_is_refused(self):
        saved = self._selection(series_uids={"a": ["s1", "s2"]})
        selection.check_series(saved, {"a": {"s1", "s2", "s3"}})

        with self.assertRaises(ValueError) as caught:
            selection.check_series(saved, {"a": {"s1"}})
        message = str(caught.exception)
        # The study is named as well as the series, so the sender of the file
        # can tell which end went stale.
        self.assertIn("study a", message)
        self.assertIn("'s2'", message)

    def test_a_study_with_no_series_of_its_own_checks_nothing(self):
        # Whole-study entries have nothing to have gone missing.
        selection.check_series(self._selection(), {})


class SelectionOfTest(unittest.TestCase):
    def test_the_level_follows_whether_any_series_were_picked(self):
        self.assertEqual(
            selection.Selection.of(CRITERIA, {"a"}).selectionLevel, "study")
        self.assertEqual(
            selection.Selection.of(CRITERIA, {"a"}, {"a": {"s1"}}).selectionLevel,
            "series")

    def test_an_empty_series_mapping_is_a_study_level_selection(self):
        made = selection.Selection.of(CRITERIA, {"a"}, {})
        self.assertEqual(made.selectionLevel, "study")
        self.assertEqual(made.series_uids, {})


if __name__ == "__main__":
    unittest.main()
