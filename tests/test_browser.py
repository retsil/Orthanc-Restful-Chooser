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

import contextlib
import curses
import fnmatch
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydicom.dataset import Dataset

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from OrthancRC.base import Application  # noqa: E402
from OrthancRC import studies  # noqa: E402
from OrthancRC.orthanc_util import seriesUUID, studyUUID  # noqa: E402
from OrthancRC.selection import SEARCH_FIELDS  # noqa: E402
from OrthancRC.orthanc.staging import StagedSeries  # noqa: E402
from OrthancRC.curses import browser  # noqa: E402


class RecordingApplication(Application):
    """A do-nothing Application that keeps the host it was handed."""

    def __init__(self, host) -> None:
        self.host = host

    def getOutputData(self, instanceUUID: str) -> Dataset:
        return Dataset()

    def notifyInputAvailable(self, instanceUUID, mainTags, lastData) -> bool:
        return True

    def bringApplicationToFront(self) -> bool:
        return False


class FakeHost:
    """Just enough Host for run_application; records the wiring."""

    def __init__(self, sendResult: bool = True) -> None:
        self.app = None
        self.sendResult = sendResult
        self.sent = False
        # What main() reads to decide whether the run reported an error.
        self.messages: list = []

    def setApplication(self, app) -> None:
        self.app = app

    def sendInputs(self) -> bool:
        self.sent = True
        return self.sendResult


class TestRunApplication(unittest.TestCase):
    def test_wires_application_to_host_and_sends(self):
        host = FakeHost()
        code = browser.run_application(host, RecordingApplication)

        self.assertEqual(code, 0)
        self.assertIsInstance(host.app, RecordingApplication)
        # The Application must receive the host it was registered with.
        self.assertIs(host.app.host, host)
        self.assertTrue(host.sent)

    def test_refused_input_is_a_nonzero_exit(self):
        host = FakeHost(sendResult=False)
        self.assertEqual(browser.run_application(host, RecordingApplication), 1)


def _record(uid: str = "u1", **overrides) -> browser.StudyRecord:
    fields = dict(
        uid=uid,
        study_instance_uid=f"1.2.{uid}",
        patient_id="PID",
        patient_surname="Doe",
        patient_given_name="Jane",
        patient_name_display="Doe Jane",
        dob="19700101",
        study_date="20260101",
        accession="ACC",
        description="desc",
    )
    fields.update(overrides)
    return browser.StudyRecord(**fields)


class HostFromBrowserTest(unittest.TestCase):
    """The picker and the Host built over what it returns, as one call."""

    def _pick(self, selected, records=None, criteria=None, series=None):
        records = records if records is not None else [_record("s1"), _record("s2")]
        picked = None if selected is None else (selected, series or {})
        with (
            mock.patch.object(browser, "fetch_all_studies",
                              return_value=records) as fetch,
            mock.patch.object(browser, "OrthancHost") as hostClass,
            mock.patch.object(browser.curses, "wrapper",
                              return_value=picked) as wrapper,
        ):
            host = browser.host_from_browser("client", criteria, uploadOutputs=False)
        return host, hostClass, fetch, wrapper

    def test_the_host_is_built_over_the_checked_studies_in_row_order(self):
        host, hostClass, fetch, _wrapper = self._pick({"s2", "s1"})

        self.assertIs(host, hostClass.return_value)
        args, kwargs = hostClass.call_args
        self.assertEqual(args, ("client", ["s1", "s2"]))
        # Nothing narrowed, so every selected study is served whole.
        self.assertEqual(kwargs["seriesUUIDs"], {})
        # Anything else given goes on to the Host.
        self.assertFalse(kwargs["uploadOutputs"])
        # Without criteria every study on the server is offered.
        self.assertIsNone(fetch.call_args[0][1])

    def test_a_sub_selection_reaches_the_host(self):
        _host, hostClass, _fetch, _wrapper = self._pick(
            {"s1", "s2"}, series={"s1": {"se1", "se2"}},
        )

        _args, kwargs = hostClass.call_args
        self.assertEqual(kwargs["seriesUUIDs"], {"s1": {"se1", "se2"}})

    def test_a_cancelled_picker_builds_no_host(self):
        host, hostClass, _fetch, _wrapper = self._pick(None)

        self.assertIsNone(host)
        hostClass.assert_not_called()

    def test_criteria_reach_the_server_and_narrow_the_rows_again(self):
        records = [_record("s1", patient_id="PID001"),
                   _record("s2", patient_id="OTHER")]
        _host, _hostClass, fetch, wrapper = self._pick(
            {"s1"}, records=records, criteria={"patient_id": "PID001"},
        )

        self.assertEqual(fetch.call_args[0][1], {"patient_id": "PID001"})
        # The find is only a widening, so the rows are filtered locally too.
        offered = wrapper.call_args[0][1]
        self.assertEqual([r.uid for r in offered], ["s1"])


