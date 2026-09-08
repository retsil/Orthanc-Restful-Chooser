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

import sys
import unittest
from pathlib import Path
from unittest import mock

from pydicom.dataset import Dataset

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from OrthancRC.base import Application  # noqa: E402
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


def _record(uid: str) -> browser.StudyRecord:
    return browser.StudyRecord(
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


class TestMainWithModule(unittest.TestCase):
    """main() with --module, against a fake Orthanc and a stubbed picker."""

    def _run(self, argv, selected):
        records = [_record("s1"), _record("s2")]
        with (
            mock.patch.object(browser, "Orthanc") as orthanc,
            mock.patch.object(browser, "loadApplicationClass",
                              return_value=RecordingApplication),
            mock.patch.object(browser, "fetch_all_studies", return_value=records),
            mock.patch.object(browser.curses, "wrapper", return_value=selected),
            mock.patch("OrthancRC.curses.host.OrthancHost") as hostClass,
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
            ["--module", "fake_app", "--output-dir", "/tmp/out", "--no-upload"],
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


if __name__ == "__main__":
    unittest.main()
