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
import io
import sys
import tempfile
import unittest
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from OrthancRC.enums import State  # noqa: E402
from OrthancRC.examples.download import DownloadSeries, ProgressBar  # noqa: E402
from OrthancRC.orthanc_util import extractMainTags, instanceUUIDFor  # noqa: E402

DATA_FILE = Path(__file__).resolve().parent / "CT_small.dcm"


class FakeHost:
    """Just enough Host to run the download Application without Orthanc."""

    def __init__(self, instances: dict[str, Dataset], tmpDir: Path,
                 announceCount: bool = False) -> None:
        self._instances = instances
        # Only a host that publishes its selection lets the Application show a
        # percentage; without instanceUUIDs it can count nothing.
        if announceCount:
            self.instanceUUIDs = list(instances)
        self._tmpDir = tmpDir
        self.states: list[State] = []
        self.messages: list[tuple] = []
        self.requested: list[str] = []

    def getTmpDir(self) -> Path:
        return self._tmpDir

    def getInputData(self, instanceUUID: str) -> Dataset:
        self.requested.append(instanceUUID)
        return self._instances.get(instanceUUID, Dataset())

    def notifyStateChanged(self, value) -> None:
        self.states.append(value)

    def notifyStatus(self, value, text: str) -> None:
        self.messages.append((value, text))


def _tags(ds: Dataset, **overrides) -> dict:
    tags = extractMainTags(ds)
    tags.update(overrides)
    return tags


