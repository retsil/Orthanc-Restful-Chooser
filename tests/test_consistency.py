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

"""The small consistency checks, one class per check, numbered as proposed.

Each check either reports (a status line, nothing else changes) or refuses
(something accepted silently before is now an error). Item 6 is not here:
pydicom already rewrites the file meta UIDs from the dataset on every write
these hosts make, so there is nothing left for it to catch.
"""

import argparse
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import pydicom
from pydicom.dataset import Dataset

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import test_host  # noqa: E402 - the fake Orthanc client lives with the host tests
import test_staging  # noqa: E402
from test_host import FakeOrthanc, _instance, _series, _study  # noqa: E402

from OrthancRC import selection  # noqa: E402
from OrthancRC.base import Application  # noqa: E402
from OrthancRC.cmdline import CmdLineHost, loadApplicationClass, runApplication  # noqa: E402
from OrthancRC.enums import State, Status  # noqa: E402
from OrthancRC.examples.cloneimage import CloneInstances  # noqa: E402
from OrthancRC.examples.cloneseries import CloneSeries  # noqa: E402
from OrthancRC.examples.download import DownloadSeries  # noqa: E402
from OrthancRC.orthanc import OrthancHost  # noqa: E402
from OrthancRC.orthanc.staging import StagingHost  # noqa: E402
from OrthancRC.orthanc_util import (extractMainTags, instanceUUID,  # noqa: E402
                                    instanceUUIDFor, seriesUUID, studyUUID)

DATA_FILE = Path(__file__).resolve().parent / "CT_small.dcm"


def _texts(host, status: Status) -> list:
    return [text for level, text in host.messages if level == status]


def _sample(**tags) -> Dataset:
    ds = pydicom.dcmread(str(DATA_FILE))
    for keyword, value in tags.items():
        if value is None:
            delattr(ds, keyword)
        else:
            setattr(ds, keyword, value)
    return ds


class OutputApp:
    """Hands back whatever dataset it is given, under any UUID it is asked for."""

    def __init__(self, ds: Dataset) -> None:
        self.ds = ds

    def getOutputData(self, instanceUUID: str) -> Dataset:
        return self.ds


