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

"""The series clone: one new series per input series, whatever its size."""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

import pydicom
from pydicom.uid import generate_uid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from OrthancRC.cmdline import CmdLineHost, loadApplicationClass  # noqa: E402
from OrthancRC.examples.cloneseries import CloneSeries, DEFAULT_SUFFIX  # noqa: E402
from OrthancRC.examples.cloneseries.__main__ import (  # noqa: E402
    main as cloneSeriesMain,
    parseArgs as cloneSeriesArgs,
)
from OrthancRC.examples.cloneseries.application import suffixedDescription  # noqa: E402
from OrthancRC.orthanc_util import extractMainTags  # noqa: E402

DATA_FILE = Path(__file__).resolve().parent / "CT_small.dcm"


class CloneSeriesRunTest(unittest.TestCase):
    """A run over two input series of several instances each."""

    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.inputDir = self.folder / "in"
        self.outputDir = self.folder / "out"
        self.inputDir.mkdir()
        # {input SeriesInstanceUID: SeriesDescription}; None leaves it unset,
        # as it is in the sample file.
        self.inputSeries: dict[str, str | None] = {}
        self.studyUID = str(pydicom.dcmread(str(DATA_FILE)).StudyInstanceUID)

    def tearDown(self) -> None:
        self._folder.cleanup()

    def _addSeries(self, count: int, description: str | None) -> str:
        """Write count instances of one new input series; returns its UID."""
        seriesUID = str(generate_uid())
        self.inputSeries[seriesUID] = description
        for _ in range(count):
            ds = pydicom.dcmread(str(DATA_FILE))
            ds.SeriesInstanceUID = seriesUID
            ds.SOPInstanceUID = str(generate_uid())
            ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
            if description is not None:
                ds.SeriesDescription = description
            ds.save_as(str(self.inputDir / f"{ds.SOPInstanceUID}.dcm"),
                       enforce_file_format=True)
        return seriesUID

    def _run(self, **kwargs) -> CloneSeries:
        # Driven the way the command-line host's main() drives it.
        host = CmdLineHost(sorted(self.inputDir.glob("*.dcm")), self.outputDir,
                           self.folder / "tmp")
        app = CloneSeries(host, **kwargs)
        host.setApplication(app)
        uids = host.instanceUIDs
        for i, uid in enumerate(uids):
            tags = extractMainTags(host.getInputData(uid))
            self.assertTrue(app.notifyInputAvailable(uid, tags, i == len(uids) - 1))
        return app

    def _outputs(self) -> list:
        return [pydicom.dcmread(str(p)) for p in self.outputDir.glob("*.dcm")]

    def _outputsBySeries(self) -> dict:
        bySeries: dict = {}
        for ds in self._outputs():
            bySeries.setdefault(str(ds.SeriesInstanceUID), []).append(ds)
        return bySeries

    def test_each_input_series_becomes_exactly_one_new_series(self):
        first = self._addSeries(3, "Head")
        second = self._addSeries(2, "Chest")
        app = self._run()

        bySeries = self._outputsBySeries()
        # Two series in, two out: not one per instance, and not merged.
        self.assertEqual(len(bySeries), 2)
        self.assertEqual(set(app.seriesUIDs), {first, second})
        self.assertEqual(set(app.seriesUIDs.values()), set(bySeries))
        # Each clone holds every instance of the series it came from.
        self.assertEqual(len(bySeries[app.seriesUIDs[first]]), 3)
        self.assertEqual(len(bySeries[app.seriesUIDs[second]]), 2)
        # And none of them reuses a UID the input series already had.
        self.assertFalse(set(bySeries) & set(self.inputSeries))

    def test_every_instance_of_a_clone_carries_the_suffixed_description(self):
        first = self._addSeries(3, "Head")
        second = self._addSeries(2, "Chest")
        app = self._run()

        bySeries = self._outputsBySeries()
        for inputUID, description in ((first, "Head"), (second, "Chest")):
            descriptions = {str(ds.SeriesDescription)
                            for ds in bySeries[app.seriesUIDs[inputUID]]}
            self.assertEqual(descriptions, {description + DEFAULT_SUFFIX})

    def test_every_output_instance_has_a_fresh_sop_instance_uid(self):
        self._addSeries(3, "Head")
        inputSOPs = {pydicom.dcmread(str(p)).SOPInstanceUID
                     for p in self.inputDir.glob("*.dcm")}
        self._run()

        outputs = self._outputs()
        outputSOPs = {str(ds.SOPInstanceUID) for ds in outputs}
        self.assertEqual(len(outputSOPs), 3)
        self.assertFalse(outputSOPs & inputSOPs)
        for ds in outputs:
            self.assertEqual(ds.file_meta.MediaStorageSOPInstanceUID, ds.SOPInstanceUID)

    def test_the_clone_stays_in_the_original_study(self):
        self._addSeries(2, "Head")
        self._run()
        self.assertEqual({str(ds.StudyInstanceUID) for ds in self._outputs()},
                         {self.studyUID})

    def test_the_suffix_can_be_chosen(self):
        self._addSeries(2, "Head")
        app = self._run(suffix=" RECON")
        self.assertEqual(app.suffix, " RECON")
        self.assertEqual({str(ds.SeriesDescription) for ds in self._outputs()},
                         {"Head RECON"})

    def test_a_series_without_a_description_gets_the_suffix_alone(self):
        self._addSeries(2, None)
        self._run()
        self.assertEqual({str(ds.SeriesDescription) for ds in self._outputs()},
                         {DEFAULT_SUFFIX.strip()})

    def test_two_runs_over_the_same_series_do_not_share_a_clone(self):
        self._addSeries(2, "Head")
        firstRun = self._run().seriesUIDs
        secondRun = self._run().seriesUIDs
        self.assertEqual(firstRun.keys(), secondRun.keys())
        self.assertNotEqual(firstRun, secondRun)

    def test_output_is_not_held_once_the_host_has_it(self):
        self._addSeries(3, "Head")
        app = self._run()
        self.assertEqual(app._outputs, {})