class TestDownloadSeries(unittest.TestCase):
    def setUp(self):
        self.ds = pydicom.dcmread(str(DATA_FILE))
        self.uuid = instanceUUIDFor(self.ds)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpPath = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.host = FakeHost({self.uuid: self.ds}, self.tmpPath / "tmp")

    def _run(self, **kwargs) -> DownloadSeries:
        app = DownloadSeries(self.host, targetFolder=self.tmpPath / "out", **kwargs)
        app.notifyInputAvailable(self.uuid, _tags(self.ds), lastData=True)
        return app

    def test_matching_instance_is_written_into_the_target_folder(self):
        app = self._run(matchModality=str(self.ds.Modality))

        expected = self.tmpPath / "out" / f"{self.ds.SOPInstanceUID}.dcm"
        self.assertTrue(expected.is_file())
        # The file must be readable DICOM, not a bare dataset dump.
        self.assertEqual(str(pydicom.dcmread(str(expected)).SOPInstanceUID),
                         str(self.ds.SOPInstanceUID))
        self.assertEqual(app.seriesFolders,
                         {str(self.ds.SeriesInstanceUID): self.tmpPath / "out"})
        self.assertEqual(self.host.states[-1], State.COMPLETED)

    def test_further_series_go_to_sibling_folders(self):
        # Three instances in three series, offered in order; the first series
        # owns the target folder and the others get -1 and -2.
        instances = {}
        for i in range(3):
            ds = pydicom.dcmread(str(DATA_FILE))
            ds.SeriesInstanceUID = f"1.2.826.0.1.{i}"
            ds.SOPInstanceUID = f"1.2.826.0.2.{i}"
            instances[f"inst{i}"] = ds
        host = FakeHost(instances, self.tmpPath / "tmp")

        app = DownloadSeries(host, targetFolder=self.tmpPath / "out")
        for i, (uuid, ds) in enumerate(instances.items()):
            app.notifyInputAvailable(uuid, _tags(ds), lastData=(i == len(instances) - 1))

        self.assertEqual(app.seriesFolders, {
            "1.2.826.0.1.0": self.tmpPath / "out",
            "1.2.826.0.1.1": self.tmpPath / "out-1",
            "1.2.826.0.1.2": self.tmpPath / "out-2",
        })
        for i in range(3):
            folder = app.seriesFolders[f"1.2.826.0.1.{i}"]
            self.assertEqual([p.name for p in sorted(folder.iterdir())],
                             [f"1.2.826.0.2.{i}.dcm"])

    def test_instances_of_one_series_share_a_folder(self):
        instances = {}
        for i in range(2):
            ds = pydicom.dcmread(str(DATA_FILE))
            ds.SOPInstanceUID = f"1.2.826.0.2.{i}"
            instances[f"inst{i}"] = ds
        host = FakeHost(instances, self.tmpPath / "tmp")

        app = DownloadSeries(host, targetFolder=self.tmpPath / "out")
        for i, (uuid, ds) in enumerate(instances.items()):
            app.notifyInputAvailable(uuid, _tags(ds), lastData=(i == len(instances) - 1))

        self.assertEqual(list(app.seriesFolders.values()), [self.tmpPath / "out"])
        self.assertEqual(
            sorted(p.name for p in (self.tmpPath / "out").iterdir()),
            ["1.2.826.0.2.0.dcm", "1.2.826.0.2.1.dcm"],
        )
        self.assertFalse((self.tmpPath / "out-1").exists())

    def test_no_criteria_downloads_everything(self):
        self._run()
        self.assertEqual(self.host.requested, [self.uuid])

    def test_non_matching_modality_is_never_downloaded(self):
        app = self._run(matchModality="XA")

        # The main tags alone decide, so the data is not even asked for.
        self.assertEqual(self.host.requested, [])
        self.assertEqual(app.seriesFolders, {})
        self.assertFalse((self.tmpPath / "out").exists())

    def test_modality_matches_exactly_and_ignores_case(self):
        app = DownloadSeries(self.host, matchModality="ct")
        self.assertTrue(app.matches({"Modality": "CT"}))
        self.assertFalse(app.matches({"Modality": "CTA"}))
        self.assertFalse(app.matches({}))

    def test_series_description_matches_a_substring(self):
        app = DownloadSeries(self.host, matchSeriesDescription="head")
        self.assertTrue(app.matches({"SeriesDescription": "CT HEAD W/O"}))
        self.assertFalse(app.matches({"SeriesDescription": "CT CHEST"}))

    def test_series_instance_uid_matches_exactly(self):
        app = DownloadSeries(self.host, matchSeriesInstanceUID="1.2.826.0.1.7")
        self.assertTrue(app.matches({"SeriesInstanceUID": "1.2.826.0.1.7"}))
        # A UID is not a prefix or a substring match: a longer UID sharing the
        # same root is a different series.
        self.assertFalse(app.matches({"SeriesInstanceUID": "1.2.826.0.1.70"}))
        self.assertFalse(app.matches({"SeriesInstanceUID": "1.2.826.0.1"}))
        self.assertFalse(app.matches({}))

    def test_series_instance_uid_ignores_surrounding_whitespace(self):
        # DICOM pads a UI value to an even length, so a trailing space is
        # normal and must not stop the series from matching.
        app = DownloadSeries(self.host, matchSeriesInstanceUID=" 1.2.826.0.1.7 ")
        self.assertTrue(app.matches({"SeriesInstanceUID": "1.2.826.0.1.7 "}))

    def test_series_instance_uid_narrows_to_one_series(self):
        # Three series offered; only the named one is downloaded, and it owns
        # the target folder because the others never reach _seriesFolder().
        instances = {}
        for i in range(3):
            ds = pydicom.dcmread(str(DATA_FILE))
            ds.SeriesInstanceUID = f"1.2.826.0.1.{i}"
            ds.SOPInstanceUID = f"1.2.826.0.2.{i}"
            instances[f"inst{i}"] = ds
        host = FakeHost(instances, self.tmpPath / "tmp")

        app = DownloadSeries(
            host,
            matchSeriesInstanceUID="1.2.826.0.1.1",
            targetFolder=self.tmpPath / "out",
        )
        for i, (uuid, ds) in enumerate(instances.items()):
            app.notifyInputAvailable(uuid, _tags(ds), lastData=(i == len(instances) - 1))

        # Only the matching instance was ever pulled from the host.
        self.assertEqual(host.requested, ["inst1"])
        self.assertEqual(app.seriesFolders,
                         {"1.2.826.0.1.1": self.tmpPath / "out"})
        self.assertEqual([p.name for p in (self.tmpPath / "out").iterdir()],
                         ["1.2.826.0.2.1.dcm"])
        self.assertFalse((self.tmpPath / "out-1").exists())

    def test_both_criteria_must_match(self):
        app = DownloadSeries(self.host, matchSeriesDescription="head", matchModality="CT")
        self.assertTrue(app.matches({"Modality": "CT", "SeriesDescription": "Head"}))
        self.assertFalse(app.matches({"Modality": "MR", "SeriesDescription": "Head"}))
        self.assertFalse(app.matches({"Modality": "CT", "SeriesDescription": "Chest"}))

    def test_every_criterion_must_match(self):
        app = DownloadSeries(
            self.host,
            matchSeriesDescription="head",
            matchModality="CT",
            matchSeriesInstanceUID="1.2.826.0.1.7",
        )
        full = {"Modality": "CT", "SeriesDescription": "Head",
                "SeriesInstanceUID": "1.2.826.0.1.7"}
        self.assertTrue(app.matches(full))
        # Any one criterion failing is enough to skip the instance.
        self.assertFalse(app.matches({**full, "SeriesInstanceUID": "1.2.826.0.1.8"}))
        self.assertFalse(app.matches({**full, "Modality": "MR"}))
        self.assertFalse(app.matches({**full, "SeriesDescription": "Chest"}))

    def test_a_failed_download_is_reported_not_written(self):
        # An unknown instance: the host hands back an empty dataset.
        app = DownloadSeries(self.host, targetFolder=self.tmpPath / "out")
        self.assertTrue(app.notifyInputAvailable("missing", {}, lastData=True))

        self.assertEqual(app.seriesFolders, {})
        self.assertFalse((self.tmpPath / "out").exists())
        self.assertIn("failed 1", self.host.messages[-1][1])

    def test_target_folder_defaults_under_the_host_tmp_dir(self):
        app = DownloadSeries(self.host)
        self.assertEqual(app.targetFolder, self.host.getTmpDir() / "download")