class TestMainWithModule(unittest.TestCase):
    """main() with --module, against a fake Orthanc and a stubbed picker."""

    def _run(self, argv, selected, series=None):
        records = [_record("s1"), _record("s2")]
        picked = None if selected is None else (selected, series or {})
        with (
            mock.patch.object(browser, "Orthanc") as orthanc,
            mock.patch.object(browser, "loadApplicationClass",
                              return_value=RecordingApplication),
            mock.patch.object(browser, "fetch_all_studies", return_value=records),
            mock.patch.object(browser.curses, "wrapper", return_value=picked),
            mock.patch.object(browser, "OrthancHost") as hostClass,
        ):
            host = FakeHost()
            hostClass.return_value = host
            code = browser.main(argv)
        return code, orthanc, hostClass, host

    def test_selection_is_handed_to_the_application(self):
        code, _orthanc, hostClass, host = self._run(
            ["--module", "fake_app"], selected={"s1"},
        )

        self.assertEqual(code, 0)
        # The host is built over the checked studies only, in browser row order.
        args, kwargs = hostClass.call_args
        self.assertEqual(args[1], ["s1"])
        # Uploading results back into Orthanc is the default.
        self.assertTrue(kwargs["uploadOutputs"])
        self.assertIsNone(kwargs["outputDir"])
        self.assertTrue(host.sent)

    def test_row_order_is_kept_not_set_order(self):
        _code, _orthanc, hostClass, _host = self._run(
            ["--module", "fake_app"], selected={"s2", "s1"},
        )

        args, _kwargs = hostClass.call_args
        self.assertEqual(args[1], ["s1", "s2"])

    def test_output_dir_and_no_upload_reach_the_host(self):
        _code, _orthanc, hostClass, _host = self._run(
            ["--module", "fake_app", "--outputdir", "/tmp/out", "--no-upload"],
            selected={"s1", "s2"},
        )

        _args, kwargs = hostClass.call_args
        self.assertEqual(kwargs["outputDir"], Path("/tmp/out"))
        self.assertFalse(kwargs["uploadOutputs"])

    def test_cancelled_selection_never_starts_the_application(self):
        code, _orthanc, hostClass, _host = self._run(
            ["--module", "fake_app"], selected=None,
        )

        self.assertEqual(code, 1)
        hostClass.assert_not_called()

    def test_without_module_no_host_is_built(self):
        code, _orthanc, hostClass, _host = self._run([], selected={"s1"})

        self.assertEqual(code, 0)
        hostClass.assert_not_called()


class TestModuleIsLoadedBeforeSelecting(unittest.TestCase):
    def test_bad_module_fails_before_touching_orthanc(self):
        with (
            mock.patch.object(browser, "Orthanc") as orthanc,
            mock.patch.object(browser, "fetch_all_studies") as fetch,
        ):
            with self.assertRaises(SystemExit):
                browser.main(["--module", "no_such_module_xyz"])

        # A bad --module must not cost the user a search and a selection.
        orthanc.assert_not_called()
        fetch.assert_not_called()


ENTER = 10
SPACE = ord(" ")
ESC = 27


class FakeScreen:
    """A curses window that plays back keys and remembers what was drawn."""

    def __init__(self, keys, height: int = 24, width: int = 120) -> None:
        self.keys = list(keys)
        self.height = height
        self.width = width
        self.lines: dict = {}
        self.frames: list = []

    # -- what the pickers draw on --
    def erase(self) -> None:
        self.lines = {}

    def getmaxyx(self) -> tuple:
        return (self.height, self.width)

    def keypad(self, _flag) -> None:
        pass

    def addnstr(self, y, _x, text, n, _attr=0) -> None:
        self.lines[y] = text[:n]

    def refresh(self) -> None:
        self.frames.append(dict(self.lines))

    def getch(self) -> int:
        if not self.keys:
            raise AssertionError("the picker asked for a key and there is none "
                                 "left: it did not return when it should have")
        return self.keys.pop(0)

    # -- what a test asks about --
    @property
    def drawn(self) -> list:
        """Every line of the last frame drawn, top to bottom."""
        last = self.frames[-1]
        return [last[y] for y in sorted(last)]

    def rows(self) -> list:
        """The list rows of the last frame: everything below the heading rule."""
        return [line for line in self.drawn[2:-1]]

    @property
    def footer(self) -> str:
        return self.drawn[-1]


def _run_picker(func, *args, keys=(), **kwargs):
    """Drive one of the curses pickers over a FakeScreen."""
    screen = FakeScreen(keys)
    with (
        mock.patch.object(browser, "_prepare"),
        mock.patch.object(browser.curses, "color_pair", return_value=0),
    ):
        result = func(screen, *args, **kwargs)
    return result, screen


def _series(uid, study="s1", number="1", modality="CT",
            description="head", instances=3):
    return browser.SeriesRecord(
        uid=uid, study_uid=study, series_instance_uid=f"1.2.{uid}",
        series_number=number, modality=modality, description=description,
        instance_count=instances,
    )


class FakeSeriesSource:
    """A SeriesCache stand-in over series given up front."""

    def __init__(self, byStudy, fails: bool = False) -> None:
        self.byStudy = byStudy
        self.fails = fails
        self.loaded: list = []
        self.seen: set = set()

    def load(self, studyUUIDs, progress=None):
        if self.fails:
            raise RuntimeError("no server")
        self.loaded.append(list(studyUUIDs))
        for i, uuid in enumerate(studyUUIDs):
            if progress is not None and uuid not in self.seen:
                progress(i, len(studyUUIDs))
            self.seen.add(uuid)
        return {uuid: self.byStudy.get(uuid, []) for uuid in studyUUIDs}

    def get(self, studyUUID):
        self.seen.add(studyUUID)
        return self.byStudy.get(studyUUID, [])

    def known(self, studyUUID):
        return self.byStudy.get(studyUUID) if studyUUID in self.seen else None


