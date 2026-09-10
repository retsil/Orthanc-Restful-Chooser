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

"""Output held back for review: what is staged, what is committed, and what
is never asked of the Application."""

import io
import sys
import tempfile
import unittest
from pathlib import Path

import pydicom

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import test_host  # noqa: E402 - the fake Orthanc client lives with the host tests

from OrthancRC.enums import Status  # noqa: E402
from OrthancRC.examples.clone.application import CloneInstances  # noqa: E402
from OrthancRC.orthanc import OrthancHost  # noqa: E402
from OrthancRC.orthanc.staging import (DEFAULT_OUTPUT_CACHE_BYTES, StagingHost,
                                       formatByteSize, parseByteSize)  # noqa: E402

DATA_FILE = Path(__file__).resolve().parent / "CT_small.dcm"


class FakeClient:
    """Just enough of the pyorthanc client to take an upload."""

    def __init__(self) -> None:
        self.uploaded: list = []

    def post_instances(self, data: bytes) -> dict:
        self.uploaded.append(data)
        return {"ID": f"stored{len(self.uploaded)}"}


class ForgetfulApp:
    """An Application that builds its output on demand and then drops it.

    Exactly the Application staging has to work for: asking it again after
    notifyOutputAvailable() has returned gets nothing, which is why the Host
    takes the output while the call is in its hands.
    """

    def __init__(self, host) -> None:
        self.host = host
        self.asked: list = []
        # {instance UUID: series tags its output is stamped with}; one not
        # listed keeps the sample file's own.
        self.series: dict = {}
        self._open = True

    def close(self) -> None:
        self._open = False

    def getOutputData(self, instanceUUID: str):
        self.asked.append(instanceUUID)
        if not self._open:
            raise AssertionError(
                f"asked for {instanceUUID} after the Application let go")
        ds = pydicom.dcmread(str(DATA_FILE))
        ds.SOPInstanceUID = f"1.3.{instanceUUID}"
        for keyword, value in self.series.get(instanceUUID, {}).items():
            if value is None:
                delattr(ds, keyword)
            else:
                setattr(ds, keyword, value)
        return ds


def _tags(seriesUID, number="1", modality="CT", description="head") -> dict:
    # None drops SeriesInstanceUID from the output altogether.
    return {"SeriesNumber": number, "Modality": modality,
            "SeriesDescription": description, "SeriesInstanceUID": seriesUID}


class StagingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)
        self.client = FakeClient()
        self.host = OrthancHost(
            self.client, [], outputDir=self.folder / "out",
            tmpDir=self.folder / "tmp",
        )

    def tearDown(self) -> None:
        self._folder.cleanup()

    def _staging(self, outputCacheBytes=DEFAULT_OUTPUT_CACHE_BYTES):
        staging = StagingHost(self.host, outputCacheBytes=outputCacheBytes)
        app = ForgetfulApp(staging)
        staging.setApplication(app)
        return staging, app

    def _stage(self, staging, *instances) -> None:
        for uuid, seriesUID in instances:
            staging._app.series[uuid] = _tags(seriesUID)
            self.assertTrue(staging.notifyOutputAvailable(uuid, False))

    def _written(self) -> list:
        out = self.folder / "out"
        return sorted(p.name for p in out.iterdir()) if out.exists() else []

    # -- during the run ---------------------------------------------------

    def test_nothing_is_written_or_uploaded_during_the_run(self):
        staging, _app = self._staging()
        self._stage(staging, ("i1", "1.2.1"), ("i2", "1.2.1"))

        self.assertEqual(self._written(), [])
        self.assertEqual(self.client.uploaded, [])

    def test_the_output_is_taken_while_the_call_is_still_in_hand(self):
        staging, app = self._staging()
        self._stage(staging, ("i1", "1.2.1"))
        # The Application may now free everything it produced.
        app.close()

        self.assertEqual(app.asked, ["i1"])
        self.assertTrue(staging.commitStagedOutputs(["1.2.1"]))
        # Committing asked it nothing further.
        self.assertEqual(app.asked, ["i1"])

    def test_an_unencodable_output_still_fails_its_own_call(self):
        staging, app = self._staging()
        app.getOutputData = lambda uuid: pydicom.Dataset()

        self.assertFalse(staging.notifyOutputAvailable("i1", True))
        self.assertIn("could not encode output", staging.messages[-1][1])
        self.assertEqual(staging.stagedOutputs, [])

    def test_a_host_with_no_application_reports_it(self):
        staging = StagingHost(self.host)
        self.assertFalse(staging.notifyOutputAvailable("i1", True))
        self.assertIn("no application registered", staging.messages[-1][1])

    # -- the table --------------------------------------------------------

    def test_output_instances_are_grouped_into_one_row_per_series(self):
        staging, _app = self._staging()
        self._stage(staging, ("i1", "1.2.1"), ("i2", "1.2.1"), ("i3", "1.2.2"))

        rows = staging.stagedOutputs
        self.assertEqual([row.seriesInstanceUID for row in rows],
                         ["1.2.1", "1.2.2"])
        # A count of output instances, which is not the size of any input.
        self.assertEqual([row.instanceCount for row in rows], [2, 1])

    def test_a_row_carries_what_the_output_says_about_its_series(self):
        staging, app = self._staging()
        app.series["i1"] = _tags("1.2.9", number="7", modality="MR",
                                 description="brain")
        staging.notifyOutputAvailable("i1", True)

        row = staging.stagedOutputs[0]
        self.assertEqual((row.seriesNumber, row.modality, row.description),
                         ("7", "MR", "brain"))
        self.assertGreater(row.byteCount, 0)

    def test_rows_come_out_in_the_order_the_run_produced_them(self):
        staging, _app = self._staging()
        self._stage(staging, ("i1", "1.2.2"), ("i2", "1.2.1"))
        self.assertEqual([row.seriesInstanceUID for row in staging.stagedOutputs],
                         ["1.2.2", "1.2.1"])

    def test_an_output_with_no_series_uid_gets_a_row_rather_than_vanishing(self):
        staging, _app = self._staging()
        self._stage(staging, ("i1", None), ("i2", None))

        rows = staging.stagedOutputs
        # A row a user can reject is fine; output missing from the table
        # because an Application was sloppy is not -- so one row each.
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.instanceCount for row in rows], [1, 1])
        self.assertNotEqual(rows[0].key, rows[1].key)

    def test_the_count_includes_instances_that_were_spilled(self):
        staging, _app = self._staging(outputCacheBytes=0)
        self._stage(staging, ("i1", "1.2.1"), ("i2", "1.2.1"))

        self.assertEqual(staging.stagedOutputs[0].instanceCount, 2)
        self.assertEqual(staging.heldBytes, 0)
        self.assertGreater(staging.spilledBytes, 0)

    # -- committing -------------------------------------------------------

    def test_an_accepted_series_is_written_and_uploaded(self):
        staging, _app = self._staging()
        self._stage(staging, ("i1", "1.2.1"), ("i2", "1.2.1"))

        self.assertTrue(staging.commitStagedOutputs(["1.2.1"]))

        self.assertEqual(self._written(), ["i1.dcm", "i2.dcm"])
        self.assertEqual(len(self.client.uploaded), 2)

    def test_a_rejected_series_is_never_written_or_uploaded(self):
        staging, _app = self._staging()
        self._stage(staging, ("i1", "1.2.1"), ("i2", "1.2.2"))

        self.assertTrue(staging.commitStagedOutputs(["1.2.2"]))

        self.assertEqual(self._written(), ["i2.dcm"])
        self.assertEqual(len(self.client.uploaded), 1)

    def test_the_bytes_committed_are_the_bytes_the_application_produced(self):
        staging, _app = self._staging()
        self._stage(staging, ("i1", "1.2.1"))
        staging.commitStagedOutputs(["1.2.1"])

        written = pydicom.dcmread(str(self.folder / "out" / "i1.dcm"))
        self.assertEqual(written.SOPInstanceUID, "1.3.i1")

    def test_rejecting_everything_is_a_confirmed_empty_commit(self):
        staging, _app = self._staging()
        self._stage(staging, ("i1", "1.2.1"))

        # Not an error: it is the decision the table exists to allow.
        self.assertTrue(staging.commitStagedOutputs([]))
        self.assertEqual(self._written(), [])
        self.assertEqual(staging.messages[-1][0], Status.INFORMATION)
        self.assertIn("nothing was written or uploaded",
                      staging.messages[-1][1])

    def test_a_key_that_names_no_row_is_reported(self):
        staging, _app = self._staging()
        self._stage(staging, ("i1", "1.2.1"))

        staging.commitStagedOutputs(["1.2.1", "nonsense"])

        self.assertIn("no staged output series",
                      [text for _status, text in staging.messages][0])
        # The rows that do exist are still committed.
        self.assertEqual(self._written(), ["i1.dcm"])

    def test_a_failed_upload_makes_the_commit_fail(self):
        def boom(_data):
            raise RuntimeError("no server")

        staging, _app = self._staging()
        self._stage(staging, ("i1", "1.2.1"))
        self.client.post_instances = boom

        self.assertFalse(staging.commitStagedOutputs(["1.2.1"]))

    def test_what_was_held_and_what_was_spilled_is_reported(self):
        staging, _app = self._staging(outputCacheBytes=0)
        self._stage(staging, ("i1", "1.2.1"))
        staging.commitStagedOutputs(["1.2.1"])

        # The only way a user finds out the budget was too low for their work.
        said = " ".join(text for _status, text in staging.messages)
        self.assertIn("held in memory", said)
        self.assertIn("spilled to disk", said)

    # -- the memory budget and the spill files ----------------------------

    def test_output_is_held_in_memory_while_it_is_under_the_budget(self):
        staging, _app = self._staging()
        self._stage(staging, ("i1", "1.2.1"))

        self.assertGreater(staging.heldBytes, 0)
        self.assertEqual(staging.spilledBytes, 0)
        self.assertEqual(self._spillFiles(), [])

    def test_past_the_budget_output_goes_to_the_hosts_tmp_dir(self):
        staging, _app = self._staging(outputCacheBytes=0)
        self._stage(staging, ("i1", "1.2.1"))

        self.assertEqual(self._spillFiles(), ["i1.dcm"])
        # The tmp dir the wrapped host already manages, not a second one.
        self.assertEqual(staging.getTmpDir(), self.host.getTmpDir())

    def test_a_spilled_output_commits_exactly_as_a_held_one_does(self):
        staging, _app = self._staging(outputCacheBytes=0)
        self._stage(staging, ("i1", "1.2.1"))

        self.assertTrue(staging.commitStagedOutputs(["1.2.1"]))
        written = pydicom.dcmread(str(self.folder / "out" / "i1.dcm"))
        self.assertEqual(written.SOPInstanceUID, "1.3.i1")

    def test_the_budget_fills_before_it_spills(self):
        # What one of this Application's outputs actually encodes to, rather
        # than the file on disk: it stamps each one, and i1 and i2 encode to
        # the same length.
        sizer = ForgetfulApp(None)
        sizer.series["i1"] = _tags("1.2.1")
        one = len(self.host.encodeOutput(sizer.getOutputData("i1"), "i1"))

        staging, _app = self._staging(outputCacheBytes=one)
        self._stage(staging, ("i1", "1.2.1"), ("i2", "1.2.1"))

        self.assertEqual(staging.heldBytes, one)
        self.assertEqual(staging.spilledBytes, one)
        self.assertEqual(self._spillFiles(), ["i2.dcm"])

    def test_a_review_costs_nothing_once_it_is_over(self):
        staging, _app = self._staging(outputCacheBytes=0)
        self._stage(staging, ("i1", "1.2.1"), ("i2", "1.2.2"))

        staging.commitStagedOutputs(["1.2.1"])

        # Accepted or rejected, no patient data is left in the tmp dir.
        self.assertEqual(self._spillFiles(), [])
        self.assertEqual(staging.stagedOutputs, [])
        self.assertEqual(staging.heldBytes, 0)
        self.assertEqual(staging.spilledBytes, 0)

    def test_discarding_cleans_up_the_paths_that_end_early(self):
        staging, _app = self._staging(outputCacheBytes=0)
        self._stage(staging, ("i1", "1.2.1"))

        staging.discardStagedOutputs()
        staging.discardStagedOutputs()  # and is safe to repeat

        self.assertEqual(self._spillFiles(), [])
        self.assertEqual(self._written(), [])

    def _spillFiles(self) -> list:
        spill = self.host.getTmpDir() / "staged-output"
        return sorted(p.name for p in spill.iterdir()) if spill.exists() else []

    # -- forwarding -------------------------------------------------------

    def test_the_wrapped_host_keeps_the_one_message_list(self):
        staging, _app = self._staging()
        staging.notifyStatus(Status.INFORMATION, "hello")

        # Forwarded rather than inherited, so nothing has a second list of
        # messages that a UI never sees.
        self.assertIs(staging.messages, self.host.messages)
        self.assertEqual(self.host.messages[-1], (Status.INFORMATION, "hello"))

    def test_the_application_is_registered_with_both(self):
        staging, app = self._staging()
        # The wrapped host drives the run, so it needs the same Application.
        self.assertIs(self.host._app, app)

    def test_inputs_are_the_wrapped_hosts_business(self):
        staging, _app = self._staging()
        self.assertEqual(staging.instanceUUIDs, [])
        self.assertEqual(staging.getAvailableScreen(),
                         self.host.getAvailableScreen())
        self.assertTrue(staging.generateUID())

    def test_closing_stops_the_run_without_dropping_the_review(self):
        staging, _app = self._staging(outputCacheBytes=0)
        self._stage(staging, ("i1", "1.2.1"))

        staging.close()

        # Staged output outlives the run by design; that is the point.
        self.assertEqual(len(staging.stagedOutputs), 1)
        self.assertEqual(self._spillFiles(), ["i1.dcm"])


