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

import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import pydicom

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from OrthancRC.enums import Status  # noqa: E402
from OrthancRC.orthanc import OrthancHost  # noqa: E402

DATA_FILE = Path(__file__).resolve().parent / "CT_small.dcm"


def _dicomBytes(sopUID: str) -> bytes:
    """One real instance, stamped so a test can tell which one came back."""
    ds = pydicom.dcmread(str(DATA_FILE))
    ds.SOPInstanceUID = sopUID
    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def _series(uid: str, number, seriesUID: str = None) -> dict:
    tags = {"SeriesInstanceUID": seriesUID or f"1.2.{uid}"}
    if number is not None:
        tags["SeriesNumber"] = number
    return {"ID": uid, "MainDicomTags": tags}


def _instance(uid: str, parent: str, number, sopUID: str = None) -> dict:
    tags = {"SOPInstanceUID": sopUID or f"1.3.{uid}"}
    if number is not None:
        tags["InstanceNumber"] = number
    return {"ID": uid, "ParentSeries": parent, "MainDicomTags": tags}


class FakeOrthanc:
    """Just enough of the pyorthanc client to expand a study selection.

    Children are handed back in the order given, which is what a real server
    does not promise: every test here shuffles them deliberately.
    """

    def __init__(self, studies: dict, failFor: set = None) -> None:
        # {study UUID: (study record, [series entries], [instance entries])}
        self._studies = studies
        self.calls: list = []
        self._failFor = failFor or set()
        self._lock = threading.Lock()
        # Instance UUIDs whose download has begun, in order, and an event per
        # instance so a test can wait for one to start rather than sleep.
        self.downloaded: list = []
        self.started: dict = {}
        # Held open to keep a download in flight for as long as a test likes.
        self.release = threading.Event()
        self.release.set()

    def get_studies_id(self, uuid: str) -> dict:
        self.calls.append(("study", uuid))
        return self._studies[uuid][0]

    def get_studies_id_series(self, uuid: str, params=None) -> list:
        self.calls.append(("series", uuid))
        return list(self._studies[uuid][1])

    def get_studies_id_instances(self, uuid: str, params=None) -> list:
        self.calls.append(("instances", uuid))
        return list(self._studies[uuid][2])

    def get_instances_id_file(self, uuid: str) -> bytes:
        with self._lock:
            self.downloaded.append(uuid)
            self.started.setdefault(uuid, threading.Event()).set()
        self.release.wait(timeout=5)
        if uuid in self._failFor:
            raise RuntimeError(f"boom for {uuid}")
        return _dicomBytes(f"1.3.{uuid}")

    def hasStarted(self, uuid: str, timeout: float = 5) -> bool:
        """Whether a download for `uuid` has begun, waiting up to `timeout`."""
        with self._lock:
            event = self.started.setdefault(uuid, threading.Event())
        return event.wait(timeout)

    # The host holds these to expand a listing of bare UUIDs. Every listing
    # here is already expanded, so reaching for one is a request too many.
    def get_series_id(self, uuid: str) -> dict:
        raise AssertionError(f"fetched series {uuid} that the listing already held")

    def get_instances_id(self, uuid: str) -> dict:
        raise AssertionError(f"fetched instance {uuid} that the listing already held")


def _study(series: list, instances: list, **tags) -> tuple:
    record = {
        "PatientMainDicomTags": {"PatientID": "PID", "PatientName": "Doe^Jane"},
        "MainDicomTags": {"StudyInstanceUID": "1.1.1", "StudyDate": "20260101"},
    }
    record["MainDicomTags"].update(tags)
    return record, series, instances