class StudyPickerTest(unittest.TestCase):
    """The study list behaves as it did; s is the only key that is new."""

    def _pick(self, keys, records=None, preselected=None,
              preselectedSeries=None, source=None):
        records = records if records is not None else [_record("s1"), _record("s2")]
        return _run_picker(
            browser.run_curses_ui, records, preselected or set(),
            preselectedSeries or {}, source, keys=keys)

    def test_confirming_without_pressing_s_selects_whole_studies(self):
        picked, _screen = self._pick([SPACE, ENTER])
        self.assertEqual(picked, ({"s1"}, {}))

    def test_a_cancel_is_still_a_cancel(self):
        picked, _screen = self._pick([SPACE, ord("q")])
        self.assertIsNone(picked)

    def test_confirming_nothing_is_a_confirmed_empty_selection(self):
        # Not a cancel: only q/ESC is that.
        picked, _screen = self._pick([ENTER])
        self.assertEqual(picked, (set(), {}))

    def test_a_narrowed_study_is_marked_and_a_whole_one_is_not(self):
        source = FakeSeriesSource({"s1": [_series("a"), _series("b")],
                                   "s2": [_series("c", study="s2")]})
        source.get("s1"), source.get("s2")  # both listed, so both totals known
        _picked, screen = self._pick(
            [ENTER], preselected={"s1", "s2"},
            preselectedSeries={"s1": {"a"}, "s2": {"c"}}, source=source,
        )

        rows = screen.rows()
        # Some of s1's series, so marked; all of s2's, so the plain check --
        # the user checked everything, and a third marker for "everything,
        # pinned" would explain an implementation detail.
        self.assertTrue(rows[0].startswith("[~]"), rows[0])
        self.assertTrue(rows[1].startswith("[x]"), rows[1])
        self.assertIn("1 narrowed", screen.footer)

    def test_a_study_whose_series_were_never_listed_is_shown_as_narrowed(self):
        # Restored from a file and not yet listed: there is no total to compare
        # against, and a list must not pay a request per row to find one.
        _picked, screen = self._pick(
            [ENTER], preselected={"s1"}, preselectedSeries={"s1": {"a"}},
            source=FakeSeriesSource({"s1": [_series("a")]}),
        )
        self.assertTrue(screen.rows()[0].startswith("[~]"))

    def test_unchecking_a_study_leaves_none_of_its_series_behind(self):
        picked, _screen = self._pick(
            [SPACE, ENTER], preselected={"s1", "s2"},
            preselectedSeries={"s1": {"a"}},
            source=FakeSeriesSource({"s1": [_series("a")]}),
        )
        self.assertEqual(picked, ({"s2"}, {}))

    def test_s_with_nothing_checked_says_so_and_asks_the_server_nothing(self):
        source = FakeSeriesSource({"s1": [_series("a")]})
        _picked, screen = self._pick([ord("s"), ENTER], source=source)

        self.assertIn("needs some checked studies", screen.footer)
        self.assertEqual(source.loaded, [])

    def test_enter_in_the_series_picker_confirms_the_browse(self):
        # ENTER means the same thing on either screen, so one press finishes.
        source = FakeSeriesSource({"s1": [_series("a1"), _series("a2")]})
        picked, _screen = self._pick(
            [SPACE, ord("s"), curses.KEY_DOWN, SPACE, ENTER], source=source)
        self.assertEqual(picked, ({"s1"}, {"s1": {"a1"}}))

    def test_a_sub_selection_that_empties_a_study_still_confirms(self):
        source = FakeSeriesSource({"s1": [_series("a1")]})
        picked, _screen = self._pick(
            [SPACE, ord("s"), ord("n"), ENTER], source=source)
        self.assertEqual(picked, (set(), {}))

    def test_discarding_a_sub_selection_goes_back_to_the_study_list(self):
        # q/ESC is the one place the two pickers differ: it drops the
        # sub-selection, and the study list is still there to confirm.
        source = FakeSeriesSource({"s1": [_series("a1"), _series("a2")]})
        picked, screen = self._pick(
            [SPACE, ord("s"), SPACE, ord("q"), ENTER], source=source)

        self.assertEqual(picked, ({"s1"}, {}))
        self.assertIn("discarded", screen.footer)

    def test_s_without_a_series_source_is_not_offered(self):
        _picked, screen = self._pick([SPACE, ord("s"), ENTER])
        self.assertIn("not available", screen.footer)

    def test_a_server_that_cannot_list_series_is_reported_in_the_footer(self):
        _picked, screen = self._pick(
            [SPACE, ord("s"), ENTER],
            source=FakeSeriesSource({}, fails=True),
        )
        self.assertIn("could not load series", screen.footer)

    def test_the_wait_for_a_listing_is_said_out_loud(self):
        source = FakeSeriesSource({"s1": [_series("a")]})
        _picked, screen = self._pick([SPACE, ord("s"), ENTER], source=source)

        drawn = " ".join(line for frame in screen.frames
                         for line in frame.values())
        self.assertIn("loading series for study 1 of 1", drawn)