class TwoSeriesOrthanc(test_host.FakeOrthanc):
    """The host tests' fake, serving each instance under its own series.

    The shared fake stamps only SOPInstanceUID, so every instance it serves
    carries the sample file's one SeriesInstanceUID -- and an output table
    grouped by that would be a single row however many series went in.
    """

    def get_instances_id_file(self, uuid: str) -> bytes:
        ds = pydicom.dcmread(str(DATA_FILE))
        ds.SOPInstanceUID = f"1.3.{uuid}"
        ds.SeriesInstanceUID = f"1.2.{uuid[0]}"
        buffer = io.BytesIO()
        pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
        return buffer.getvalue()


class EndToEndTest(unittest.TestCase):
    """A real Application, a real run, and only some of it committed."""

    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.folder = Path(self._folder.name)

    def tearDown(self) -> None:
        self._folder.cleanup()

    def _host(self) -> OrthancHost:
        client = TwoSeriesOrthanc({"st1": test_host._study(
            [test_host._series("sA", "1"), test_host._series("sB", "2")],
            [test_host._instance("a1", "sA", "1"),
             test_host._instance("b1", "sB", "1")],
        )})
        return OrthancHost(client, ["st1"], outputDir=self.folder / "out",
                           tmpDir=self.folder / "tmp", uploadOutputs=False)

    def _written(self) -> list:
        out = self.folder / "out"
        return sorted(p.name for p in out.iterdir()) if out.exists() else []

    def test_a_whole_run_is_reviewed_before_any_of_it_is_written(self):
        host = self._host()
        staging = StagingHost(host, outputCacheBytes=0)
        staging.setApplication(CloneInstances(staging))

        self.assertTrue(staging.sendInputs())
        # The run is over and nothing has been written.
        self.assertEqual(self._written(), [])

        rows = staging.stagedOutputs
        # Two input series in, two output series out, one instance each.
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.instanceCount for row in rows], [1, 1])

        # Keep the first output series and reject the second.
        self.assertTrue(staging.commitStagedOutputs([rows[0].key]))

        self.assertEqual(len(self._written()), 1)
        # And the review left nothing behind it.
        self.assertFalse((host.getTmpDir() / "staged-output").exists())

    def test_confirming_the_table_untouched_matches_the_unstaged_run(self):
        host = self._host()
        staging = StagingHost(host)
        staging.setApplication(CloneInstances(staging))
        staging.sendInputs()
        staging.commitStagedOutputs(row.key for row in staging.stagedOutputs)
        staged = self._outputSeries()

        self.setUp()  # a fresh output dir for the same run without staging
        host = self._host()
        host.setApplication(CloneInstances(host))
        host.sendInputs()

        # The worst a reflexive confirm can do is today's behaviour. The file
        # names differ because the clone takes a fresh UID per run; what was
        # written does not.
        self.assertEqual(staged, self._outputSeries())
        self.assertEqual(len(staged), 2)

    def _outputSeries(self) -> set:
        return {pydicom.dcmread(str(p)).SeriesInstanceUID
                for p in (self.folder / "out").iterdir()}