class TestDownloadProgress(unittest.TestCase):
    """The bar the download draws while it works through the selection."""

    def _bar(self, total: int, isTTY: bool = True) -> tuple:
        stream = io.StringIO()
        return ProgressBar(total, stream=stream, isTTY=isTTY), stream

    def test_percent_follows_the_items_finished(self):
        bar, _ = self._bar(4)
        self.assertEqual(bar.percent, 0)
        bar.advance()
        self.assertEqual(bar.percent, 25)
        bar.advance(2)
        self.assertEqual(bar.percent, 75)
        bar.finish()
        self.assertEqual(bar.percent, 100)

    def test_a_terminal_gets_a_bar_rewritten_in_place(self):
        bar, stream = self._bar(2)
        bar.advance()
        bar.advance()

        # One line, rewritten: every draw starts by returning to its start.
        self.assertEqual(stream.getvalue().count("\n"), 0)
        drawn = stream.getvalue().split("\r\x1b[K")[1:]
        self.assertEqual(len(drawn), 2)
        self.assertIn("50% (1/2)", drawn[0])
        self.assertIn("100% (2/2)", drawn[1])
        # The bar fills in step with the percentage.
        self.assertEqual(drawn[0].count("#"), drawn[0].count("-"))
        self.assertNotIn("-", drawn[1].split("]")[0])

    def test_finish_completes_the_bar_and_ends_its_line(self):
        bar, stream = self._bar(4)
        bar.advance()
        bar.finish()

        self.assertTrue(stream.getvalue().endswith("\n"))
        self.assertIn("100% (4/4)", stream.getvalue())

    def test_clear_leaves_the_line_only_while_a_bar_is_on_it(self):
        bar, stream = self._bar(2)
        bar.clear()
        # Nothing has been drawn yet, so there is no line to leave.
        self.assertEqual(stream.getvalue(), "")

        bar.advance()
        stream.truncate(0), stream.seek(0)
        bar.clear()
        self.assertEqual(stream.getvalue(), "\r\x1b[K")
        # Already off the line: a second clear must not wipe another one.
        bar.clear()
        self.assertEqual(stream.getvalue(), "\r\x1b[K")

    def test_a_redirected_run_gets_whole_lines_every_ten_percent(self):
        bar, stream = self._bar(100, isTTY=False)
        for _ in range(25):
            bar.advance()
        bar.finish()

        lines = stream.getvalue().splitlines()
        self.assertEqual([line.split("%")[0].split()[-1] for line in lines],
                         ["10", "20", "100"])
        # Nothing is rewritten in place where a carriage return means nothing.
        self.assertNotIn("\r", stream.getvalue())

    def test_an_unknown_total_reports_nothing(self):
        bar, stream = self._bar(0)
        bar.advance()
        bar.finish()
        self.assertEqual(stream.getvalue(), "")

    def test_the_application_counts_every_instance_offered(self):
        instances = {}
        for i in range(4):
            ds = pydicom.dcmread(str(DATA_FILE))
            ds.SOPInstanceUID = f"1.2.826.0.2.{i}"
            instances[f"inst{i}"] = ds
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        host = FakeHost(instances, Path(tmp.name) / "tmp", announceCount=True)

        # Only two instances match, but progress tracks the way through the
        # whole selection, so the skipped ones count too.
        app = DownloadSeries(host, matchModality="CT",
                             targetFolder=Path(tmp.name) / "out")
        # Progress is written to standard error, which is not a terminal here,
        # so every step arrives on a line of its own.
        drawn = io.StringIO()
        with contextlib.redirect_stderr(drawn):
            for i, (uuid, ds) in enumerate(instances.items()):
                tags = _tags(ds)
                if i >= 2:
                    tags["Modality"] = "MR"
                app.notifyInputAvailable(uuid, tags,
                                         lastData=(i == len(instances) - 1))

        self.assertEqual(app.progress.total, 4)
        self.assertEqual(app.progress.percent, 100)
        for expected in ("25% (1/4)", "50% (2/4)", "75% (3/4)", "100% (4/4)"):
            self.assertIn(expected, drawn.getvalue())

    def test_a_host_that_cannot_count_gets_a_silent_bar(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ds = pydicom.dcmread(str(DATA_FILE))
        uuid = instanceUUIDFor(ds)
        host = FakeHost({uuid: ds}, Path(tmp.name) / "tmp")

        app = DownloadSeries(host, targetFolder=Path(tmp.name) / "out")
        drawn = io.StringIO()
        with contextlib.redirect_stderr(drawn):
            app.notifyInputAvailable(uuid, _tags(ds), lastData=True)

        self.assertEqual(app.progress.total, 0)
        self.assertEqual(drawn.getvalue(), "")
        # The download itself is unaffected by having no progress to show.
        self.assertTrue((Path(tmp.name) / "out" / f"{ds.SOPInstanceUID}.dcm").is_file())

    def test_progress_can_be_switched_off(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ds = pydicom.dcmread(str(DATA_FILE))
        uuid = instanceUUIDFor(ds)
        host = FakeHost({uuid: ds}, Path(tmp.name) / "tmp", announceCount=True)

        app = DownloadSeries(host, targetFolder=Path(tmp.name) / "out",
                             showProgress=False)
        self.assertIsNone(app.progress)
        app.notifyInputAvailable(uuid, _tags(ds), lastData=True)
        self.assertTrue((Path(tmp.name) / "out" / f"{ds.SOPInstanceUID}.dcm").is_file())


class TestDownloadCli(unittest.TestCase):
    def test_selection_file_and_criteria_reach_the_host_and_application(self):
        from unittest import mock

        from OrthancRC.examples.download import cli

        with (
            mock.patch.object(cli, "Orthanc") as orthanc,
            mock.patch.object(cli.OrthancHost, "fromSelectionFile") as fromSelectionFile,
        ):
            host = mock.MagicMock()
            host.sendInputs.return_value = True
            fromSelectionFile.return_value = host
            code = cli.main([
                "--from-selection-file", "selection.json",
                "--match-series-description", "head",
                "--match-modality", "CT",
                "--match-series-instance-uid", "1.2.826.0.1.7",
                "--target-folder", "/tmp/series",
            ])

        self.assertEqual(code, 0)
        _client, path = fromSelectionFile.call_args[0]
        self.assertEqual(path, "selection.json")
        # Nothing is produced, so nothing may be uploaded back into Orthanc.
        self.assertFalse(fromSelectionFile.call_args[1]["uploadOutputs"])
        self.assertIs(fromSelectionFile.call_args[0][0], orthanc.return_value)

        app = host.setApplication.call_args[0][0]
        self.assertIsInstance(app, DownloadSeries)
        self.assertEqual(app.targetFolder, Path("/tmp/series"))
        matching = {"Modality": "CT", "SeriesDescription": "Head",
                    "SeriesInstanceUID": "1.2.826.0.1.7"}
        self.assertTrue(app.matches(matching))
        self.assertFalse(app.matches({**matching, "Modality": "MR"}))
        # --match-series-instance-uid reached the Application too.
        self.assertFalse(app.matches({**matching, "SeriesInstanceUID": "1.2.9"}))

    def test_no_progress_reaches_the_application(self):
        from unittest import mock

        from OrthancRC.examples.download import cli

        with (
            mock.patch.object(cli, "Orthanc"),
            mock.patch.object(cli.OrthancHost, "fromSelectionFile") as fromSelectionFile,
        ):
            host = mock.MagicMock()
            host.sendInputs.return_value = True
            fromSelectionFile.return_value = host
            cli.main([
                "--from-selection-file", "selection.json",
                "--target-folder", "/tmp/series",
                "--no-progress",
            ])

        self.assertIsNone(host.setApplication.call_args[0][0].progress)

    def test_refused_input_is_a_nonzero_exit(self):
        from unittest import mock

        from OrthancRC.examples.download import cli

        with (
            mock.patch.object(cli, "Orthanc"),
            mock.patch.object(cli.OrthancHost, "fromSelectionFile") as fromSelectionFile,
        ):
            fromSelectionFile.return_value.sendInputs.return_value = False
            code = cli.main([
                "--from-selection-file", "selection.json",
                "--target-folder", "/tmp/series",
            ])

        self.assertEqual(code, 1)

    def test_unreadable_selection_file_is_an_error(self):
        from unittest import mock

        from OrthancRC.examples.download import cli

        with mock.patch.object(cli, "Orthanc"):
            code = cli.main([
                "--from-selection-file", str(Path(self.id()) / "nope.json"),
                "--target-folder", "/tmp/series",
            ])

        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