class SeriesPickerTest(unittest.TestCase):
    """The series of every checked study, as one list."""

    RECORDS = [_record("s1"), _record("s2")]
    BY_STUDY = {
        "s1": [_series("a1", number="1"), _series("a2", number="2")],
        "s2": [_series("b1", study="s2", number="1")],
    }

    def _pick(self, keys, covered=("s1", "s2"), current=None, byStudy=None):
        return _run_picker(
            browser.run_series_ui, self.RECORDS, list(covered),
            byStudy if byStudy is not None else self.BY_STUDY,
            current or {}, keys=keys)

    def test_everything_is_checked_when_a_study_has_no_sub_selection_yet(self):
        # The whole study is what it currently means, so that is what shows.
        picked, _screen = self._pick([ENTER])
        self.assertEqual(picked, {"s1": {"a1", "a2"}, "s2": {"b1"}})

    def test_a_studys_current_sub_selection_is_what_reopens(self):
        picked, screen = self._pick([ENTER], current={"s1": {"a2"}})

        self.assertEqual(picked["s1"], {"a2"})
        checked = [row for row in screen.rows() if row.startswith("[x]")]
        self.assertEqual(len(checked), 2)  # a2 and the whole of s2

    def test_one_cursor_runs_through_every_study(self):
        # Down twice from the first series lands on the second study's series,
        # skipping the heading that groups it.
        picked, _screen = self._pick(
            [curses.KEY_DOWN, curses.KEY_DOWN, SPACE, ENTER])
        self.assertEqual(picked, {"s1": {"a1", "a2"}, "s2": set()})

    def test_studies_are_headings_the_cursor_never_lands_on(self):
        _picked, screen = self._pick([ENTER])
        rows = screen.rows()

        self.assertTrue(rows[0].startswith("---"))
        self.assertTrue(rows[1].startswith("[x]"))
        self.assertTrue(rows[3].startswith("---"))

    def test_a_and_n_cover_every_series_of_every_study_at_once(self):
        allOf, _screen = self._pick([ord("n"), ord("a"), ENTER])
        self.assertEqual(allOf, {"s1": {"a1", "a2"}, "s2": {"b1"}})

        none, _screen = self._pick([ord("n"), ENTER])
        self.assertEqual(none, {"s1": set(), "s2": set()})

    def test_q_discards_the_sub_selection_rather_than_the_browse(self):
        for key in (ord("q"), ESC):
            picked, _screen = self._pick([SPACE, key])
            self.assertIsNone(picked)

    def test_the_footer_says_which_way_enter_and_q_go(self):
        _picked, screen = self._pick([ENTER])
        self.assertIn("ENTER confirm", screen.footer)
        self.assertIn("q/ESC discard changes", screen.footer)

    def test_the_footer_warns_that_a_study_would_be_un_checked(self):
        _picked, screen = self._pick([ord("n"), ENTER])
        self.assertIn("would be un-checked", screen.footer)

    def test_a_row_shows_what_the_series_is(self):
        _picked, screen = self._pick([ENTER])
        row = screen.rows()[1]
        self.assertIn("CT", row)
        self.assertIn("head", row)
        self.assertIn("3", row)

    def test_a_picker_with_nothing_to_narrow_asks_nothing(self):
        # _sub_select says so before it gets this far; this is the guard for a
        # caller that did not.
        picked, _screen = self._pick([], byStudy={"s1": [], "s2": []})
        self.assertIsNone(picked)


class SubSelectTest(unittest.TestCase):
    """What the study list does with what the series picker returns."""

    def _sub(self, keys, selected, series=None, byStudy=None):
        records = [_record("s1"), _record("s2")]
        source = FakeSeriesSource(byStudy if byStudy is not None else {
            "s1": [_series("a1"), _series("a2")],
            "s2": [_series("b1", study="s2")],
        })
        selected = set(selected)
        series = dict(series or {})
        screen = FakeScreen(keys)
        with (
            mock.patch.object(browser, "_prepare"),
            mock.patch.object(browser.curses, "color_pair", return_value=0),
        ):
            message, confirmed = browser._sub_select(
                screen, records, selected, series, source)
        return message, confirmed, selected, series, source

    def test_it_covers_every_checked_study_in_row_order(self):
        _message, _confirmed, _selected, _series, source = self._sub(
            [ENTER], selected={"s2", "s1"})
        self.assertEqual(source.loaded, [["s1", "s2"]])

    def test_a_fully_checked_study_keeps_its_explicit_list(self):
        """series_uids records which studies were looked at, not only which
        were narrowed."""
        _message, _confirmed, selected, series, _source = self._sub(
            [ENTER], selected={"s1", "s2"})

        self.assertEqual(selected, {"s1", "s2"})
        self.assertEqual(series, {"s1": {"a1", "a2"}, "s2": {"b1"}})

    def test_enter_confirms_the_browse_and_q_only_the_sub_selection(self):
        _message, confirmed, _selected, _series, _source = self._sub(
            [ENTER], selected={"s1"})
        self.assertTrue(confirmed)

        _message, confirmed, _selected, _series, _source = self._sub(
            [ord("q")], selected={"s1"})
        self.assertFalse(confirmed)

    def test_nothing_to_pick_from_does_not_confirm_the_browse(self):
        for keys, selected, byStudy in (
            ([], set(), None),                       # no checked studies
            ([], {"s1"}, {"s1": []}),                # a stale selection
        ):
            _message, confirmed, _selected, _series, _source = self._sub(
                keys, selected=selected, byStudy=byStudy)
            self.assertFalse(confirmed)

    def test_a_study_left_with_no_series_is_un_checked_instead(self):
        # No series is no study, so it is not written as an empty list.
        message, _confirmed, selected, series, _source = self._sub(
            [ord("n"), ENTER], selected={"s1", "s2"})

        self.assertEqual(selected, set())
        self.assertEqual(series, {})
        self.assertIn("un-checked", message)

    def test_a_discarded_sub_selection_leaves_the_previous_state_intact(self):
        message, _confirmed, selected, series, _source = self._sub(
            [ord("n"), ord("q")], selected={"s1"}, series={"s1": {"a1"}})

        self.assertEqual(selected, {"s1"})
        self.assertEqual(series, {"s1": {"a1"}})
        self.assertIn("discarded", message)

    def test_studies_with_no_series_at_all_is_a_stale_selection(self):
        message, _confirmed, selected, series, _source = self._sub(
            [], selected={"s1"}, byStudy={"s1": []})

        self.assertIn("has any series to pick from", message)
        # Nothing said, so nothing changed.
        self.assertEqual(selected, {"s1"})
        self.assertEqual(series, {})

    def test_a_study_the_picker_never_covered_stays_absent(self):
        _message, _confirmed, selected, series, _source = self._sub(
            [ENTER], selected={"s1"})

        self.assertEqual(selected, {"s1"})
        self.assertNotIn("s2", series)