class InstanceOrderTest(unittest.TestCase):
    """_loadInstances puts a study's instances into DICOM order."""

    def _host(self, studies: dict, order=None) -> OrthancHost:
        return OrthancHost(FakeOrthanc(studies), order or list(studies))

    def test_instances_are_grouped_by_series_in_series_number_order(self):
        # Offered interleaved and with the higher series first.
        host = self._host({"st1": _study(
            [_series("sA", "2"), _series("sB", "1")],
            [_instance("a1", "sA", "1"), _instance("b1", "sB", "1"),
             _instance("a2", "sA", "2"), _instance("b2", "sB", "2")],
        )})

        self.assertEqual(host.instanceUUIDs, ["b1", "b2", "a1", "a2"])

    def test_instance_numbers_are_compared_as_numbers_not_strings(self):
        host = self._host({"st1": _study(
            [_series("sA", "1")],
            [_instance("i10", "sA", "10"), _instance("i2", "sA", "2"),
             _instance("i1", "sA", "1")],
        )})

        self.assertEqual(host.instanceUUIDs, ["i1", "i2", "i10"])

    def test_series_numbers_are_compared_as_numbers_too(self):
        host = self._host({"st1": _study(
            [_series("sA", "10"), _series("sB", "2")],
            [_instance("a1", "sA", "1"), _instance("b1", "sB", "1")],
        )})

        self.assertEqual(host.instanceUUIDs, ["b1", "a1"])

    def test_an_unnumbered_instance_sorts_after_the_numbered_ones(self):
        # Not in front of them, which treating a missing number as 0 would do.
        host = self._host({"st1": _study(
            [_series("sA", "1")],
            [_instance("none", "sA", None), _instance("i1", "sA", "1")],
        )})

        self.assertEqual(host.instanceUUIDs, ["i1", "none"])

    def test_an_unnumbered_series_sorts_after_the_numbered_ones(self):
        host = self._host({"st1": _study(
            [_series("sA", None), _series("sB", "3")],
            [_instance("a1", "sA", "1"), _instance("b1", "sB", "1")],
        )})

        self.assertEqual(host.instanceUUIDs, ["b1", "a1"])

    def test_a_malformed_number_is_not_an_error(self):
        host = self._host({"st1": _study(
            [_series("sA", "1")],
            [_instance("bad", "sA", "N/A"), _instance("i1", "sA", "1")],
        )})

        self.assertEqual(host.instanceUUIDs, ["i1", "bad"])

    def test_uids_break_a_tie_so_the_order_is_stable(self):
        # Two series sharing a number, and two instances sharing one.
        host = self._host({"st1": _study(
            [_series("sA", "1", seriesUID="1.2.9"), _series("sB", "1", seriesUID="1.2.1")],
            [_instance("a2", "sA", "1", sopUID="1.3.9"),
             _instance("a1", "sA", "1", sopUID="1.3.1"),
             _instance("b1", "sB", "1")],
        )})

        self.assertEqual(host.instanceUUIDs, ["b1", "a1", "a2"])

    def test_an_instance_of_an_unknown_series_is_still_offered(self):
        # A series listing that does not cover every instance must not lose
        # one; it sorts with the unnumbered.
        host = self._host({"st1": _study(
            [_series("sA", "1")],
            [_instance("orphan", "sZ", "1"), _instance("a1", "sA", "1")],
        )})

        self.assertEqual(host.instanceUUIDs, ["a1", "orphan"])

    def test_studies_keep_the_order_they_were_selected_in(self):
        studies = {
            "st1": _study([_series("sA", "1")], [_instance("a1", "sA", "1")]),
            "st2": _study([_series("sB", "1")], [_instance("b1", "sB", "1")]),
        }
        # Selected second-study-first: its instances come first, whatever the
        # series numbers say, because ordering is per study.
        host = self._host(studies, order=["st2", "st1"])

        self.assertEqual(host.instanceUUIDs, ["b1", "a1"])


class RequestCountTest(unittest.TestCase):
    def test_a_study_is_expanded_in_three_requests(self):
        client = FakeOrthanc({"st1": _study(
            [_series("sA", "1")],
            [_instance("a1", "sA", "1"), _instance("a2", "sA", "2")],
        )})
        host = OrthancHost(client, ["st1"])

        # Once, however often the instances are asked for: the expansion is
        # cached, and the per-child fetches the fake refuses never happen.
        self.assertEqual(host.instanceUUIDs, ["a1", "a2"])
        host.getMainTags("a1")
        self.assertEqual(client.calls,
                         [("study", "st1"), ("series", "st1"), ("instances", "st1")])


class MainTagsTest(unittest.TestCase):
    def test_patient_study_series_and_instance_tags_are_merged(self):
        host = OrthancHost(FakeOrthanc({"st1": _study(
            [_series("sA", "2")],
            [_instance("a1", "sA", "7")],
        )}), ["st1"])

        tags = host.getMainTags("a1")
        self.assertEqual(tags["PatientID"], "PID")
        self.assertEqual(tags["StudyInstanceUID"], "1.1.1")
        self.assertEqual(tags["SeriesNumber"], "2")
        self.assertEqual(tags["InstanceNumber"], "7")

    def test_an_unknown_instance_has_no_tags(self):
        host = OrthancHost(FakeOrthanc({"st1": _study(
            [_series("sA", "1")], [_instance("a1", "sA", "1")],
        )}), ["st1"])

        self.assertEqual(host.getMainTags("nope"), {})