class _Folder(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.addCleanup(self._folder.cleanup)

    def _orthancHost(self, studies=None, order=None, **kwargs) -> OrthancHost:
        studies = studies or {}
        kwargs.setdefault("outputDir", self.folder / "out")
        kwargs.setdefault("tmpDir", self.folder / "tmp")
        kwargs.setdefault("uploadOutputs", False)
        return OrthancHost(FakeOrthanc(studies), order or list(studies), **kwargs)


# -- output path -------------------------------------------------------------


class AnnouncedTwiceTest(_Folder):
    """1. An output UUID announced twice is refused."""

    def test_the_second_output_under_one_uuid_is_refused(self):
        host = self._orthancHost()
        host.setApplication(OutputApp(_sample(SOPInstanceUID="1.3.9")))

        self.assertTrue(host.notifyOutputAvailable("i1", False))
        self.assertFalse(host.notifyOutputAvailable("i1", True))
        self.assertIn("already announced", _texts(host, Status.ERROR)[0])

    def test_two_spilled_outputs_no_longer_share_a_file(self):
        staging = StagingHost(self._orthancHost(), outputCacheBytes=0)
        app = OutputApp(_sample(SOPInstanceUID="1.3.1"))
        staging.setApplication(app)

        self.assertTrue(staging.notifyOutputAvailable("i1", False))
        app.ds = _sample(SOPInstanceUID="1.3.2")
        self.assertFalse(staging.notifyOutputAvailable("i1", True))

        # The first output's bytes are the ones kept.
        spilled = self.folder / "tmp" / "staged-output" / "i1.dcm"
        self.assertEqual(pydicom.dcmread(str(spilled)).SOPInstanceUID, "1.3.1")
        self.assertEqual(staging.stagedOutputs[0].instanceCount, 1)

    def test_the_command_line_host_refuses_it_too(self):
        host = CmdLineHost([DATA_FILE], self.folder / "out")
        host.setApplication(OutputApp(_sample(SOPInstanceUID="1.3.9")))

        self.assertTrue(host.notifyOutputAvailable("i1", False))
        self.assertFalse(host.notifyOutputAvailable("i1", True))
        self.assertIn("already announced", _texts(host, Status.ERROR)[0])


class OutputIsAnInputTest(_Folder):
    """2. An output with an input's identity is refused."""

    def test_an_input_handed_back_unchanged_is_refused(self):
        inputID = instanceUUID("PID", "1.1.1", "1.2.sA", "1.3.x")
        host = self._orthancHost({"st1": _study(
            [_series("sA", "1")], [_instance(inputID, "sA", "1", sopUID="1.3.x")])})
        host.setApplication(OutputApp(host.getInputData(inputID)))

        self.assertFalse(host.notifyOutputAvailable(inputID, True))
        self.assertIn("needs a new SOPInstanceUID", _texts(host, Status.ERROR)[0])
        self.assertEqual(list((self.folder / "out").iterdir()), [])

    def test_the_command_line_host_refuses_it_too(self):
        host = CmdLineHost([DATA_FILE], self.folder / "out")
        [inputID] = host.instanceUIDs
        host.setApplication(OutputApp(host.getInputData(inputID)))

        self.assertFalse(host.notifyOutputAvailable(inputID, True))
        self.assertEqual(list((self.folder / "out").iterdir()), [])


class UploadStatusTest(_Folder):
    """3. An upload Orthanc answered without storing anything is reported."""

    def _store(self, reply: dict) -> OrthancHost:
        client = test_staging.FakeClient()
        client.post_instances = lambda data: reply
        host = OrthancHost(client, [], tmpDir=self.folder / "tmp")
        self.assertTrue(host.storeOutput("i1", b"encoded"))
        return host

    def test_already_stored_is_a_warning_not_an_upload(self):
        host = self._store({"ID": "x", "Status": "AlreadyStored"})
        self.assertIn("AlreadyStored", _texts(host, Status.WARNING)[0])
        self.assertFalse(any("uploaded" in text for _s, text in host.messages))

    def test_success_is_still_an_upload(self):
        host = self._store({"ID": "x", "Status": "Success"})
        self.assertEqual(_texts(host, Status.WARNING), [])
        self.assertIn("uploaded output as x", _texts(host, Status.INFORMATION)[0])


class AnnouncedNameTest(_Folder):
    """4. An output whose name and identity disagree is reported, and kept."""

    def test_a_mismatch_is_a_warning(self):
        host = self._orthancHost()
        host.setApplication(OutputApp(_sample(SOPInstanceUID="1.3.9")))

        self.assertTrue(host.notifyOutputAvailable("i1", True))
        self.assertIn("announced as i1", _texts(host, Status.WARNING)[0])

    def test_the_examples_name_their_output_after_its_identity(self):
        host = CmdLineHost([DATA_FILE], self.folder / "out")
        app = CloneSeries(host)
        host.setApplication(app)
        [uid] = host.instanceUIDs
        app.notifyInputAvailable(uid, {}, True)
        self.assertEqual(_texts(host, Status.WARNING), [])


class EmptyOutputTest(_Folder):
    """5. An empty output is refused with a message that says so."""

    def test_both_hosts_say_no_data_was_returned(self):
        for host in (self._orthancHost(), CmdLineHost([DATA_FILE], self.folder / "cmd")):
            host.setApplication(OutputApp(Dataset()))
            self.assertFalse(host.notifyOutputAvailable("i1", True))
            self.assertIn("returned no data", _texts(host, Status.ERROR)[-1])


class MixedStagedSeriesTest(_Folder):
    """7. A staged series whose instances disagree is reported once."""

    def test_the_first_disagreement_is_reported_and_no_more(self):
        staging = StagingHost(self._orthancHost())
        app = test_staging.ForgetfulApp(staging)
        staging.setApplication(app)
        for uuid, description in (("i1", "head"), ("i2", "neck"), ("i3", "chest")):
            app.series[uuid] = test_staging._tags("1.2.1", description=description)
            self.assertTrue(staging.notifyOutputAvailable(uuid, False))

        mixed = [text for text in _texts(staging, Status.WARNING) if "mixes" in text]
        self.assertEqual(len(mixed), 1)
        self.assertIn("'neck'", mixed[0])
        # The table still shows the first instance's values.
        self.assertEqual(staging.stagedOutputs[0].description, "head")


class ForeignPatientTest(_Folder):
    """8. An output for a patient or study not among the inputs is reported."""

    STUDY = {"st1": _study([_series("sA", "1")], [_instance("a1", "sA", "1")])}

    def _announce(self, **tags) -> OrthancHost:
        host = self._orthancHost(self.STUDY)
        host.instanceUUIDs  # the inputs, as sendInputs would have listed them
        host.setApplication(OutputApp(_sample(SOPInstanceUID="1.3.9", **tags)))
        self.assertTrue(host.notifyOutputAvailable("o1", True))
        return host

    def test_another_patient_and_study_are_named(self):
        warnings = " ".join(_texts(self._announce(), Status.WARNING))
        self.assertIn("names patient", warnings)
        self.assertIn("names study", warnings)

    def test_the_inputs_own_patient_and_study_pass(self):
        warnings = " ".join(_texts(
            self._announce(PatientID="PID", StudyInstanceUID="1.1.1"), Status.WARNING))
        self.assertNotIn("names patient", warnings)
        self.assertNotIn("names study", warnings)

    def test_the_command_line_host_checks_against_its_files(self):
        host = CmdLineHost([DATA_FILE], self.folder / "out")
        host.setApplication(OutputApp(_sample(SOPInstanceUID="1.3.9", PatientID="OTHER")))
        self.assertTrue(host.notifyOutputAvailable("o1", True))
        self.assertIn("'OTHER'", " ".join(_texts(host, Status.WARNING)))


class OverwriteTest(_Folder):
    """9. A file left in outputDir by an earlier run is reported before it goes."""

    def test_the_orthanc_host_reports_it(self):
        host = self._orthancHost()
        host.storeOutput("i1", b"first")
        host.storeOutput("i1", b"second")
        self.assertIn("overwriting", _texts(host, Status.WARNING)[0])

    def test_the_command_line_host_reports_it(self):
        (self.folder / "out").mkdir()
        host = CmdLineHost([DATA_FILE], self.folder / "out")
        ds = _sample(SOPInstanceUID="1.3.9")
        (self.folder / "out" / f"{instanceUUIDFor(ds)}.dcm").write_bytes(b"old")
        host.setApplication(OutputApp(ds))

        self.assertTrue(host.notifyOutputAvailable(instanceUUIDFor(ds), True))
        self.assertIn("overwriting", _texts(host, Status.WARNING)[0])


# -- input path --------------------------------------------------------------


class WrongFileTest(_Folder):
    """10. A downloaded file that is not the instance listed is refused."""

    class WrongFileOrthanc(FakeOrthanc):
        def _identity(self, uuid: str) -> dict:
            return {**super()._identity(uuid), "sopUID": "9.9.9"}

    def test_it_is_handed_over_as_a_failed_download(self):
        client = self.WrongFileOrthanc(
            {"st1": _study([_series("sA", "1")], [_instance("a1", "sA", "1")])})
        host = OrthancHost(client, ["st1"], tmpDir=self.folder / "tmp")

        self.assertEqual(len(host.getInputData("a1")), 0)
        self.assertIn("not the instance listed", _texts(host, Status.ERROR)[0])

    def test_the_file_that_was_listed_is_handed_over(self):
        host = self._orthancHost(
            {"st1": _study([_series("sA", "1")], [_instance("a1", "sA", "1")])})
        self.assertEqual(host.getInputData("a1").SOPInstanceUID, "1.3.a1")
        self.assertEqual(_texts(host, Status.ERROR), [])


class DuplicateInputFileTest(_Folder):
    """11. Two input files with one identity, or none, are reported."""

    def test_both_paths_are_named(self):
        copy = self.folder / "copy.dcm"
        copy.write_bytes(DATA_FILE.read_bytes())
        host = CmdLineHost([DATA_FILE, copy], self.folder / "out")

        self.assertEqual(len(host.instanceUIDs), 1)
        [warning] = _texts(host, Status.WARNING)
        self.assertIn(str(copy), warning)
        self.assertIn(str(DATA_FILE), warning)

    def test_missing_identifiers_are_named(self):
        bare = self.folder / "bare.dcm"
        _sample(PatientID=None).save_as(str(bare), enforce_file_format=True)
        host = CmdLineHost([bare], self.folder / "out")
        self.assertIn("has no PatientID", _texts(host, Status.WARNING)[0])


class UnknownUUIDTest(_Folder):
    """12. Every host reports an instance it is not serving the same way."""

    def test_the_command_line_host_reports_it(self):
        host = CmdLineHost([DATA_FILE], self.folder / "out")
        self.assertEqual(len(host.getInputData("nope")), 0)
        self.assertEqual(_texts(host, Status.ERROR), ["nope is not a selected instance"])

    def test_main_tags_for_an_unknown_instance_are_reported(self):
        host = self._orthancHost()
        self.assertEqual(host.getMainTags("nope"), {})
        self.assertEqual(_texts(host, Status.ERROR), ["nope is not a selected instance"])


class ChangingStudyTest(_Folder):
    """13. A study still arriving, or changed mid-listing, is reported."""

    def _warnings(self, record, series, instances) -> str:
        host = self._orthancHost({"st1": (record, series, instances)})
        host.instanceUUIDs
        return " ".join(_texts(host, Status.WARNING))

    def test_an_unstable_study_is_reported(self):
        record, series, instances = _study([_series("sA", "1")], [_instance("a1", "sA", "1")])
        record["IsStable"] = False
        self.assertIn("still receiving", self._warnings(record, series, instances))

    def test_listings_that_disagree_are_reported(self):
        record, series, instances = _study([_series("sA", "1")], [_instance("a1", "sA", "1")])
        series[0]["Instances"] = ["a1", "a2"]
        self.assertIn("changed while being read", self._warnings(record, series, instances))

    def test_an_instance_of_an_unlisted_series_is_reported(self):
        warnings = self._warnings(*_study(
            [_series("sA", "1")],
            [_instance("a1", "sA", "1"), _instance("x1", "sX", "1")]))
        self.assertIn("does not list: ['sX']", warnings)

    def test_a_study_in_order_says_nothing(self):
        record, series, instances = _study([_series("sA", "1")], [_instance("a1", "sA", "1")])
        record["IsStable"] = True
        series[0]["Instances"] = ["a1"]
        self.assertEqual(self._warnings(record, series, instances), "")


class EmptyStudyTest(_Folder):
    """14. A study that contributes nothing is reported."""

    STUDY = _study([_series("sA", "1"), _series("sB", "2")], [_instance("a1", "sA", "1")])

    def test_the_constructor_reports_what_it_would_silently_accept(self):
        host = OrthancHost(FakeOrthanc({}), ["st1", "st1"],
                           seriesUUIDs={"st1": [], "st9": ["sA"]})
        warnings = " ".join(_texts(host, Status.WARNING))
        self.assertIn("more than once: ['st1']", warnings)
        self.assertIn("not selected, so ignored: ['st9']", warnings)
        self.assertIn("no series at all: ['st1']", warnings)

    def test_a_study_narrowed_to_an_empty_series_is_reported_when_listed(self):
        host = self._orthancHost({"st1": self.STUDY}, seriesUUIDs={"st1": ["sB"]})
        self.assertEqual(host.instanceUUIDs, [])
        self.assertIn("contributes no instances", _texts(host, Status.WARNING)[0])


class CallingThreadTest(_Folder):
    """15. Data asked for off the run's thread, or after it, is reported once."""

    STUDY = {"st1": _study([_series("sA", "1")],
                           [_instance("a1", "sA", "1"), _instance("a2", "sA", "2")])}

    class ThreadedApp:
        def __init__(self, host) -> None:
            self.host = host

        def notifyInputAvailable(self, instanceUUID, mainTags, lastData) -> bool:
            worker = threading.Thread(target=self.host.getInputData, args=(instanceUUID,))
            worker.start()
            worker.join()
            return True

    def test_another_thread_is_reported_once(self):
        host = self._orthancHost(self.STUDY)
        host.setApplication(self.ThreadedApp(host))
        self.assertTrue(host.sendInputs())
        self.assertEqual(
            [t for t in _texts(host, Status.WARNING) if "other than" in t],
            ["input data asked for from a thread other than the one sendInputs() "
             "is running on"])

    def test_a_call_after_the_run_is_reported_once(self):
        host = self._orthancHost(self.STUDY, prefetchDepth=0)
        host.setApplication(test_host.SendInputsTest.RecordingApp())
        self.assertTrue(host.sendInputs())

        host.getInputData("a1")
        host.getInputData("a2")
        host.close()
        self.assertEqual(len([t for t in _texts(host, Status.WARNING)
                              if "after sendInputs() returned" in t]), 1)


# -- selection file and criteria ---------------------------------------------

CRITERIA = {"patient_id": "PID001", "description": None}
A = studyUUID("PID001", "1.2.1")
B = studyUUID("PID001", "1.2.2")
S1 = seriesUUID("PID001", "1.2.1", "1.2.1.1")


class SaveWhatLoadsTest(_Folder):
    """16. save_selection refuses, and writes nothing, for a file load refuses."""

    def test_series_for_an_unselected_study_are_not_written(self):
        path = self.folder / "s.json"
        with self.assertRaises(ValueError):
            selection.save_selection(str(path), CRITERIA, {A}, {B: {S1}})
        self.assertFalse(path.exists())

    def test_an_empty_series_set_is_not_written(self):
        path = self.folder / "s.json"
        with self.assertRaises(ValueError):
            selection.save_selection(str(path), CRITERIA, {A}, {A: set()})
        self.assertFalse(path.exists())


class OrthancIdentifierTest(unittest.TestCase):
    """17. UUID lists hold Orthanc identifiers, each once."""

    def _load(self, **keys):
        return selection.load_selection_json(json.dumps({"criteria": CRITERIA, **keys}))

    def test_dicom_uids_are_refused_by_name(self):
        with self.assertRaises(ValueError) as caught:
            self._load(study_uids=[A], series_uids={A: ["1.2.840.1"]})
        self.assertIn("not Orthanc identifiers: ['1.2.840.1']", str(caught.exception))

    def test_upper_case_is_not_what_orthanc_writes(self):
        with self.assertRaises(ValueError):
            self._load(study_uids=[A.upper()])

    def test_a_uid_listed_twice_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self._load(study_uids=[A, B, A])
        self.assertIn("more than once", str(caught.exception))


class CriteriaTest(unittest.TestCase):
    """18. Criteria the reader does not know are refused at load."""

    def _load(self, criteria):
        return selection.load_selection_json(
            json.dumps({"criteria": criteria, "study_uids": [A]}))

    def test_an_unknown_key_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self._load({"patient_id": None, "modality": "CT"})
        self.assertIn("['modality']", str(caught.exception))

    def test_a_value_that_is_not_a_string_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self._load({"patient_id": 42})
        self.assertIn("['patient_id']", str(caught.exception))

    def test_a_mismatch_names_the_keys_that_differ(self):
        saved = self._load({"patient_id": "PID001", "description": None})
        with self.assertRaises(ValueError) as caught:
            selection.check_criteria(saved, {"patient_id": "PID002", "description": None})
        message = str(caught.exception)
        self.assertIn("patient_id: saved 'PID001', current 'PID002'", message)
        self.assertNotIn("description", message)

    def test_an_absent_key_differs_from_a_null_one(self):
        saved = self._load({"patient_id": None})
        with self.assertRaises(ValueError) as caught:
            selection.check_criteria(saved, {})
        self.assertIn("patient_id: saved None, current '(absent)'", str(caught.exception))


class DatesTest(unittest.TestCase):
    """19. Dates must be days, and a range must not be inverted."""

    def _args(self, **values) -> argparse.Namespace:
        args = argparse.Namespace(**{field.dest: None for field in selection.SEARCH_FIELDS})
        for key, value in values.items():
            setattr(args, f"search_{key}", value)
        return args

    def test_eight_digits_that_are_no_day_are_refused(self):
        with self.assertRaises(ValueError):
            selection.normalize_date("20241399")
        self.assertEqual(selection.normalize_date("2024-02-29"), "20240229")

    def test_an_inverted_range_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            selection.build_criteria(self._args(study_after="20250101", study_before="20240101"))
        self.assertIn("nothing can match", str(caught.exception))

    def test_a_one_day_range_is_fine(self):
        criteria = selection.build_criteria(
            self._args(study_after="20240101", study_before="2024-01-01"))
        self.assertEqual(criteria["study_after"], criteria["study_before"])


# -- staging bookkeeping -----------------------------------------------------


class StagingTotalsTest(_Folder):
    """20. Totals that drift apart are reported at commit."""

    def _staging(self, **kwargs) -> StagingHost:
        staging = StagingHost(self._orthancHost(), **kwargs)
        app = test_staging.ForgetfulApp(staging)
        staging.setApplication(app)
        app.series["i1"] = test_staging._tags("1.2.1")
        self.assertTrue(staging.notifyOutputAvailable("i1", True))
        return staging

    def test_a_drift_is_an_error(self):
        staging = self._staging()
        staging._heldBytes += 1
        staging.commitStagedOutputs(["1.2.1"])
        self.assertIn("totals disagree", _texts(staging, Status.ERROR)[0])

    def test_totals_that_agree_say_nothing(self):
        staging = self._staging(outputCacheBytes=0)
        staging.commitStagedOutputs(["1.2.1"])
        self.assertEqual(_texts(staging, Status.ERROR), [])


class LeftBehindTest(StagingTotalsTest):
    """21. Spill files that cannot be cleaned up, or written, are reported."""

    def test_leftovers_are_named_and_the_commit_still_returns(self):
        staging = self._staging(outputCacheBytes=0)
        stray = self.folder / "tmp" / "staged-output" / "stray.dcm"
        stray.write_bytes(b"patient data")

        self.assertTrue(staging.commitStagedOutputs(["1.2.1"]))
        self.assertIn("stray.dcm", _texts(staging, Status.ERROR)[0])

    def test_a_spill_that_fails_fails_its_own_call(self):
        staging = StagingHost(self._orthancHost(), outputCacheBytes=0)
        staging.setApplication(test_staging.ForgetfulApp(staging))
        with mock.patch.object(StagingHost, "_spill", side_effect=OSError("disk full")):
            self.assertFalse(staging.notifyOutputAvailable("i1", True))

        self.assertIn("disk full", _texts(staging, Status.ERROR)[0])
        # Nothing of it reached the table, so the totals still agree.
        self.assertEqual(staging.stagedOutputs, [])
        self.assertEqual(staging.spilledBytes, 0)


# -- front ends and examples -------------------------------------------------


class EmptyOutputApp(Application):
    """Announces one output per input and hands back nothing for it."""

    def __init__(self, host) -> None:
        self.host = host

    def notifyInputAvailable(self, instanceUUID, mainTags, lastData) -> bool:
        self.host.notifyOutputAvailable("o-" + instanceUUID, lastData)
        return True

    def getOutputData(self, instanceUUID: str) -> Dataset:
        return Dataset()

    def bringApplicationToFront(self) -> bool:
        return False


class ExitCodeTest(_Folder):
    """22. A run that reported an ERROR does not exit 0."""

    def test_the_command_line_host(self):
        args = argparse.Namespace(input=[str(DATA_FILE)], inputlist=None,
                                  outputdir=str(self.folder / "out"), tmpdir=None)
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stdout(io.StringIO()):
            runApplication(args, EmptyOutputApp)
        self.assertEqual(caught.exception.code, 1)

    def test_the_command_line_host_after_a_clean_run(self):
        args = argparse.Namespace(input=[str(DATA_FILE)], inputlist=None,
                                  outputdir=str(self.folder / "out"), tmpdir=None)
        with contextlib.redirect_stdout(io.StringIO()):
            runApplication(args, CloneInstances)  # returns, rather than exiting

    def test_the_browser(self):
        from OrthancRC.curses import browser
        import test_browser

        host = test_browser.FakeHost()
        host.sendInputs = lambda: host.messages.append((Status.ERROR, "boom")) or True
        with (
            mock.patch.object(browser, "Orthanc"),
            mock.patch.object(browser, "loadApplicationClass",
                              return_value=test_browser.RecordingApplication),
            mock.patch.object(browser, "fetch_all_studies",
                              return_value=[test_browser._record("s1")]),
            mock.patch.object(browser.curses, "wrapper", return_value=({"s1"}, {})),
            mock.patch.object(browser, "OrthancHost", return_value=host),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(browser.main(["--module", "fake_app"]), 1)

    def test_the_download_example(self):
        from OrthancRC.examples.download import cli

        host = mock.MagicMock()
        host.messages = []
        host.sendInputs.side_effect = lambda: host.messages.append((Status.ERROR, "boom")) or True
        with (
            mock.patch.object(cli, "Orthanc"),
            mock.patch.object(cli.OrthancHost, "fromSelectionFile", return_value=host),
        ):
            code = cli.main(["--from-selection-file", "s.json",
                             "--target-folder", str(self.folder), "--no-progress"])
        self.assertEqual(code, 1)


class StrictRestoreTest(_Folder):
    """23. The download example refuses a selection the archive no longer holds."""

    def test_an_error_from_the_listing_stops_the_run_before_it_starts(self):
        from OrthancRC.examples.download import cli

        host = mock.MagicMock()
        host.messages = [(Status.ERROR, "study st1 no longer has selected series")]
        with (
            mock.patch.object(cli, "Orthanc"),
            mock.patch.object(cli.OrthancHost, "fromSelectionFile", return_value=host),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = cli.main(["--from-selection-file", "s.json",
                             "--target-folder", str(self.folder)])
        self.assertEqual(code, 1)
        host.sendInputs.assert_not_called()
        self.assertIn("no longer matches", err.getvalue())


class DownloadFilesTest(_Folder):
    """24. The download example neither overwrites nor mixes runs silently."""

    def setUp(self) -> None:
        super().setUp()
        import test_download
        self.ds = _sample()
        self.uuid = instanceUUIDFor(self.ds)
        self.host = test_download.FakeHost({self.uuid: self.ds}, self.folder / "tmp")
        self.target = self.folder / "series"

    def _run(self) -> None:
        app = DownloadSeries(self.host, targetFolder=self.target, showProgress=False)
        app.notifyInputAvailable(self.uuid, extractMainTags(self.ds), lastData=True)

    def test_an_existing_file_is_a_failure_and_is_kept(self):
        self.target.mkdir()
        existing = self.target / f"{self.ds.SOPInstanceUID}.dcm"
        existing.write_bytes(b"from before")

        self._run()

        self.assertEqual(existing.read_bytes(), b"from before")
        self.assertIn("already exists", _texts(self.host, Status.ERROR)[0])
        self.assertIn("failed 1", self.host.messages[-1][1])

    def test_a_folder_that_is_not_empty_is_reported(self):
        self.target.mkdir()
        (self.target / "other.dcm").write_bytes(b"from before")
        self._run()
        self.assertIn("is not empty", _texts(self.host, Status.WARNING)[0])

    def test_counts_that_do_not_add_up_are_reported(self):
        # The host says there are two, but the run ends after one.
        self.host.instanceUUIDs = [self.uuid, "never-offered"]
        self._run()
        self.assertIn("do not add up", _texts(self.host, Status.WARNING)[0])


class RefusedOutputTest(_Folder):
    """25. The clone examples count the outputs the host would not take."""

    class RefusingHost(CmdLineHost):
        def notifyOutputAvailable(self, instanceUUID: str, lastData: bool) -> bool:
            return False

    def _run(self, appClass):
        host = self.RefusingHost([DATA_FILE], self.folder / "out")
        app = appClass(host)
        [uid] = host.instanceUIDs
        app.notifyInputAvailable(uid, {}, True)
        return host

    def test_cloneimage_reports_them(self):
        host = self._run(CloneInstances)
        self.assertIn("did not take 1 output(s)", _texts(host, Status.WARNING)[0])

    def test_cloneseries_counts_them_as_failed(self):
        host = self._run(CloneSeries)
        self.assertIn("cloned 0 instance(s) in 1 series, failed 1",
                      _texts(host, Status.INFORMATION)[-1])


class AmbiguousModuleTest(unittest.TestCase):
    """26. A module with more than one Application is refused, not guessed at."""

    APP = ("from OrthancRC.base import Application\n"
           "class {name}(Application):\n"
           "    def __init__(self, host): pass\n"
           "    def getOutputData(self, u): pass\n"
           "    def notifyInputAvailable(self, u, t, l): return True\n"
           "    def bringApplicationToFront(self): return False\n")

    def _load(self, name: str, source: str):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, f"{name}.py").write_text(source)
            sys.path.insert(0, folder)
            try:
                return loadApplicationClass(name)
            finally:
                sys.path.remove(folder)
                sys.modules.pop(name, None)

    def test_two_applications_are_refused_by_name(self):
        with self.assertRaises(SystemExit) as caught:
            self._load("two_apps", self.APP.format(name="First")
                       + self.APP.format(name="Second").split("\n", 1)[1])
        self.assertIn("First, Second", str(caught.exception))

    def test_subclassing_an_imported_application_is_refused_without_all(self):
        with self.assertRaises(SystemExit):
            self._load("builds_on", "from OrthancRC.examples.cloneimage import CloneInstances\n"
                                    "class Mine(CloneInstances): pass\n")

    def test_all_narrows_it_to_one(self):
        cls = self._load("builds_on_all",
                         "from OrthancRC.examples.cloneimage import CloneInstances\n"
                         "__all__ = ['Mine']\n"
                         "class Mine(CloneInstances): pass\n")
        self.assertEqual(cls.__name__, "Mine")

    def test_an_abstract_intermediate_is_not_a_candidate(self):
        cls = self._load("abstract_base",
                         "from OrthancRC.base import Application\n"
                         "class Partial(Application): pass\n"
                         + self.APP.format(name="Whole").split("\n", 1)[1]
                         .replace("(Application)", "(Partial)"))
        self.assertEqual(cls.__name__, "Whole")


class BareIntTest(_Folder):
    """27. A bare int passed as a state or status is converted and reported."""

    def test_every_host_converts_a_state_in_range(self):
        for host in (self._orthancHost(), CmdLineHost([DATA_FILE], self.folder / "cmd")):
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                host.notifyStateChanged(State.COMPLETED.value)
            self.assertIn("use State.COMPLETED", _texts(host, Status.WARNING)[0])

    def test_a_state_out_of_range_is_an_error_not_an_exception(self):
        host = self._orthancHost()
        host.notifyStateChanged(99)
        self.assertIn("99 is not a State", _texts(host, Status.ERROR)[0])

    def test_a_status_keeps_its_message_whatever_it_was_sent_as(self):
        host = self._orthancHost()
        host.notifyStatus(0, "information")
        host.notifyStatus(99, "unknown level")
        self.assertIn((Status.INFORMATION, "information"), host.messages)
        self.assertIn((Status.ERROR, "unknown level"), host.messages)
        self.assertIn("99 is not a Status", " ".join(_texts(host, Status.ERROR)))


if __name__ == "__main__":
    unittest.main()