class StageTableTest(unittest.TestCase):
    """The y/n review of a staged run's output."""

    ROWS = (
        StagedSeries(key="1.2.1", seriesInstanceUID="1.2.1", seriesNumber="1",
                     modality="CT", description="axial", instanceCount=40,
                     byteCount=2048),
        StagedSeries(key="1.2.2", seriesInstanceUID="1.2.2", seriesNumber="2",
                     modality="CT", description="coronal", instanceCount=3,
                     byteCount=512),
    )

    def _review(self, keys):
        return _run_picker(browser.run_stage_ui, list(self.ROWS), keys=keys)

    def test_every_row_starts_accepted(self):
        accepted, screen = self._review([ENTER])

        # Confirming the table untouched does what the same run without
        # --stage would have done: the worst a reflexive confirm can do.
        self.assertEqual(accepted, {"1.2.1", "1.2.2"})
        self.assertTrue(all(row.startswith("[y]") for row in screen.rows()))

    def test_rejecting_is_the_action(self):
        accepted, _screen = self._review([SPACE, ENTER])
        self.assertEqual(accepted, {"1.2.2"})

    def test_n_rejects_everything_as_an_escape_hatch(self):
        accepted, _screen = self._review([ord("n"), ENTER])
        self.assertEqual(accepted, set())

    def test_a_row_shows_what_the_series_cost(self):
        _accepted, screen = self._review([ENTER])
        row = screen.rows()[0]
        self.assertIn("axial", row)
        self.assertIn("40", row)
        self.assertIn("2.0 KiB", row)
        # The count is of output instances, so the heading has to say so.
        self.assertIn("Out. inst.", screen.drawn[0])

    def test_there_is_no_cancel_because_there_is_nothing_to_cancel(self):
        for key in (ord("q"), ESC):
            accepted, screen = self._review([key, ENTER])
            # q neither commits nor discards: it says why, and waits. With a
            # default of accept-everything it could only mean commit nothing,
            # which is the exact opposite of the ENTER sitting next to it.
            self.assertEqual(accepted, {"1.2.1", "1.2.2"})
            self.assertIn("nothing to cancel", screen.footer)
            # So the footer never offers it as a way out.
            self.assertNotIn("q/ESC", screen.frames[0][23])


# A selection file holds Orthanc identifiers and refuses anything else, so
# the studies and series that go through one are named the way Orthanc does.
S1 = studyUUID("PID", "1.2.s1")
S2 = studyUUID("PID", "1.2.s2")
A1 = seriesUUID("PID", "1.2.s1", "1.2.a1")
A2 = seriesUUID("PID", "1.2.s1", "1.2.a2")
GONE = seriesUUID("PID", "1.2.s1", "1.2.gone")