class SendInputsTest(unittest.TestCase):
    """The Application is fed in the same order, and told which input is last."""

    class RecordingApp:
        def __init__(self, refuseAt: str = None) -> None:
            self.seen: list = []
            self._refuseAt = refuseAt

        def notifyInputAvailable(self, instanceUUID, mainTags, lastData) -> bool:
            self.seen.append((instanceUUID, lastData))
            return instanceUUID != self._refuseAt

    def _host(self) -> OrthancHost:
        return OrthancHost(FakeOrthanc({"st1": _study(
            [_series("sA", "2"), _series("sB", "1")],
            [_instance("a1", "sA", "1"), _instance("b1", "sB", "1"),
             _instance("b2", "sB", "2")],
        )}), ["st1"])

    def test_inputs_are_offered_in_order_with_the_last_one_flagged(self):
        host = self._host()
        app = self.RecordingApp()
        host.setApplication(app)

        self.assertTrue(host.sendInputs())
        self.assertEqual(app.seen,
                         [("b1", False), ("b2", False), ("a1", True)])

    def test_a_refused_input_stops_the_run(self):
        host = self._host()
        app = self.RecordingApp(refuseAt="b2")
        host.setApplication(app)

        self.assertFalse(host.sendInputs())
        self.assertEqual([uuid for uuid, _last in app.seen], ["b1", "b2"])

    def test_a_host_with_no_application_reports_it(self):
        host = self._host()
        self.assertFalse(host.sendInputs())
        self.assertIn("no application registered", host.messages[-1][1])

    def test_an_empty_selection_is_a_warning_not_a_run(self):
        host = OrthancHost(FakeOrthanc({}), [])
        host.setApplication(self.RecordingApp())

        self.assertFalse(host.sendInputs())
        self.assertIn("no instances", host.messages[-1][1])