class ByteSizeTest(unittest.TestCase):
    def test_a_plain_count_and_every_suffix(self):
        self.assertEqual(parseByteSize("128"), 128)
        self.assertEqual(parseByteSize("2K"), 2048)
        self.assertEqual(parseByteSize("128M"), 128 * 1024 ** 2)
        self.assertEqual(parseByteSize("1G"), 1024 ** 3)

    def test_the_default_is_the_value_anyone_would_type(self):
        self.assertEqual(parseByteSize("128M"), DEFAULT_OUTPUT_CACHE_BYTES)

    def test_zero_is_meaningful_and_accepted(self):
        # It spills everything, which is how the spill path is exercised
        # without producing 128 MB of output.
        self.assertEqual(parseByteSize("0"), 0)

    def test_lower_case_and_a_trailing_b_are_the_same_thing(self):
        self.assertEqual(parseByteSize("128m"), parseByteSize("128MB"))

    def test_nonsense_is_refused_with_the_spelling_it_wanted(self):
        for text in ("", "M", "12X", "1.5M", "-1"):
            with self.assertRaises(ValueError):
                parseByteSize(text)

    def test_sizes_are_reported_in_the_units_they_were_given_in(self):
        self.assertEqual(formatByteSize(512), "512 B")
        self.assertEqual(formatByteSize(2048), "2.0 KiB")
        self.assertEqual(formatByteSize(3 * 1024 ** 2), "3.0 MiB")


if __name__ == "__main__":
    unittest.main()