class MainSelectionFileTest(unittest.TestCase):
    """--save-selection and --restore-selection, through main()."""

    def _main(self, argv, picked=None, records=None, byStudy=None):
        records = records if records is not None else [_record(S1), _record(S2)]
        source = FakeSeriesSource(byStudy if byStudy is not None else {})
        err = io.StringIO()
        with (
            mock.patch.object(browser, "Orthanc"),
            mock.patch.object(browser, "fetch_all_studies", return_value=records),
            mock.patch.object(browser, "SeriesCache", return_value=source),
            mock.patch.object(browser.curses, "wrapper",
                              return_value=picked) as wrapper,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            code = browser.main(argv)
        return code, wrapper, source, err.getvalue()

    def test_a_sub_selection_is_saved_and_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "selection.json")

            code, _wrapper, _source, err = self._main(
                ["--save-selection", path],
                picked=({S1, S2}, {S1: {A1}}),
            )
            self.assertEqual(code, 0)
            self.assertIn("2 study UUID(s) and 1 series UUID(s)", err)

            code, wrapper, _source, _err = self._main(
                ["--restore-selection", path],
                picked=({S1}, {}),
                byStudy={S1: [_series(A1, study=S1), _series(A2, study=S1)]},
            )

        self.assertEqual(code, 0)
        # Both halves are pre-checked in the picker.
        self.assertEqual(wrapper.call_args[0][2], {S1, S2})
        self.assertEqual(wrapper.call_args[0][3], {S1: {A1}})

    def test_a_study_only_save_writes_the_file_it_always_wrote(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "selection.json"
            code, _wrapper, _source, err = self._main(
                ["--save-selection", str(path)], picked=({S1}, {}))

            data = json.loads(path.read_text())

        self.assertEqual(code, 0)
        self.assertNotIn("series_uids", data)
        self.assertIn("Saved 1 study UUID(s) to", err)

    def test_a_file_written_before_series_existed_still_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "selection.json"
            path.write_text(json.dumps({
                "criteria": {field.key: None for field in SEARCH_FIELDS},
                "study_uids": [S1],
            }))
            code, wrapper, source, _err = self._main(
                ["--restore-selection", str(path)], picked=({S1}, {}))

        self.assertEqual(code, 0)
        self.assertEqual(wrapper.call_args[0][2], {S1})
        self.assertEqual(wrapper.call_args[0][3], {})
        # Nothing to validate, so no study is listed at start up.
        self.assertEqual(source.seen, set())

    def test_a_restored_series_that_has_gone_is_a_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "selection.json"
            path.write_text(json.dumps({
                "criteria": {field.key: None for field in SEARCH_FIELDS},
                "selection_level": "series",
                "study_uids": [S1],
                "series_uids": {S1: [A1, GONE]},
            }))
            code, wrapper, _source, err = self._main(
                ["--restore-selection", str(path)],
                byStudy={S1: [_series(A1, study=S1)]},
            )

        # Strict, and machine readable: the far end of an IPC channel cannot
        # answer a prompt.
        self.assertEqual(code, 1)
        self.assertIn("no longer hold", err)
        self.assertIn(f"study {S1}", err)
        wrapper.assert_not_called()

    def test_a_contradictory_file_is_refused_before_anything_is_fetched(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "selection.json"
            path.write_text(json.dumps({
                "criteria": {field.key: None for field in SEARCH_FIELDS},
                "selection_level": "study",
                "study_uids": [S1],
                "series_uids": {S1: [A1]},
            }))
            code, wrapper, _source, err = self._main(
                ["--restore-selection", str(path)])

        self.assertEqual(code, 1)
        self.assertIn("disagree", err)
        wrapper.assert_not_called()

    def test_mismatched_criteria_are_still_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "selection.json"
            path.write_text(json.dumps(
                {"criteria": {"patient_id": "OTHER"}, "study_uids": [S1]}))
            code, wrapper, _source, err = self._main(
                ["--restore-selection", str(path)])

        self.assertEqual(code, 1)
        self.assertIn("do not match", err)
        wrapper.assert_not_called()

    def test_a_file_that_is_not_there_is_reported_as_such(self):
        code, _wrapper, _source, err = self._main(
            ["--restore-selection", "/nonexistent/selection.json"])

        self.assertEqual(code, 1)
        self.assertIn("could not read --restore-selection file", err)


class MainStageOptionsTest(unittest.TestCase):
    def test_the_output_cache_default_is_the_documented_one(self):
        args = browser.parse_args([])
        self.assertFalse(args.stage)
        self.assertEqual(browser.parseByteSize(args.output_cache),
                         browser.DEFAULT_OUTPUT_CACHE_BYTES)

    def test_a_size_that_makes_no_sense_fails_before_anything_is_searched(self):
        err = io.StringIO()
        with (
            mock.patch.object(browser, "fetch_all_studies") as fetch,
            contextlib.redirect_stderr(err),
        ):
            code = browser.main(["--output-cache", "lots"])

        self.assertEqual(code, 1)
        self.assertIn("Invalid size", err.getvalue())
        fetch.assert_not_called()

    def test_staging_with_nowhere_to_commit_to_says_so(self):
        err = io.StringIO()
        with (
            mock.patch.object(browser, "Orthanc"),
            mock.patch.object(browser, "loadApplicationClass",
                              return_value=RecordingApplication),
            mock.patch.object(browser, "fetch_all_studies", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            browser.main(["--module", "fake_app", "--stage", "--no-upload"])

        self.assertIn("nowhere to commit output to", err.getvalue())

    def test_stage_routes_the_run_through_the_review(self):
        records = [_record("s1")]
        with (
            mock.patch.object(browser, "Orthanc"),
            mock.patch.object(browser, "loadApplicationClass",
                              return_value=RecordingApplication),
            mock.patch.object(browser, "fetch_all_studies", return_value=records),
            mock.patch.object(browser, "SeriesCache"),
            mock.patch.object(browser.curses, "wrapper",
                              return_value=({"s1"}, {})),
            mock.patch.object(browser, "OrthancHost") as hostClass,
            mock.patch.object(browser, "run_staged_application",
                              return_value=0) as staged,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = browser.main(["--module", "fake_app", "--stage",
                                 "--output-cache", "1K"])

        self.assertEqual(code, 0)
        staged.assert_called_once_with(
            hostClass.return_value, RecordingApplication, 1024)


class RunStagedApplicationTest(unittest.TestCase):
    """The run, the table and the commit, in that order."""

    ROW = StagedSeries(key="1.2.1", seriesInstanceUID="1.2.1", seriesNumber="1",
                       modality="CT", description="axial", instanceCount=2,
                       byteCount=10)

    def _run(self, rows, accepted=frozenset({"1.2.1"}), sendResult=True,
             commitResult=True):
        with (
            mock.patch.object(browser, "StagingHost") as stagingClass,
            mock.patch.object(browser.curses, "wrapper",
                              return_value=set(accepted)) as wrapper,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            staging = stagingClass.return_value
            staging.stagedOutputs = rows
            staging.sendInputs.return_value = sendResult
            staging.commitStagedOutputs.return_value = commitResult
            code = browser.run_staged_application(
                "host", RecordingApplication, 4096)
        return code, stagingClass, staging, wrapper

    def test_the_host_is_wrapped_with_the_budget_it_was_given(self):
        _code, stagingClass, staging, _wrapper = self._run([self.ROW])

        stagingClass.assert_called_once_with("host", outputCacheBytes=4096)
        self.assertIsInstance(staging.setApplication.call_args[0][0],
                              RecordingApplication)
        self.assertTrue(staging.sendInputs.called)

    def test_only_the_accepted_rows_are_committed(self):
        code, _stagingClass, staging, wrapper = self._run(
            [self.ROW], accepted=set())

        self.assertEqual(code, 0)
        self.assertEqual(wrapper.call_args[0][1], [self.ROW])
        staging.commitStagedOutputs.assert_called_once_with(set())

    def test_a_run_that_produced_no_output_skips_the_table(self):
        code, _stagingClass, staging, wrapper = self._run([])

        self.assertEqual(code, 0)
        wrapper.assert_not_called()
        staging.commitStagedOutputs.assert_not_called()
        # And leaves nothing behind.
        staging.discardStagedOutputs.assert_called_once_with()

    def test_a_failed_commit_is_a_nonzero_exit(self):
        code, _stagingClass, _staging, _wrapper = self._run(
            [self.ROW], commitResult=False)
        self.assertEqual(code, 1)

    def test_an_application_that_cancelled_still_gets_its_output_reviewed(self):
        code, _stagingClass, staging, wrapper = self._run(
            [self.ROW], sendResult=False)

        # The run failed, but what it did produce is still the user's to keep.
        self.assertTrue(wrapper.called)
        self.assertTrue(staging.commitStagedOutputs.called)
        self.assertEqual(code, 1)


def _server_would_match(query, record) -> bool:
    """Apply an Orthanc /tools/find Query to a StudyRecord the way the server
    would: case insensitive DICOM wildcards, and ranges on StudyDate."""
    values = {
        "PatientID": record.patient_id,
        "PatientName": f"{record.patient_surname}^{record.patient_given_name}",
        "PatientBirthDate": record.dob,
        "AccessionNumber": record.accession,
        "StudyDescription": record.description,
    }
    for tag, pattern in query.items():
        if tag == "StudyDate":
            low, _, high = pattern.partition("-")
            if not record.study_date:
                return False
            if low and record.study_date < low:
                return False
            if high and record.study_date > high:
                return False
            continue
        if not fnmatch.fnmatch(values[tag].lower(), pattern.lower()):
            return False
    return True


class SearchFieldsTest(unittest.TestCase):
    """The one table behind the --search-* options, the criteria and matches()."""

    def test_every_field_reaches_the_criteria_from_its_own_option(self):
        args = browser.parse_args([
            "--search-patient-id", "PID001",
            "--search-patient-surname", "Smith",
            "--search-patient-givenname", "John",
            "--search-dob", "1980-01-01",
            "--search-study-after", "2020-01-01",
            "--search-study-before", "20201231",
            "--search-accession", "ACC42",
            "--search-description", "HEAD",
        ])

        # Dates arrive in either spelling and are stored as DICOM DA.
        self.assertEqual(browser.build_criteria(args), {
            "patient_id": "PID001",
            "patient_surname": "Smith",
            "patient_givenname": "John",
            "dob": "19800101",
            "study_after": "20200101",
            "study_before": "20201231",
            "accession": "ACC42",
            "description": "HEAD",
        })

    def test_an_unused_option_leaves_its_criterion_unset(self):
        criteria = browser.build_criteria(browser.parse_args([]))
        self.assertEqual(set(criteria), {field.key for field in SEARCH_FIELDS})
        self.assertEqual(set(criteria.values()), {None})

    def test_every_field_names_a_record_attribute_that_exists(self):
        record = _record()
        for field in SEARCH_FIELDS:
            self.assertTrue(hasattr(record, field.attr), field.key)


class MatchesTest(unittest.TestCase):
    """What each criterion accepts, once it is set."""

    def _matches(self, record, **criteria) -> bool:
        return studies.matches(record, criteria)

    def test_no_criteria_matches_every_study(self):
        self.assertTrue(studies.matches(_record(), {}))

    def test_text_criteria_are_case_insensitive_substrings(self):
        record = _record()
        self.assertTrue(self._matches(record, patient_id="pi"))
        self.assertTrue(self._matches(record, patient_surname="do"))
        self.assertTrue(self._matches(record, patient_givenname="ANE"))
        self.assertTrue(self._matches(record, accession="cc"))
        self.assertTrue(self._matches(record, description="DES"))
        self.assertFalse(self._matches(record, description="chest"))

    def test_the_two_name_criteria_narrow_different_halves_of_the_name(self):
        record = _record(patient_surname="Doe", patient_given_name="Jane")
        self.assertFalse(self._matches(record, patient_givenname="Doe"))
        self.assertFalse(self._matches(record, patient_surname="Jane"))

    def test_a_date_of_birth_is_matched_exactly(self):
        record = _record(dob="19700101")
        self.assertTrue(self._matches(record, dob="19700101"))
        # Not a prefix and not a substring: a date is the whole value or nothing.
        self.assertFalse(self._matches(record, dob="1970010"))

    def test_the_study_date_bounds_include_their_own_day(self):
        record = _record(study_date="20260101")
        self.assertTrue(self._matches(record, study_after="20260101"))
        self.assertTrue(self._matches(record, study_before="20260101"))
        self.assertFalse(self._matches(record, study_after="20260102"))
        self.assertFalse(self._matches(record, study_before="20251231"))

    def test_a_study_without_a_date_fails_any_bound(self):
        record = _record(study_date="")
        self.assertFalse(self._matches(record, study_after="20200101"))
        self.assertFalse(self._matches(record, study_before="20301231"))
        # With no bound set it is still a hit.
        self.assertTrue(self._matches(record, description="desc"))

    def test_every_criterion_set_must_match(self):
        record = _record()
        both = {"patient_surname": "Doe", "description": "desc"}
        self.assertTrue(studies.matches(record, both))
        self.assertFalse(studies.matches(record, {**both, "description": "chest"}))


class BuildQueryTest(unittest.TestCase):
    """build_query() must narrow the find without ever excluding a hit."""

    def _criteria(self, **overrides):
        base = {field.key: None for field in SEARCH_FIELDS}
        base.update(overrides)
        return base

    def test_no_criteria_queries_everything(self):
        self.assertEqual(studies.build_query(None), {})
        self.assertEqual(studies.build_query(self._criteria()), {})

    def test_substring_criteria_become_wildcards(self):
        query = studies.build_query(self._criteria(
            patient_id="123", accession="ACC", description="HEAD",
        ))
        self.assertEqual(query, {
            "PatientID": "*123*",
            "AccessionNumber": "*ACC*",
            "StudyDescription": "*HEAD*",
        })

    def test_dob_is_matched_exactly(self):
        query = studies.build_query(self._criteria(dob="19800101"))
        self.assertEqual(query["PatientBirthDate"], "19800101")

    def test_date_bounds_become_a_dicom_range(self):
        both = studies.build_query(self._criteria(
            study_after="20200101", study_before="20201231",
        ))
        self.assertEqual(both["StudyDate"], "20200101-20201231")

        after = studies.build_query(self._criteria(study_after="20200101"))
        self.assertEqual(after["StudyDate"], "20200101-")

        before = studies.build_query(self._criteria(study_before="20201231"))
        self.assertEqual(before["StudyDate"], "-20201231")

    def test_name_criteria_share_one_widened_patient_name(self):
        surname = studies.build_query(self._criteria(patient_surname="Smith"))
        self.assertEqual(surname["PatientName"], "*Smith*")

        given = studies.build_query(self._criteria(patient_givenname="John"))
        self.assertEqual(given["PatientName"], "*John*")

        # Only one pattern can be sent for the single PatientName tag; the
        # given name is left to matches().
        both = studies.build_query(self._criteria(
            patient_surname="Smith", patient_givenname="John",
        ))
        self.assertEqual(both["PatientName"], "*Smith*")

    def test_query_never_excludes_a_study_matches_accepts(self):
        """The whole point of the widening: no hit is lost server side."""
        record = studies.StudyRecord(
            uid="s1",
            study_instance_uid="1.2.3",
            patient_id="PID123",
            patient_surname="Smith",
            patient_given_name="John",
            patient_name_display="Smith John",
            dob="19800101",
            study_date="20200601",
            accession="ACC9",
            description="CT HEAD",
        )
        criteria = self._criteria(
            patient_id="id12", patient_surname="smi", patient_givenname="ohn",
            dob="19800101", study_after="20200101", study_before="20201231",
            accession="cc9", description="head",
        )
        self.assertTrue(studies.matches(record, criteria))

        # Replaying the query the way Orthanc would still returns the study.
        self.assertTrue(_server_would_match(studies.build_query(criteria), record))

    def test_query_keeps_a_hit_the_given_name_alone_would_lose(self):
        """PatientName carries the surname pattern, but a study matching on
        the given name only must still survive the server side filter."""
        record = studies.StudyRecord(
            uid="s2", study_instance_uid="1.2.4", patient_id="PID9",
            patient_surname="Jones", patient_given_name="Smith",
            patient_name_display="Jones Smith", dob="", study_date="20200601",
            accession="", description="",
        )
        criteria = self._criteria(patient_givenname="Smith")

        self.assertTrue(studies.matches(record, criteria))
        self.assertTrue(_server_would_match(studies.build_query(criteria), record))


class FetchAllStudiesTest(unittest.TestCase):
    """Paging must survive a server that returns fewer rows than asked for."""

    @staticmethod
    def _entry(uid):
        return {
            "ID": uid,
            "MainDicomTags": {"StudyDescription": "CT"},
            "PatientMainDicomTags": {"PatientName": "Smith^John"},
        }

    def _client(self, pages):
        client = mock.Mock()
        client.post_tools_find.side_effect = pages
        return client

    def test_pages_until_an_empty_page(self):
        # A short page does not mean the end of the data: a server enforcing
        # its own find limit answers with fewer rows than Limit asked for.
        client = self._client([
            [self._entry(f"a{i}") for i in range(10)],
            [self._entry(f"b{i}") for i in range(10)],
            [],
        ])
        records = studies.fetch_all_studies(client)

        self.assertEqual(len(records), 20)
        self.assertEqual(client.post_tools_find.call_count, 3)

    def test_since_advances_by_rows_actually_returned(self):
        client = self._client([
            [self._entry(f"a{i}") for i in range(10)],
            [],
        ])
        studies.fetch_all_studies(client)

        sinces = [c.args[0]["Since"] for c in client.post_tools_find.call_args_list]
        # Advancing by FIND_PAGE_SIZE here would skip studies 10..999.
        self.assertEqual(sinces, [0, 10])

    def test_criteria_are_sent_to_the_server(self):
        client = self._client([[]])
        studies.fetch_all_studies(client, {"patient_id": "123"})

        body = client.post_tools_find.call_args.args[0]
        self.assertEqual(body["Query"], {"PatientID": "*123*"})
        self.assertFalse(body["CaseSensitive"])

    def test_a_server_ignoring_since_does_not_loop_forever(self):
        page = [self._entry("a1")]
        client = mock.Mock()
        client.post_tools_find.return_value = page

        records = studies.fetch_all_studies(client)

        self.assertEqual([r.uid for r in records], ["a1"])


if __name__ == "__main__":
    unittest.main()