class PrefetchTest(unittest.TestCase):
    """Downloads run ahead of the Application, but only where it wants them."""

    class ClaimingApp:
        """Takes the data of the instances named, and reports what it saw."""

        def __init__(self, host, claim, onInput=None) -> None:
            self._host = host
            self._claim = claim
            self._onInput = onInput
            self.got: dict = {}

        def notifyInputAvailable(self, instanceUUID, mainTags, lastData) -> bool:
            if self._claim is None or instanceUUID in self._claim:
                self.got[instanceUUID] = self._host.getInputData(instanceUUID)
            if self._onInput is not None:
                self._onInput(instanceUUID)
            return True

    def _host(self, count: int = 5, failFor: set = None, **kwargs) -> tuple:
        instances = [_instance(f"i{n}", "sA", str(n)) for n in range(1, count + 1)]
        client = FakeOrthanc({"st1": _study([_series("sA", "1")], instances)},
                             failFor=failFor)
        return OrthancHost(client, ["st1"], **kwargs), client

    def _run(self, host, client, app) -> None:
        host.setApplication(app)
        self.assertTrue(host.sendInputs())

    def test_the_next_instances_download_while_the_application_works(self):
        host, client = self._host(prefetchDepth=2)
        seen: list = []

        def onInput(uuid):
            if uuid == "i1":
                # Still inside the first input, and the window is in flight.
                seen.append(client.hasStarted("i2"))
                seen.append(client.hasStarted("i3"))

        app = self.ClaimingApp(host, claim=None, onInput=onInput)
        self._run(host, client, app)

        self.assertEqual(seen, [True, True])
        # Every instance was claimed, so every one was fetched exactly once.
        self.assertEqual(sorted(client.downloaded),
                         ["i1", "i2", "i3", "i4", "i5"])

    def test_the_data_handed_over_is_the_data_that_was_asked_for(self):
        host, client = self._host(prefetchDepth=2)
        app = self.ClaimingApp(host, claim=None)
        self._run(host, client, app)

        self.assertEqual(
            {uuid: str(ds.SOPInstanceUID) for uuid, ds in app.got.items()},
            {f"i{n}": f"1.3.i{n}" for n in range(1, 6)},
        )

    def test_an_application_that_wants_nothing_downloads_nothing(self):
        # Prefetching is driven by a claim, so an Application that reads main
        # tags and takes no data costs no transfers at all.
        host, client = self._host(prefetchDepth=2)
        app = self.ClaimingApp(host, claim=set())
        self._run(host, client, app)

        self.assertEqual(client.downloaded, [])

    def test_prefetching_resumes_once_the_application_claims_again(self):
        host, client = self._host(count=6, prefetchDepth=2)
        # Skips i2 and i3 entirely, then wants i4 onwards.
        app = self.ClaimingApp(host, claim={"i1", "i4", "i5", "i6"})
        self._run(host, client, app)

        # i2 and i3 were guessed at from i1, then nothing until i4 was taken.
        self.assertEqual(sorted(client.downloaded),
                         ["i1", "i2", "i3", "i4", "i5", "i6"])
        self.assertEqual(sorted(app.got), ["i1", "i4", "i5", "i6"])

    def test_no_prefetching_at_all_downloads_only_what_is_claimed(self):
        host, client = self._host(prefetchDepth=0)
        app = self.ClaimingApp(host, claim={"i1"})
        self._run(host, client, app)

        self.assertEqual(client.downloaded, ["i1"])

    def test_the_window_is_all_that_is_held_at_once(self):
        host, client = self._host(count=8, prefetchDepth=2)
        held: list = []
        app = self.ClaimingApp(
            host, claim=None,
            # Whatever has been fetched but not yet handed over.
            onInput=lambda _uuid: held.append(len(host._pending)),
        )
        self._run(host, client, app)

        self.assertLessEqual(max(held), 2)

    def test_an_application_that_claims_only_at_the_end_still_prefetches(self):
        """The two-pass shape: read every main tag, then ask for the data."""
        host, client = self._host(count=5, prefetchDepth=2)
        seen: list = []

        class TwoPassApp:
            def __init__(self) -> None:
                self.offered: list = []
                self.got: list = []

            def notifyInputAvailable(self, instanceUUID, mainTags, lastData) -> bool:
                self.offered.append(instanceUUID)
                if not lastData:
                    return True
                # Nothing has been downloaded yet: the offers took no data.
                seen.append(list(client.downloaded))
                for uuid in self.offered:
                    self.got.append(host.getInputData(uuid))
                    if uuid == "i1":
                        # Claiming the first one put the next two in flight.
                        seen.append(client.hasStarted("i2"))
                        seen.append(client.hasStarted("i3"))
                return True

        app = TwoPassApp()
        self._run(host, client, app)

        self.assertEqual(seen, [[], True, True])
        self.assertEqual(sorted(client.downloaded),
                         ["i1", "i2", "i3", "i4", "i5"])
        self.assertEqual(len(app.got), 5)

    def test_data_claimed_after_the_run_is_still_served_and_prefetched(self):
        host, client = self._host(count=4, prefetchDepth=2)

        class CollectingApp:
            def __init__(self) -> None:
                self.offered: list = []

            def notifyInputAvailable(self, instanceUUID, mainTags, lastData) -> bool:
                self.offered.append(instanceUUID)
                return True

        app = CollectingApp()
        self._run(host, client, app)
        self.assertEqual(client.downloaded, [])

        # The run is over and its pool is shut down; asking again starts a new
        # one rather than falling back to fetching one at a time.
        first = host.getInputData("i1")
        self.assertTrue(client.hasStarted("i2"))
        self.assertEqual(str(first.SOPInstanceUID), "1.3.i1")
        host.close()
        self.assertEqual(host._pending, {})

    def test_nothing_is_left_running_when_the_run_ends(self):
        host, client = self._host(prefetchDepth=2)
        self._run(host, client, self.ClaimingApp(host, claim=None))

        self.assertEqual(host._pending, {})

    def test_a_prefetch_failure_is_reported_when_the_data_is_claimed(self):
        host, client = self._host(failFor={"i3"}, prefetchDepth=2)
        app = self.ClaimingApp(host, claim=None)
        self._run(host, client, app)

        self.assertEqual(len(app.got["i3"]), 0)
        errors = [text for value, text in host.messages if value == Status.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn("could not download i3", errors[0])
        # The run carried on to the end regardless.
        self.assertEqual(sorted(app.got), ["i1", "i2", "i3", "i4", "i5"])

    def test_a_failure_for_data_that_is_never_claimed_is_never_reported(self):
        host, client = self._host(failFor={"i2"}, prefetchDepth=2)
        app = self.ClaimingApp(host, claim={"i1"})
        self._run(host, client, app)

        # i2 was fetched on spec and failed; nobody ever asked for it.
        self.assertIn("i2", client.downloaded)
        self.assertEqual([text for value, text in host.messages
                          if value == Status.ERROR], [])

    def test_a_refused_input_leaves_no_download_running(self):
        host, client = self._host(prefetchDepth=2)

        class RefusingApp:
            def notifyInputAvailable(self, instanceUUID, mainTags, lastData) -> bool:
                return instanceUUID != "i2"

        host.setApplication(RefusingApp())
        self.assertFalse(host.sendInputs())
        self.assertEqual(host._pending, {})

    def test_an_unselected_instance_is_still_refused(self):
        host, client = self._host(prefetchDepth=2)
        self.assertEqual(len(host.getInputData("nope")), 0)
        self.assertIn("not a selected instance", host.messages[-1][1])
        self.assertEqual(client.downloaded, [])


class ExpandTest(unittest.TestCase):
    def test_a_listing_of_bare_uuids_is_fetched_one_by_one(self):
        records = {"x": {"ID": "x", "MainDicomTags": {}}}
        fetched: list = []

        def fetch(uuid):
            fetched.append(uuid)
            return records[uuid]

        entries = OrthancHost._expand(["x", {"ID": "y"}], fetch)
        # Only the bare UUID costs a request; a full record is passed through.
        self.assertEqual(fetched, ["x"])
        self.assertEqual([e["ID"] for e in entries], ["x", "y"])


class SeriesSelectionTest(unittest.TestCase):
    """A study may be narrowed to some of its series, by Orthanc identifier."""

    STUDY = _study(
        [_series("sA", "1"), _series("sB", "2")],
        [_instance("a1", "sA", "1"), _instance("a2", "sA", "2"),
         _instance("b1", "sB", "1")],
    )

    def _host(self, seriesUUIDs=None) -> OrthancHost:
        return OrthancHost(FakeOrthanc({"st1": self.STUDY}), ["st1"],
                           seriesUUIDs=seriesUUIDs)

    def test_a_study_nobody_narrowed_is_served_whole(self):
        self.assertEqual(self._host().instanceUUIDs, ["a1", "a2", "b1"])
        self.assertEqual(self._host({}).instanceUUIDs, ["a1", "a2", "b1"])

    def test_only_the_selected_series_instances_are_offered(self):
        host = self._host({"st1": ["sB"]})
        self.assertEqual(host.instanceUUIDs, ["b1"])

    def test_a_filtered_instance_does_not_exist_as_far_as_the_host_knows(self):
        """Filtered in _loadInstances, not in sendInputs: the rest of the Host
        must never see an instance the selection excluded."""
        host = self._host({"st1": ["sB"]})

        self.assertEqual(host.getMainTags("a1"), {})
        self.assertEqual(host.getInputData("a1"), pydicom.Dataset())
        self.assertIn("not a selected instance", host.messages[-1][1])

    def test_ordering_survives_the_filter(self):
        host = self._host({"st1": ["sA", "sB"]})
        self.assertEqual(host.instanceUUIDs, ["a1", "a2", "b1"])

    def test_narrowing_one_study_leaves_another_whole(self):
        client = FakeOrthanc({
            "st1": self.STUDY,
            "st2": _study([_series("sC", "1")], [_instance("c1", "sC", "1")]),
        })
        host = OrthancHost(client, ["st1", "st2"], seriesUUIDs={"st1": ["sB"]})

        self.assertEqual(host.instanceUUIDs, ["b1", "c1"])

    def test_a_series_the_study_no_longer_holds_is_reported_and_carried_past(self):
        host = self._host({"st1": ["sB", "gone"]})

        # The same treatment a study that cannot be read gets: report, and go
        # on with what is there. A front end restoring a selection is stricter.
        self.assertEqual(host.instanceUUIDs, ["b1"])
        self.assertEqual(host.messages[0][0], Status.ERROR)
        self.assertIn("no longer has selected series", host.messages[0][1])
        self.assertIn("gone", host.messages[0][1])

    def test_the_selection_is_reported_back_sorted(self):
        host = self._host({"st1": ["sB", "sA"]})
        self.assertEqual(host.seriesUUIDs, {"st1": ["sA", "sB"]})

    def test_a_selection_of_no_series_at_all_is_an_empty_run(self):
        # Not the same as the study being absent: it is a study narrowed to
        # nothing, and the run has no instances.
        host = self._host({"st1": []})
        host.setApplication(SendInputsTest.RecordingApp())

        self.assertFalse(host.sendInputs())
        self.assertIn("no instances", host.messages[-1][1])


class FromSelectionFileTest(unittest.TestCase):
    """The file says which level it is, so there is no mode argument."""

    def _write(self, folder, **keys) -> str:
        path = Path(folder) / "selection.json"
        path.write_text(json.dumps({"criteria": {}, **keys}))
        return str(path)

    def test_a_study_level_file_serves_whole_studies(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(folder, study_uids=["st1"])
            host = OrthancHost.fromSelectionFile(
                FakeOrthanc({"st1": SeriesSelectionTest.STUDY}), path)

        self.assertEqual(host.studyUUIDs, ["st1"])
        self.assertEqual(host.seriesUUIDs, {})
        self.assertEqual(host.instanceUUIDs, ["a1", "a2", "b1"])

    def test_a_series_level_file_serves_the_series_it_names(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(folder, selection_level="series",
                               study_uids=["st1"], series_uids={"st1": ["sB"]})
            host = OrthancHost.fromSelectionFile(
                FakeOrthanc({"st1": SeriesSelectionTest.STUDY}), path)

        self.assertEqual(host.instanceUUIDs, ["b1"])

    def test_a_file_that_contradicts_itself_builds_no_host(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(folder, selection_level="study",
                               study_uids=["st1"], series_uids={"st1": ["sB"]})
            with self.assertRaises(ValueError):
                OrthancHost.fromSelectionFile(FakeOrthanc({}), path)

    def test_other_keywords_still_reach_the_constructor(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(folder, study_uids=["st1"])
            host = OrthancHost.fromSelectionFile(
                FakeOrthanc({"st1": SeriesSelectionTest.STUDY}), path,
                uploadOutputs=False, prefetchDepth=0)

        self.assertFalse(host._uploadOutputs)


class OutputTest(unittest.TestCase):
    """notifyOutputAvailable is a get-and-encode and a store, separably."""

    class OutputApp:
        def __init__(self, ds=None, asked=None) -> None:
            self.ds = ds if ds is not None else pydicom.dcmread(str(DATA_FILE))
            self.asked = asked if asked is not None else []

        def getOutputData(self, instanceUUID):
            self.asked.append(instanceUUID)
            return self.ds

    def _host(self, folder, **kwargs) -> OrthancHost:
        return OrthancHost(FakeOrthanc({}), [],
                           outputDir=Path(folder) / "out", **kwargs)

    def test_an_output_is_written_and_uploaded_as_it_always_was(self):
        with tempfile.TemporaryDirectory() as folder:
            host = self._host(folder)
            host._client.post_instances = lambda data: {"ID": "stored"}
            app = self.OutputApp()
            host.setApplication(app)

            self.assertTrue(host.notifyOutputAvailable("i1", True))

            written = Path(folder) / "out" / "i1.dcm"
            self.assertTrue(written.exists())
        self.assertEqual(app.asked, ["i1"])

    def test_store_output_writes_bytes_without_asking_the_application(self):
        with tempfile.TemporaryDirectory() as folder:
            host = self._host(folder, uploadOutputs=False)
            app = self.OutputApp()
            host.setApplication(app)

            self.assertTrue(host.storeOutput("i1", b"already encoded"))

            written = Path(folder) / "out" / "i1.dcm"
            self.assertEqual(written.read_bytes(), b"already encoded")
        # The seam exists so that staged output can be stored long after the
        # Application let go of it.
        self.assertEqual(app.asked, [])

    def test_an_unencodable_dataset_is_reported_by_the_call_that_made_it(self):
        with tempfile.TemporaryDirectory() as folder:
            host = self._host(folder, uploadOutputs=False)
            host.setApplication(self.OutputApp(ds=pydicom.Dataset()))

            self.assertFalse(host.notifyOutputAvailable("i1", True))

        self.assertEqual(host.messages[-1][0], Status.ERROR)
        self.assertIn("could not encode output", host.messages[-1][1])

    def test_encode_output_hands_back_the_bytes_both_sinks_take(self):
        host = OrthancHost(FakeOrthanc({}), [])
        data = host.encodeOutput(pydicom.dcmread(str(DATA_FILE)), "i1")

        self.assertIsInstance(data, bytes)
        self.assertEqual(
            pydicom.dcmread(io.BytesIO(data)).PatientID,
            pydicom.dcmread(str(DATA_FILE)).PatientID)

    def test_a_failed_upload_is_a_failed_store(self):
        def boom(_data):
            raise RuntimeError("no server")

        host = OrthancHost(FakeOrthanc({}), [])
        host._client.post_instances = boom

        self.assertFalse(host.storeOutput("i1", b"x"))
        self.assertIn("could not upload output", host.messages[-1][1])


if __name__ == "__main__":
    unittest.main()