class SuffixedDescriptionTest(unittest.TestCase):
    def test_the_suffix_is_appended(self):
        self.assertEqual(suffixedDescription("Head", " (clone)"), "Head (clone)")

    def test_an_overlong_result_trims_the_original_not_the_suffix(self):
        result = suffixedDescription("x" * 64, " (clone)")
        self.assertEqual(len(result), 64)
        self.assertTrue(result.endswith(" (clone)"))

    def test_a_suffix_longer_than_an_lo_is_itself_cut_to_fit(self):
        self.assertEqual(len(suffixedDescription("Head", "y" * 100)), 64)
        self.assertEqual(len(suffixedDescription("", "y" * 100)), 64)


class CloneSeriesCommandLineTest(unittest.TestCase):
    """python -m OrthancRC.examples.cloneseries: the host's options plus --suffix."""

    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.outputDir = self.folder / "out"
        source = pydicom.dcmread(str(DATA_FILE))
        source.SeriesDescription = "Head"
        self.inputFile = self.folder / "head.dcm"
        source.save_as(str(self.inputFile), enforce_file_format=True)

    def tearDown(self) -> None:
        self._folder.cleanup()

    def _descriptions(self) -> set:
        return {str(pydicom.dcmread(str(p)).SeriesDescription)
                for p in self.outputDir.glob("*.dcm")}

    def _main(self, *extra: str) -> None:
        cloneSeriesMain(["--input", str(self.inputFile),
                         "--outputdir", str(self.outputDir),
                         "--tmpdir", str(self.folder / "tmp"), *extra])

    def test_the_suffix_given_is_the_one_used(self):
        self._main("--suffix", " RECON")
        self.assertEqual(self._descriptions(), {"Head RECON"})

    def test_a_suffix_starting_with_a_dash_can_be_given_with_equals(self):
        self._main("--suffix=-copy")
        self.assertEqual(self._descriptions(), {"Head-copy"})

    def test_without_suffix_the_default_is_used(self):
        self._main()
        self.assertEqual(self._descriptions(), {"Head" + DEFAULT_SUFFIX})

    def test_the_host_options_are_the_command_line_hosts_own(self):
        args = cloneSeriesArgs(["--inputlist", "list.txt", "--input", "a.dcm",
                                "--input", "b.dcm"])
        self.assertEqual(args.input, ["a.dcm", "b.dcm"])
        self.assertEqual(args.inputlist, "list.txt")
        self.assertEqual(args.outputdir, ".")
        self.assertIsNone(args.tmpdir)

    def test_no_module_is_asked_for(self):
        # The Application is this one; --module would only be a way to get it wrong.
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cloneSeriesArgs(["--module", "OrthancRC.examples.cloneimage"])

    def test_no_inputs_is_refused_as_the_host_refuses_it(self):
        with self.assertRaises(SystemExit) as caught:
            cloneSeriesMain(["--outputdir", str(self.outputDir)])
        self.assertIn("no input files", str(caught.exception))


class LoadCloneSeriesTest(unittest.TestCase):
    def test_module_is_loadable_with_module(self):
        cls = loadApplicationClass("OrthancRC.examples.cloneseries")
        self.assertIs(cls, CloneSeries)

    def test_the_application_module_itself_loads_this_application(self):
        # It borrows from cloneimage; had it imported CloneInstances, that
        # would be the first Application subclass --module found here.
        cls = loadApplicationClass("OrthancRC.examples.cloneseries.application")
        self.assertIs(cls, CloneSeries)


if __name__ == "__main__":
    unittest.main()
